import os
from datetime import datetime
from bson import ObjectId
from werkzeug.utils import secure_filename

from .audit import log_audit

ALLOWED_LOAN_STATUS = {"ongoing", "closed", "discontinued"}
SUPPORTED_MPR_LEVELS = {"pg", "clf", "block", "district", "state"}


def get_next_sequence(db, key: str) -> int:
    res = db.counters.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
        return_document=True,
    )
    return int(res.get("seq", 1))


def next_pg_auto_id(db) -> str:
    seq = get_next_sequence(db, "pg_auto_id")
    return f"PG-{seq:06d}"


def ensure_pg_locked(pg_doc: dict) -> bool:
    return bool(pg_doc.get("is_locked")) or pg_doc.get("status") == "approved"


def _safe_objectid(value):
    try:
        if isinstance(value, ObjectId):
            return value
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _normalize_mpr_level(level: str) -> str:
    """
    Normalize dashboard/workflow levels.

    CLF_ADMIN is a login role, but MPR aggregation level is "clf".
    BLOCK_ADMIN/DISTRICT_ADMIN/ADMIN are roles, but aggregation levels are
    "block", "district", and "state".
    """
    raw = str(level or "").strip().lower()
    aliases = {
        "pg_data_entry": "pg",
        "pg": "pg",
        "clf_admin": "clf",
        "clf_manager": "clf",
        "clf": "clf",
        "block_admin": "block",
        "block": "block",
        "district_admin": "district",
        "district": "district",
        "admin": "state",
        "state_admin": "state",
        "super_admin": "state",
        "state": "state",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in SUPPORTED_MPR_LEVELS:
        raise ValueError(f"Unsupported MPR level: {level}")
    return normalized


def _pg_scope_query_for_level(level: str, ref_obj):
    """
    Build the PG lookup query for aggregated MPR scopes.

    This supports the new CLF hierarchy:
      CLF_ADMIN -> clf_id
      BLOCK_ADMIN -> block_id
      DISTRICT_ADMIN -> district_id
      ADMIN/SUPER_ADMIN -> state_id
    """
    level = _normalize_mpr_level(level)

    if level == "pg":
        return {"_id": ref_obj}

    return {f"{level}_id": ref_obj}


def _to_float(value, default=0.0):
    try:
        return float(value or default)
    except Exception:
        return float(default)


def _to_int(value, default=0):
    try:
        return int(value or default)
    except Exception:
        return int(default)


def create_change_request(db, *, collection: str, doc_id, proposed_changes: dict, user: dict, reason: str = None):
    req = {
        "collection": collection,
        "doc_id": str(doc_id),
        "proposed_changes": proposed_changes or {},
        "reason": reason,
        "status": "pending",
        "created_by": user,
        "created_at": datetime.utcnow(),
        "decision": None,
        "history": [
            {"ts": datetime.utcnow(), "by": user, "action": "created", "note": reason}
        ],
    }
    db.change_requests.insert_one(req)
    log_audit(
        db,
        action="change_request:create",
        collection=collection,
        doc_id=doc_id,
        user=user,
        after=req,
    )
    return req


def decide_change_request(db, *, request_id, approve: bool, user: dict, note: str = None):
    cr = db.change_requests.find_one({"_id": ObjectId(request_id)})
    if not cr:
        return None

    if cr.get("status") != "pending":
        return cr

    status = "approved" if approve else "rejected"
    now = datetime.utcnow()

    update = {
        "$set": {
            "status": status,
            "decision": {"by": user, "ts": now, "note": note},
            "decided_at": now,
            "updated_at": now,
        },
        "$push": {
            "history": {
                "ts": now,
                "by": user,
                "action": status,
                "note": note,
            }
        },
    }

    db.change_requests.update_one({"_id": ObjectId(request_id)}, update)
    cr = db.change_requests.find_one({"_id": ObjectId(request_id)})

    if approve:
        doc_id = ObjectId(cr["doc_id"]) if ObjectId.is_valid(cr["doc_id"]) else cr["doc_id"]
        before = db[cr["collection"]].find_one({"_id": doc_id})
        proposed_changes = cr.get("proposed_changes") or {}
        proposed_changes["updated_at"] = datetime.utcnow()

        db[cr["collection"]].update_one(
            {"_id": doc_id},
            {"$set": proposed_changes},
        )

        after = db[cr["collection"]].find_one({"_id": doc_id})
        log_audit(
            db,
            action="change_request:apply",
            collection=cr["collection"],
            doc_id=doc_id,
            user=user,
            before=before,
            after=after,
            meta={"change_request_id": str(cr["_id"])},
        )
    else:
        log_audit(
            db,
            action="change_request:reject",
            collection=cr["collection"],
            doc_id=cr["doc_id"],
            user=user,
            meta={"change_request_id": str(cr["_id"])},
        )

    return cr


def add_notification(
    db,
    *,
    to_user_id: str = None,
    to_role: str = None,
    title: str = "",
    body: str = "",
    link: str = None,
    level: str = "info",
    meta=None,
):
    meta = meta or {}
    n = {
        "ts": datetime.utcnow(),
        "to_user_id": to_user_id,
        "to_role": to_role,
        "title": title,
        "body": body,
        "link": link,
        "level": level,
        "meta": meta,
        "is_read": False,
    }
    db.notifications.insert_one(n)
    return n


def save_uploaded_document(app, *, pg_id: str, file_storage, doc_type: str, user: dict):
    upload_root = app.config.get("UPLOAD_FOLDER", "uploads")
    safe_type = secure_filename(doc_type or "document")
    safe_name = secure_filename(file_storage.filename or "upload")
    rel_dir = os.path.join("pg_documents", pg_id, safe_type)
    abs_dir = os.path.join(upload_root, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}__{safe_name}"
    abs_path = os.path.join(abs_dir, filename)
    file_storage.save(abs_path)

    doc = {
        "pg_id": ObjectId(pg_id) if ObjectId.is_valid(pg_id) else pg_id,
        "doc_type": doc_type,
        "filename": filename,
        "relative_path": os.path.join(rel_dir, filename),
        "uploaded_by": user,
        "uploaded_at": datetime.utcnow(),
    }
    app.mongo_db.pg_documents.insert_one(doc)
    log_audit(
        app.mongo_db,
        action="document:upload",
        collection="pg_documents",
        doc_id=doc.get("_id", filename),
        user=user,
        after=doc,
    )
    return doc


def calc_fund_balance(fund_doc: dict) -> dict:
    total_received = _to_float(fund_doc.get("total_received", 0))
    total_utilized = _to_float(fund_doc.get("total_utilized", 0))
    balance = total_received - total_utilized
    fund_doc["balance"] = round(balance, 2)
    return fund_doc


def calc_turnover_and_stock(db, pg_id: str, year: int, month: int):
    """Compute month KPI inputs for MPR.

    Manual mapping:
      - Turnover = internal/member business total (6.1) + market/outsider total (6.2)
      - Stock = closing stock at end of month
      - Member involvement % = involved members / total PG members * 100

    This remains PG-level source-of-truth. CLF/Block/District/State summaries
    aggregate these PG-level values.
    """
    pg_obj = ObjectId(pg_id) if ObjectId.is_valid(str(pg_id)) else pg_id
    q = {"pg_id": pg_obj, "year": int(year), "month": int(month)}

    internal = db.pg_business_monthly.find_one(q) or {}
    internal_total = _to_float(internal.get("internal_total") or internal.get("total_internal") or 0)

    market = db.pg_market_transactions.find_one(q) or {}
    market_total = _to_float(
        market.get("market_total")
        or market.get("total_market")
        or market.get("total_turnover")
        or 0
    )

    total_turnover = round(internal_total + market_total, 2)

    closing_stock_value = 0.0
    stock_doc = db.pg_stocks_monthly.find_one({**q, "commodity": {"$exists": False}}) or {}
    if stock_doc:
        for k in ("closing_stock_value", "closing_value", "closing_stock", "closing_amount"):
            if k in stock_doc:
                closing_stock_value = _to_float(stock_doc.get(k))
                break
    else:
        rows = list(
            db.pg_stocks_monthly.find(
                q,
                {"closing_value": 1, "closing_stock_value": 1, "closing_qty": 1},
            )
        )
        for row in rows:
            if "closing_value" in row:
                closing_stock_value += _to_float(row.get("closing_value"))
            elif "closing_stock_value" in row:
                closing_stock_value += _to_float(row.get("closing_stock_value"))

    closing_stock_value = round(closing_stock_value, 2)

    total_members = db.pg_members.count_documents({"pg_id": pg_obj})
    members_inputs = _to_int(internal.get("members_input_count"))
    members_outputs = _to_int(internal.get("members_output_count"))

    def _pct(n, d):
        return round((float(n) / float(d)) * 100.0, 2) if d else 0.0

    return {
        "internal_total": round(internal_total, 2),
        "market_total": round(market_total, 2),
        "total_turnover": total_turnover,
        "closing_stock_value": closing_stock_value,
        "members_input_count": members_inputs,
        "members_output_count": members_outputs,
        "members_input_pct": _pct(members_inputs, total_members),
        "members_output_pct": _pct(members_outputs, total_members),
        "total_members": total_members,
    }


def _loan_summary_for_pg(db, pg_obj):
    loan_outstanding = 0.0
    loan_overdue = 0.0
    active_loans = 0

    for ln in db.pg_loan_accounts.find(
        {"pg_id": pg_obj},
        {"outstanding_amount": 1, "overdue_amount": 1, "status": 1},
    ):
        loan_outstanding += _to_float(ln.get("outstanding_amount"))
        loan_overdue += _to_float(ln.get("overdue_amount"))
        if ln.get("status") in ("active", "ongoing", "npa"):
            active_loans += 1

    for ln in db.pg_member_loan_accounts.find(
        {"pg_id": pg_obj},
        {"outstanding_amount": 1, "overdue_amount": 1, "status": 1},
    ):
        loan_outstanding += _to_float(ln.get("outstanding_amount"))
        loan_overdue += _to_float(ln.get("overdue_amount"))
        if ln.get("status") in ("active", "ongoing", "npa"):
            active_loans += 1

    return {
        "loan_outstanding_total": round(loan_outstanding, 2),
        "loan_overdue_total": round(loan_overdue, 2),
        "loan_active_count": active_loans,
    }


def _aggregate_mpr_for_scope(db, *, level: str, ref_obj, year: int, month: int):
    """
    Aggregate MPR values for CLF/Block/District/State.

    It first identifies PGs by scope, then reuses the PG-level calculation to
    avoid breaking existing MPR display/export logic.
    """
    level = _normalize_mpr_level(level)
    pg_query = _pg_scope_query_for_level(level, ref_obj)
    pgs = list(db.pgs.find(pg_query, {"_id": 1}))

    pg_ids = [pg["_id"] for pg in pgs]
    total_turnover = 0.0
    internal_total = 0.0
    market_total = 0.0
    closing_stock_value = 0.0
    total_members = 0
    members_input_count = 0
    members_output_count = 0
    loan_outstanding = 0.0
    loan_overdue = 0.0
    active_loans = 0

    for pg_obj in pg_ids:
        metrics = calc_turnover_and_stock(db, str(pg_obj), year, month)
        total_turnover += _to_float(metrics.get("total_turnover"))
        internal_total += _to_float(metrics.get("internal_total"))
        market_total += _to_float(metrics.get("market_total"))
        closing_stock_value += _to_float(metrics.get("closing_stock_value"))
        total_members += _to_int(metrics.get("total_members"))
        members_input_count += _to_int(metrics.get("members_input_count"))
        members_output_count += _to_int(metrics.get("members_output_count"))

        loan_summary = _loan_summary_for_pg(db, pg_obj)
        loan_outstanding += _to_float(loan_summary.get("loan_outstanding_total"))
        loan_overdue += _to_float(loan_summary.get("loan_overdue_total"))
        active_loans += _to_int(loan_summary.get("loan_active_count"))

    def _pct(n, d):
        return round((float(n) / float(d)) * 100.0, 2) if d else 0.0

    return {
        "pg_count": len(pg_ids),
        "internal_total": round(internal_total, 2),
        "market_total": round(market_total, 2),
        "total_turnover": round(total_turnover, 2),
        "closing_stock_value": round(closing_stock_value, 2),
        "members_input_count": members_input_count,
        "members_output_count": members_output_count,
        "members_input_pct": _pct(members_input_count, total_members),
        "members_output_pct": _pct(members_output_count, total_members),
        "total_members": total_members,
        "loan_status": None,
        "loan_estimated_amount": 0,
        "loan_outstanding_total": round(loan_outstanding, 2),
        "loan_overdue_total": round(loan_overdue, 2),
        "loan_active_count": active_loans,
    }


def generate_mpr_snapshot(db, *, level: str, ref_id, year: int, month: int, user: dict):
    """Auto-generate an MPR snapshot.

    Supported levels:
      - pg
      - clf
      - block
      - district
      - state

    The new CLF_ADMIN role uses level="clf". Block/District/State can aggregate
    all PGs under their scope, including the PGs mapped to CLFs.
    """
    level = _normalize_mpr_level(level)
    y = int(year)
    m = int(month)

    if isinstance(ref_id, str) and ObjectId.is_valid(ref_id):
        ref_obj = ObjectId(ref_id)
    else:
        ref_obj = ref_id

    snapshot = {
        "level": level,
        "ref_id": ref_obj,
        "year": y,
        "month": m,
        "generated_at": datetime.utcnow(),
        "generated_by": user,
    }

    if level == "pg":
        metrics = calc_turnover_and_stock(db, str(ref_obj), y, m)
        loan_summary = _loan_summary_for_pg(db, ref_obj)

        snapshot["monthly_turnover"] = _to_float(metrics.get("total_turnover"))
        snapshot["closing_stock_value"] = _to_float(metrics.get("closing_stock_value"))
        snapshot["metrics"] = {
            **metrics,
            "loan_status": None,
            "loan_estimated_amount": 0,
            **loan_summary,
        }
    else:
        metrics = _aggregate_mpr_for_scope(
            db,
            level=level,
            ref_obj=ref_obj,
            year=y,
            month=m,
        )
        snapshot["monthly_turnover"] = _to_float(metrics.get("total_turnover"))
        snapshot["closing_stock_value"] = _to_float(metrics.get("closing_stock_value"))
        snapshot["metrics"] = metrics

    db.mpr_snapshots.update_one(
        {"level": level, "ref_id": ref_obj, "year": y, "month": m},
        {"$set": snapshot},
        upsert=True,
    )

    log_audit(
        db,
        action="mpr:generate",
        collection="mpr_snapshots",
        doc_id=f"{level}:{ref_obj}:{y}-{m}",
        user=user,
        after=snapshot,
    )

    return snapshot
