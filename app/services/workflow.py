import os
from datetime import datetime
from bson import ObjectId
from werkzeug.utils import secure_filename

from .audit import log_audit

ALLOWED_LOAN_STATUS = {"ongoing", "closed", "discontinued"}

def get_next_sequence(db, key: str) -> int:
    res = db.counters.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
        return_document=True,
    )
    # pymongo's return_document=True returns updated doc
    return int(res.get("seq", 1))

def next_pg_auto_id(db) -> str:
    seq = get_next_sequence(db, "pg_auto_id")
    return f"PG-{seq:06d}"

def ensure_pg_locked(pg_doc: dict) -> bool:
    return bool(pg_doc.get("is_locked")) or pg_doc.get("status") == "approved"

def create_change_request(db, *, collection: str, doc_id, proposed_changes: dict, user: dict, reason: str = None):
    req = {
        "collection": collection,
        "doc_id": str(doc_id),
        "proposed_changes": proposed_changes,
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
    log_audit(db, action="change_request:create", collection=collection, doc_id=doc_id, user=user, after=req)
    return req

def decide_change_request(db, *, request_id, approve: bool, user: dict, note: str = None):
    cr = db.change_requests.find_one({"_id": ObjectId(request_id)})
    if not cr:
        return None
    if cr.get("status") != "pending":
        return cr

    status = "approved" if approve else "rejected"
    update = {
        "$set": {
            "status": status,
            "decision": {"by": user, "ts": datetime.utcnow(), "note": note},
            "decided_at": datetime.utcnow(),
        },
        "$push": {"history": {"ts": datetime.utcnow(), "by": user, "action": status, "note": note}},
    }
    db.change_requests.update_one({"_id": ObjectId(request_id)}, update)
    cr = db.change_requests.find_one({"_id": ObjectId(request_id)})

    if approve:
        # Apply changes
        doc_id = ObjectId(cr["doc_id"]) if ObjectId.is_valid(cr["doc_id"]) else cr["doc_id"]
        before = db[cr["collection"]].find_one({"_id": doc_id})
        db[cr["collection"]].update_one({"_id": doc_id}, {"$set": cr["proposed_changes"], "$set": {"updated_at": datetime.utcnow()}})
        after = db[cr["collection"]].find_one({"_id": doc_id})
        log_audit(db, action="change_request:apply", collection=cr["collection"], doc_id=doc_id, user=user, before=before, after=after, meta={"change_request_id": str(cr["_id"])})
    else:
        log_audit(db, action="change_request:reject", collection=cr["collection"], doc_id=cr["doc_id"], user=user, meta={"change_request_id": str(cr["_id"])})
    return cr

def add_notification(db, *, to_user_id: str = None, to_role: str = None, title: str = "", body: str = "", link: str = None, level: str = "info", meta=None):
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
    log_audit(app.mongo_db, action="document:upload", collection="pg_documents", doc_id=doc.get("_id", filename), user=user, after=doc)
    return doc

def calc_fund_balance(fund_doc: dict) -> dict:
    # Generic balance calculation; does not assume specific heads
    total_received = float(fund_doc.get("total_received", 0) or 0)
    total_utilized = float(fund_doc.get("total_utilized", 0) or 0)
    balance = total_received - total_utilized
    fund_doc["balance"] = round(balance, 2)
    return fund_doc

def calc_turnover_and_stock(db, pg_id: str, year: int, month: int):
    """Compute month KPI inputs for MPR.

    Manual mapping:
      - Turnover = internal (6.1 total) + market (6.2 total)
      - Stock = closing stock at end of month (sum of all commodities if commodity-wise stored)
      - Member involvement % = members involved / total members * 100
    """
    q = {"pg_id": ObjectId(pg_id), "year": int(year), "month": int(month)}

    # 6.1 (internal/member) totals
    internal = db.pg_business_monthly.find_one(q) or {}
    internal_total = float(internal.get("internal_total") or internal.get("total_internal") or 0)

    # 6.2 (market) totals
    market = db.pg_market_transactions.find_one(q) or {}
    market_total = float(market.get("market_total") or market.get("total_market") or market.get("total_turnover") or 0)

    total_turnover = round(internal_total + market_total, 2)

    # Stock: support both (a) single monthly doc and (b) commodity-wise rows
    closing_stock_value = 0.0
    stock_doc = db.pg_stocks_monthly.find_one({**q, "commodity": {"$exists": False}}) or {}
    if stock_doc:
        for k in ("closing_stock_value", "closing_value", "closing_stock", "closing_amount"):
            if k in stock_doc:
                try:
                    closing_stock_value = float(stock_doc.get(k) or 0)
                    break
                except Exception:
                    pass
    else:
        # commodity-wise sum
        rows = list(db.pg_stocks_monthly.find(q, {"closing_value": 1, "closing_stock_value": 1, "closing_qty": 1}))
        for r in rows:
            if "closing_value" in r:
                try:
                    closing_stock_value += float(r.get("closing_value") or 0)
                except Exception:
                    pass
            elif "closing_stock_value" in r:
                try:
                    closing_stock_value += float(r.get("closing_stock_value") or 0)
                except Exception:
                    pass
    closing_stock_value = round(closing_stock_value, 2)

    # Member involvement (best-effort)
    total_members = db.pg_members.count_documents({"pg_id": ObjectId(pg_id)})
    members_inputs = int(internal.get("members_input_count") or 0)
    members_outputs = int(internal.get("members_output_count") or 0)

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

def generate_mpr_snapshot(db, *, level: str, ref_id, year: int, month: int, user: dict):
    """Auto-generate an MPR snapshot.

    level: 'pg' | 'block' | 'district' | 'state'
    ref_id: ObjectId / str (pg_id etc.)
    """
    y = int(year); m = int(month)
    if isinstance(ref_id, str) and ObjectId.is_valid(ref_id):
        ref_obj = ObjectId(ref_id)
    else:
        ref_obj = ref_id

    snapshot = {"level": level, "ref_id": ref_obj, "year": y, "month": m, "generated_at": datetime.utcnow(), "generated_by": user}

    if level == "pg":
        metrics = calc_turnover_and_stock(db, str(ref_obj), y, m)

        # Legacy loan summary (kept for backward compatibility)
        loan = db.pg_loans.find_one({"pg_id": ref_obj}) or {}

        # New loan lifecycle summary (preferred)
        loan_outstanding = 0.0
        loan_overdue = 0.0
        active_loans = 0
        for ln in db.pg_loan_accounts.find({"pg_id": ref_obj}, {"outstanding_amount": 1, "overdue_amount": 1, "status": 1}):
            try:
                loan_outstanding += float(ln.get("outstanding_amount") or 0)
            except Exception:
                pass
            try:
                loan_overdue += float(ln.get("overdue_amount") or 0)
            except Exception:
                pass
            if (ln.get("status") in ("active", "ongoing", "npa")):
                active_loans += 1
        for ln in db.pg_member_loan_accounts.find({"pg_id": ref_obj}, {"outstanding_amount": 1, "overdue_amount": 1, "status": 1}):
            try:
                loan_outstanding += float(ln.get("outstanding_amount") or 0)
            except Exception:
                pass
            try:
                loan_overdue += float(ln.get("overdue_amount") or 0)
            except Exception:
                pass
            if (ln.get("status") in ("active", "ongoing", "npa")):
                active_loans += 1

        # Important: keep a top-level 'monthly_turnover' so dashboards can SUM it.
        snapshot["monthly_turnover"] = float(metrics.get("total_turnover") or 0)
        snapshot["closing_stock_value"] = float(metrics.get("closing_stock_value") or 0)
        snapshot["metrics"] = {
            **metrics,
            "loan_status": loan.get("status") or loan.get("loan_status"),
            "loan_estimated_amount": loan.get("estimated_amount", loan.get("loan_amount", 0)),
            "loan_outstanding_total": round(loan_outstanding, 2),
            "loan_overdue_total": round(loan_overdue, 2),
            "loan_active_count": active_loans,
        }
    else:
        # Aggregate by PGs under the geo ref
        match = {"year": y, "month": m}
        # best-effort: try join by pg_id list under ref (block/district/state)
        pgs = list(db.pgs.find({f"{level}_id": ref_obj}, {"_id": 1}))
        pg_ids = [p["_id"] for p in pgs]
        match["pg_id"] = {"$in": pg_ids} if pg_ids else {"$in": []}
        agg = list(db.pg_market_transactions.aggregate([
            {"$match": match},
            {"$group": {"_id": None, "total_turnover": {"$sum": "$total_turnover"}}}
        ]))
        snapshot["metrics"] = {"total_turnover": agg[0]["total_turnover"] if agg else 0, "pg_count": len(pg_ids)}

    db.mpr_snapshots.update_one(
        {"level": level, "ref_id": ref_obj, "year": y, "month": m},
        {"$set": snapshot},
        upsert=True
    )
    log_audit(db, action="mpr:generate", collection="mpr_snapshots", doc_id=f"{level}:{ref_obj}:{y}-{m}", user=user, after=snapshot)
    return snapshot
