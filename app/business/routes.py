from services.audit_engine import AuditLogger
from flask import render_template, request, redirect, url_for, flash, current_app, session, jsonify, g
from app.services.guards import require_unlocked_period
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
import jwt

from . import business_bp
from ..rbac import login_required, roles_required


BUSINESS_VIEW_ROLES = (
    "PG_DATA_ENTRY",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
)

BUSINESS_WRITE_ROLES = {
    "PG_DATA_ENTRY",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
}


def _wants_json_response():
    accept = (request.headers.get("Accept") or "").lower()
    content_type = (request.content_type or "").lower()
    requested_with = (request.headers.get("X-Requested-With") or "").lower()
    return (
        request.is_json
        or "application/json" in accept
        or "application/json" in content_type
        or request.args.get("format") == "json"
        or requested_with == "xmlhttprequest"
    )


def _to_object_id(value):
    try:
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _id_list(values):
    out = []
    for value in values or []:
        oid = _to_object_id(value)
        if oid:
            out.append(oid)
    return out


def _business_error(message, status=403, redirect_endpoint="pg.pg_home"):
    if _wants_json_response():
        return jsonify({"success": False, "ok": False, "message": message}), status
    flash(message, "danger" if status >= 400 else "warning")
    return redirect(url_for(redirect_endpoint))

def _bearer_token():
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def _decode_mobile_token():
    token = _bearer_token()
    if not token:
        return {}

    secrets = [
        current_app.config.get("JWT_SECRET_KEY"),
        current_app.config.get("SECRET_KEY"),
    ]

    for secret in secrets:
        if not secret:
            continue

        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            ) or {}
        except Exception:
            continue

    return {}


def _current_request_user_doc(db):
    """
    Web uses Flask session.
    Mobile app uses Bearer token.
    This helper loads the same user context for both.
    """
    raw_user_id = (
        session.get("user_id")
        or getattr(g, "user_id", None)
        or getattr(g, "current_user_id", None)
    )

    payload = {}

    current_user = getattr(g, "current_user", None)
    if isinstance(current_user, dict):
        payload.update(current_user)

    token_payload = _decode_mobile_token()
    if isinstance(token_payload, dict):
        payload.update(token_payload)

    raw_user_id = (
        raw_user_id
        or payload.get("user_id")
        or payload.get("id")
        or payload.get("_id")
        or payload.get("sub")
    )

    user_obj_id = _to_object_id(raw_user_id)
    if user_obj_id:
        user_doc = db.users.find_one({"_id": user_obj_id})
        if user_doc:
            return user_doc

    username = payload.get("username") or payload.get("email") or session.get("username")
    if username:
        user_doc = db.users.find_one({
            "$or": [
                {"username": username},
                {"email": username},
                {"phone": username},
            ]
        })
        if user_doc:
            return user_doc

    return {}


def _scope_value(user_doc, *keys):
    for key in keys:
        value = session.get(key)
        if value not in (None, "", []):
            return value

    for key in keys:
        value = user_doc.get(key)
        if value not in (None, "", []):
            return value

    return None

def _get_scoped_pg(db, pg_obj_id):
    """
    Return PG only if it is inside the logged-in user's jurisdiction.

    Supports:
    - Web browser Flask session
    - Mobile app Bearer JWT token
    """
    user_doc = _current_request_user_doc(db)

    role = (
        session.get("role")
        or user_doc.get("role")
        or user_doc.get("user_role")
        or ""
    )

    role = str(role or "").upper()

    user_id = _to_object_id(
        session.get("user_id")
        or user_doc.get("_id")
        or user_doc.get("user_id")
    )

    state_id = _to_object_id(_scope_value(user_doc, "state_id"))
    district_id = _to_object_id(_scope_value(user_doc, "district_id"))
    block_id = _to_object_id(_scope_value(user_doc, "block_id"))
    clf_id = _to_object_id(_scope_value(user_doc, "clf_id"))

    own_pg_id = _to_object_id(
        _scope_value(
            user_doc,
            "pg_id",
            "active_pg_id",
            "mapped_pg_id",
            "assigned_pg_id",
        )
    )

    assigned_pg_ids = _id_list(
        _scope_value(user_doc, "assigned_pg_ids", "pg_ids", "mapped_pg_ids") or []
    )

    q = {"_id": pg_obj_id}

    if role in ("SUPER_ADMIN", "ADMIN"):
        if role == "ADMIN" and state_id:
            q["state_id"] = state_id
        return db.pgs.find_one(q)

    if role == "DISTRICT_ADMIN":
        if not district_id:
            return None
        q["district_id"] = district_id
        return db.pgs.find_one(q)

    if role == "BLOCK_ADMIN":
        if not block_id:
            return None
        q["block_id"] = block_id
        return db.pgs.find_one(q)

    if role in ("CLF_ADMIN", "CLF_MANAGER"):
        scope_or = []

        if clf_id:
            scope_or.append({"clf_id": clf_id})

        if user_id:
            scope_or.append({"assigned_clf_user_id": user_id})

        if assigned_pg_ids:
            scope_or.append({"_id": {"$in": assigned_pg_ids}})

        if not scope_or:
            return None

        q["$or"] = scope_or
        return db.pgs.find_one(q)

    if role == "PG_DATA_ENTRY":
        if own_pg_id and str(own_pg_id) == str(pg_obj_id):
            return db.pgs.find_one(q)

        # fallback for mobile users where user document may not store pg_id,
        # but requested PG has the same assigned user id.
        if user_id:
            pg = db.pgs.find_one({
                "_id": pg_obj_id,
                "$or": [
                    {"user_id": user_id},
                    {"created_by": user_id},
                    {"pg_user_id": user_id},
                    {"assigned_user_id": user_id},
                    {"data_entry_user_id": user_id},
                ],
            })
            if pg:
                return pg

        return None

    return None

def _can_write_business(pg=None):
    role = session.get("role") or ""
    if role not in BUSINESS_WRITE_ROLES:
        return False

    if role == "PG_DATA_ENTRY" and pg:
        own_pg_id = _to_object_id(session.get("pg_id") or session.get("active_pg_id"))
        return bool(own_pg_id and str(own_pg_id) == str(pg.get("_id")))

    return True


def _guard_business_write(pg=None, message="This business module is view-only for your role."):
    if _can_write_business(pg):
        return None
    return _business_error(message, 403)

#changes made by atlanta
@business_bp.route("/income_expenditure/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def income_expenditure(pg_id):
    db = current_app.mongo_db

    def wants_json():
        accept = (request.headers.get("Accept") or "").lower()
        content_type = (request.content_type or "").lower()
        return (
            request.is_json
            or "application/json" in accept
            or "application/json" in content_type
            or request.args.get("format") == "json"
        )

    def fval(src, key, default=0.0):
        try:
            return float(src.get(key, default) or default)
        except Exception:
            return float(default)

    def ival(src, key, default=0):
        try:
            return int(src.get(key, default) or default)
        except Exception:
            return int(default)

    try:
        pg_obj_id = ObjectId(pg_id)
    except InvalidId:
        if wants_json():
            return jsonify({"success": False, "message": "Invalid PG ID."}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        if wants_json():
            return jsonify({"success": False, "message": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    now = datetime.utcnow()

    if request.method == "POST":
        write_denied = _guard_business_write(pg)
        if write_denied:
            return write_denied

        if request.is_json:
            src = request.get_json(silent=True) or {}
            year = ival(src, "year", now.year)
            month = ival(src, "month", now.month)
        else:
            src = request.form
            year = ival(src, "year", now.year)
            month = ival(src, "month", now.month)

        income = {
            "startup_cost_received": fval(src, "startup_cost_received"),
            "membership_fees": fval(src, "membership_fees"),
            "interest_received_from_members": fval(src, "interest_received_from_members"),
            "income_selling_product": fval(src, "income_selling_product"),
            "income_selling_input_to_member": fval(src, "income_selling_input_to_member"),
            "income_selling_input_to_outsider": fval(src, "income_selling_input_to_outsider"),
            "other_income": fval(src, "other_income"),
        }

        expenditure = {
            "establishment_cost_utilized": fval(src, "establishment_cost_utilized"),
            "input_procurement": fval(src, "input_procurement"),
            "interest_paid_against_loan": fval(src, "interest_paid_against_loan"),
            "product_procurement": fval(src, "product_procurement"),
            "recurring_expenditure": fval(src, "recurring_expenditure"),
            "other_expenditure": fval(src, "other_expenditure"),
        }

        total_income = round(sum(income.values()), 2)
        total_expenditure = round(sum(expenditure.values()), 2)
        excess_income = round(total_income - total_expenditure, 2)

        doc = {
            "pg_id": pg_obj_id,
            "year": year,
            "month": month,
            "income": income,
            "expenditure": expenditure,
            "total_income": total_income,
            "total_expenditure": total_expenditure,
            "excess_income_over_expenditure": excess_income,
            "updated_at": datetime.utcnow(),
        }

        db.pg_income_expenditure.update_one(
            {"pg_id": pg_obj_id, "year": year, "month": month},
            {"$set": doc},
            upsert=True,
        )

        saved = db.pg_income_expenditure.find_one(
            {"pg_id": pg_obj_id, "year": year, "month": month}
        )

        if wants_json():
            return jsonify({
                "success": True,
                "message": "Income–Expenditure saved successfully.",
                "pg": {
                    "id": str(pg["_id"]),
                    "name": pg.get("pg_name") or pg.get("name") or "Producer Group",
                },
                "record": {
                    "id": str(saved.get("_id", "")) if saved else "",
                    "year": saved.get("year") if saved else None,
                    "month": saved.get("month") if saved else None,
                    "income": saved.get("income", {}) if saved else {},
                    "expenditure": saved.get("expenditure", {}) if saved else {},
                    "total_income": saved.get("total_income", 0) if saved else 0,
                    "total_expenditure": saved.get("total_expenditure", 0) if saved else 0,
                    "excess_income_over_expenditure": saved.get("excess_income_over_expenditure", 0) if saved else 0,
                    "updated_at": saved.get("updated_at").isoformat() if saved and saved.get("updated_at") else None,
                }
            }), 200

        flash("Income–Expenditure saved.", "success")
        return redirect(url_for("business.income_expenditure", pg_id=pg_id))

    year = request.args.get("year")
    month = request.args.get("month")

    query = {"pg_id": pg_obj_id}

    if year is not None and str(year).strip() != "":
        try:
            query["year"] = int(year)
        except Exception:
            if wants_json():
                return jsonify({"success": False, "message": "Invalid year."}), 400
            flash("Invalid year.", "danger")
            return redirect(url_for("business.income_expenditure", pg_id=pg_id))

    if month is not None and str(month).strip() != "":
        try:
            query["month"] = int(month)
        except Exception:
            if wants_json():
                return jsonify({"success": False, "message": "Invalid month."}), 400
            flash("Invalid month.", "danger")
            return redirect(url_for("business.income_expenditure", pg_id=pg_id))

    if "year" in query and "month" in query:
        record = db.pg_income_expenditure.find_one(query)
    else:
        record = db.pg_income_expenditure.find_one(
            {"pg_id": pg_obj_id},
            sort=[("year", -1), ("month", -1)]
        )

    if wants_json():
        return jsonify({
            "success": True,
            "pg": {
                "id": str(pg["_id"]),
                "name": pg.get("pg_name") or pg.get("name") or "Producer Group",
            },
            "filters": {
                "year": int(year) if year not in (None, "") else None,
                "month": int(month) if month not in (None, "") else None,
            },
            "record": {
                "id": str(record.get("_id", "")) if record else "",
                "year": record.get("year") if record else None,
                "month": record.get("month") if record else None,
                "income": record.get("income", {}) if record else {},
                "expenditure": record.get("expenditure", {}) if record else {},
                "total_income": record.get("total_income", 0) if record else 0,
                "total_expenditure": record.get("total_expenditure", 0) if record else 0,
                "excess_income_over_expenditure": record.get("excess_income_over_expenditure", 0) if record else 0,
                "updated_at": record.get("updated_at").isoformat() if record and record.get("updated_at") else None,
            } if record else None
        }), 200

    return render_template("income_expenditure.html", pg=pg, record=record)

# ============================================================
# NEW: STOCK / INVENTORY TRACKING
# - Opening/closing stock commodity-wise (monthly)
# - Movement ledger (purchase/sale/adjustment)
# ============================================================

# changes made by atlanta
@business_bp.route("/stock_register/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def stock_register(pg_id):
    """Manual 6.5: monthly input/product closing stock; new quantities use kg.

    POST accepts one row (legacy form/API) or JSON {year, month, rows: [...]}.
    Manual rows use stock_type, commodity, closing_qty, rate and optional id.
    Omitted rows are never deleted. An id edits that row within the same period.
    Old clients can still send closing_value without rate or stock_type.
    GET keeps monthly_records/movements and adds sections, summary and locks.
    """
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    from pymongo.errors import DuplicateKeyError, PyMongoError
    from app.services.periods import is_period_locked

    db = current_app.mongo_db
    coll = db.pg_stocks_monthly
    wants_json = bool(_wants_json_response() or request.headers.get("Authorization"))
    year = month = None

    def fail(message, status=400, **extra):
        if wants_json:
            return jsonify(success=False, ok=False, message=message, error=message, **extra), status
        flash(message, "danger")
        params = {"pg_id": pg_id}
        if year is not None and month is not None:
            params.update(year=year, month=month)
        return redirect(url_for("business.stock_register", **params))

    def period_number(value, label, low, high):
        try:
            if isinstance(value, bool):
                raise ValueError()
            result = int(str(value))
            if not low <= result <= high:
                raise ValueError()
            return result
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be between {low} and {high}.")

    def number(value, label, places=3):
        try:
            if isinstance(value, bool) or value in (None, ""):
                raise ValueError()
            result = Decimal(str(value))
            if not result.is_finite() or result < 0 or result > Decimal("1000000000000"):
                raise ValueError()
            return result.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"{label} must be a valid non-negative number (maximum 1 trillion).")

    def date_text(value):
        return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)

    def display_number(value):
        try:
            result = Decimal(str(value or 0))
            return float(result) if result.is_finite() else 0.0
        except (InvalidOperation, TypeError, ValueError):
            return 0.0

    def serialize(doc):
        qty = display_number(doc.get("closing_qty"))
        value = display_number(doc.get("closing_value"))
        category = doc.get("stock_type")
        category = category if category in ("input", "product") else "unclassified"
        unit = doc.get("unit") or ""
        rate = display_number(doc.get("rate")) if doc.get("rate") is not None else (value / qty if qty else 0)
        return {
            "id": str(doc.get("_id", "")), "year": doc.get("year"), "month": doc.get("month"),
            "stock_type": category, "commodity": doc.get("commodity") or "",
            "unit": unit, "closing_qty": qty, "rate": round(rate, 4), "closing_value": value,
            "opening_qty": display_number(doc.get("opening_qty")),
            "opening_value": display_number(doc.get("opening_value")),
            "valuation_method": doc.get("valuation_method") or "avg",
            "updated_at": date_text(doc.get("updated_at")),
            "needs_review": category == "unclassified" or unit != "kg",
        }

    pg_obj_id = _to_object_id(pg_id)
    if pg_obj_id is None:
        return fail("Invalid PG ID.")
    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        return fail("PG not found or outside your access scope.", 404)

    role = str(getattr(g, "role", None) or session.get("role") or "").upper()
    # Scope has already been checked above. JWT and session users share write rules.
    can_write = role in BUSINESS_WRITE_ROLES
    now = datetime.utcnow()
    src = request.get_json(silent=True) if request.is_json else request.form
    if request.method == "POST" and not isinstance(src, dict):
        return fail("Send a stock form or a JSON object.")
    if src is None:
        src = {}
    try:
        period_src = src if request.method == "POST" else request.args
        year = period_number(period_src.get("year", request.args.get("year", now.year)), "Year", 1900, 9999)
        month = period_number(period_src.get("month", request.args.get("month", now.month)), "Month", 1, 12)
    except ValueError as exc:
        return fail(str(exc))

    period_query = {"pg_id": pg_obj_id, "year": year, "month": month}
    locked = is_period_locked(db, scope="pg", ref_id=str(pg_obj_id), year=year, month=month)

    def build_payload():
        # No 200-row truncation: summaries cover the entire selected month.
        records = list(coll.find(period_query).sort([("stock_type", 1), ("commodity", 1)]))
        rows = [serialize(doc) for doc in records]
        sections = {key: [row for row in rows if row["stock_type"] == key]
                    for key in ("input", "product", "unclassified")}
        summary = {"period": {"year": year, "month": month}, "item_count": len(rows),
                   "total_value": round(sum(row["closing_value"] for row in rows), 2),
                   "quantity_kg": round(sum(row["closing_qty"] for row in rows if row["unit"] == "kg"), 3),
                   "needs_review_count": sum(row["needs_review"] for row in rows)}
        for key, entries in sections.items():
            summary[key] = {"item_count": len(entries),
                            "quantity_kg": round(sum(row["closing_qty"] for row in entries if row["unit"] == "kg"), 3),
                            "value": round(sum(row["closing_value"] for row in entries), 2)}
        # Retain movement response for existing mobile clients and the old template.
        movements = list(db.pg_stock_movements.find({"pg_id": pg_obj_id}).sort([("ts", -1)]).limit(100))
        serialized_movements = [{
            "id": str(doc.get("_id", "")), "commodity": doc.get("commodity") or "",
            "movement_type": doc.get("movement_type") or "purchase",
            "qty": display_number(doc.get("qty")), "rate": display_number(doc.get("rate")),
            "amount": display_number(doc.get("amount")), "remarks": doc.get("remarks") or "",
            "ts": date_text(doc.get("ts")), "created_at": date_text(doc.get("created_at")),
        } for doc in movements]
        return {
            "success": True, "ok": True,
            "pg": {"id": str(pg_obj_id), "name": pg.get("pg_name") or pg.get("name") or "Producer Group"},
            "year": year, "month": month, "filters": {"year": year, "month": month},
            "monthly_records": rows, "sections": sections, "summary": summary,
            "movements": serialized_movements, "locked": locked, "can_edit": can_write and not locked,
        }, records, movements

    if request.method == "POST":
        if not can_write:
            return fail("This business module is view-only for your role.", 403)
        if locked:
            return fail("This period is locked/approved. Editing is disabled.", 423)
        batch = "rows" in src
        raw_rows = src.get("rows") if batch else [src]
        if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > 500:
            return fail("Send between 1 and 500 stock rows per save.")

        existing = list(coll.find(period_query))
        by_id = {str(doc["_id"]): doc for doc in existing}
        prepared = []
        target_ids = set()
        target_keys = set()
        try:
            for row_no, row in enumerate(raw_rows, 1):
                if not isinstance(row, dict):
                    raise ValueError(f"Row {row_no}: invalid stock row.")
                commodity = row.get("commodity", row.get("item", ""))
                if not isinstance(commodity, str):
                    raise ValueError(f"Row {row_no}: enter an item name.")
                commodity = " ".join(commodity.split())
                if not commodity or len(commodity) > 200:
                    raise ValueError(f"Row {row_no}: item name must contain 1 to 200 characters.")
                row_id = str(row.get("id") or "")
                previous = by_id.get(row_id) if row_id else None
                if row_id and previous is None:
                    raise ValueError(f"Row {row_no}: entry not found in this PG and month. Reload before editing.")
                supplied_type = row.get("stock_type")
                manual = batch or supplied_type is not None
                category = str(supplied_type or "").strip().lower() if manual else (previous or {}).get("stock_type")
                if manual and category not in ("input", "product"):
                    raise ValueError(f"Row {row_no}: choose Input or Product.")
                if manual and row.get("unit", "kg") != "kg":
                    raise ValueError(f"Row {row_no}: quantity must be in kg.")
                key = (category, commodity.casefold())
                if key in target_keys:
                    raise ValueError(f"Row {row_no}: this item appears twice in the same stock section.")
                target_keys.add(key)
                # Resolve names case-insensitively without rewriting legacy records.
                matches = [doc for doc in existing
                           if " ".join(str(doc.get("commodity") or "").split()).casefold() == commodity.casefold()
                           and (doc.get("stock_type") == category or (not manual and not row_id))]
                if len(matches) > 1:
                    raise ValueError(f"Row {row_no}: multiple matching entries exist. Reload and edit by entry ID.")
                if matches:
                    if previous and matches[0]["_id"] != previous["_id"]:
                        raise ValueError(f"Row {row_no}: another entry already uses this item and stock type.")
                    previous = previous or matches[0]
                if previous:
                    if str(previous["_id"]) in target_ids:
                        raise ValueError(f"Row {row_no}: the same saved entry was included twice.")
                    target_ids.add(str(previous["_id"]))
                    if not manual:
                        category = previous.get("stock_type")
                    if row.get("updated_at") and row["updated_at"] != date_text(previous.get("updated_at")):
                        raise ValueError(f"Row {row_no}: entry changed since it was loaded. Reload before saving.")
                qty = number(row.get("closing_qty", row.get("quantity")), f"Row {row_no} quantity")
                if manual or row.get("rate") not in (None, ""):
                    rate = number(row.get("rate"), f"Row {row_no} rate", 4)
                    value = (qty * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    # Compatibility with the existing form/mobile closing-value contract.
                    value = number(row.get("closing_value", 0), f"Row {row_no} value", 2)
                    if qty == 0 and value != 0:
                        raise ValueError(f"Row {row_no}: zero quantity must have zero value.")
                    rate = (value / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if qty else Decimal(0)
                if value > Decimal("1000000000000"):
                    raise ValueError(f"Row {row_no}: total value is too large.")
                fields = dict(period_query, commodity=commodity, closing_qty=float(qty),
                              closing_value=float(value), rate=float(rate), updated_at=datetime.utcnow())
                if manual:
                    fields.update(stock_type=category, unit="kg", schema_version=2)
                # Preserve opening data; only old clients can explicitly update it.
                if not manual:
                    for name in ("opening_qty", "opening_value"):
                        if name in row:
                            fields[name] = float(number(row[name], f"Row {row_no} {name}"))
                    if "valuation_method" in row:
                        fields["valuation_method"] = str(row.get("valuation_method") or "avg")
                if previous:
                    query = dict(period_query, _id=previous["_id"])
                    # Optimistic check also protects against edits during this request.
                    query["updated_at"] = previous.get("updated_at")
                else:
                    query = dict(period_query, commodity=commodity, stock_type=category)
                prepared.append((query, fields, previous))
        except ValueError as exc:
            return fail(str(exc))

        saved_ids = []
        try:
            for query, fields, previous in prepared:
                if previous:
                    result = coll.update_one(query, {"$set": fields})
                    if result.matched_count != 1:
                        return fail("An entry changed during saving. Reload before retrying.", 409,
                                    saved_ids=saved_ids, partial_save=bool(saved_ids))
                    saved_ids.append(str(previous["_id"]))
                else:
                    # Insert after validation; the unique index rejects concurrent duplicates.
                    result = coll.insert_one(dict(fields, created_at=datetime.utcnow()))
                    saved_ids.append(str(result.inserted_id))
        except DuplicateKeyError:
            return fail("A matching stock entry already exists, or the old stock index is still active. "
                        "Reload the month; if this persists, confirm File 1 is installed and Flask restarted.",
                        409, saved_ids=saved_ids, partial_save=bool(saved_ids))
        except PyMongoError:
            current_app.logger.exception("Monthly stock save failed")
            return fail("Stock save could not finish. Reload the month before retrying.", 503,
                        saved_ids=saved_ids, partial_save=bool(saved_ids))
        if wants_json:
            payload, _, _ = build_payload()
            payload.update(message="Monthly stock saved successfully.", saved_ids=saved_ids)
            return jsonify(payload), 200
        flash("Monthly stock saved successfully.", "success")
        return redirect(url_for("business.stock_register", pg_id=pg_id, year=year, month=month))

    payload, records, movements = build_payload()
    if wants_json:
        return jsonify(payload), 200
    return render_template("stock_register.html", pg=pg, records=records, movements=movements,
                           year=year, month=month, stock_data=payload, summary=payload["summary"],
                           sections=payload["sections"], can_edit=payload["can_edit"], period_locked=locked)

# changes made by atlanta
@business_bp.route("/stock_movement/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def stock_movement(pg_id):
    db = current_app.mongo_db

    def wants_json():
        accept = (request.headers.get("Accept") or "").lower()
        content_type = (request.content_type or "").lower()
        return (
            request.is_json
            or "application/json" in accept
            or "application/json" in content_type
            or request.args.get("format") == "json"
        )

    def fval(src, key, default=0.0):
        try:
            return float(src.get(key, default) or default)
        except Exception:
            return float(default)

    try:
        pg_obj_id = ObjectId(pg_id)
    except InvalidId:
        if wants_json():
            return jsonify({"success": False, "message": "Invalid PG ID."}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        if wants_json():
            return jsonify({"success": False, "message": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    write_denied = _guard_business_write(pg)
    if write_denied:
        return write_denied

    src = request.get_json(silent=True) or {} if request.is_json else request.form

    commodity = (src.get("commodity") or "").strip()
    if not commodity:
        if wants_json():
            return jsonify({"success": False, "message": "Commodity is required."}), 400
        flash("Commodity is required.", "danger")
        return redirect(url_for("business.stock_register", pg_id=pg_id))

    qty = fval(src, "qty")
    rate = fval(src, "rate")

    doc = {
        "pg_id": pg_obj_id,
        "ts": datetime.utcnow(),
        "commodity": commodity,
        "movement_type": (src.get("movement_type") or "purchase").strip().lower(),
        "qty": qty,
        "rate": rate,
        "amount": qty * rate,
        "remarks": src.get("remarks") or "",
        "created_at": datetime.utcnow(),
    }

    db.pg_stock_movements.insert_one(doc)

    if wants_json():
        return jsonify({
            "success": True,
            "message": "Stock movement added successfully.",
            "movement": {
                "id": str(doc.get("_id", "")),
                "commodity": doc["commodity"],
                "movement_type": doc["movement_type"],
                "qty": doc["qty"],
                "rate": doc["rate"],
                "amount": doc["amount"],
                "remarks": doc["remarks"],
                "ts": doc["ts"].isoformat(),
            }
        }), 200

    flash("Stock movement added.", "success")
    return redirect(url_for("business.stock_register", pg_id=pg_id))



# ============================================================
# NEW: BUSINESS PLAN TRACKING (planned vs actual)
# ============================================================

@business_bp.route("/business_plan/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def business_plan(pg_id):
    db = current_app.mongo_db
    pg_obj_id = _to_object_id(pg_id)
    if not pg_obj_id:
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        write_denied = _guard_business_write(pg)
        if write_denied:
            return write_denied

        year = int(request.form.get("year") or datetime.utcnow().year)
        plan = {
            "turnover_target": float(request.form.get("turnover_target") or 0),
            "profit_target": float(request.form.get("profit_target") or 0),
            "member_coverage_target": int(request.form.get("member_coverage_target") or 0),
            "notes": request.form.get("notes") or "",
        }
        doc = {"pg_id": pg_obj_id, "year": year, "plan": plan, "updated_at": datetime.utcnow()}
        db.pg_business_plans.update_one({"pg_id": pg_obj_id, "year": year}, {"$set": doc}, upsert=True)
        flash("Business plan saved.", "success")
        return redirect(url_for("business.business_plan", pg_id=pg_id))

    # latest plan
    plan_doc = db.pg_business_plans.find_one({"pg_id": pg_obj_id}, sort=[("year", -1)])
    # actuals from market transactions (sum year)
    actual_turnover = 0.0
    if plan_doc:
        y = int(plan_doc.get("year") or datetime.utcnow().year)
        agg = list(db.pg_market_transactions.aggregate([
            {"$match": {"pg_id": pg_obj_id, "year": y}},
            {"$group": {"_id": None, "turnover": {"$sum": "$total_turnover"}}}
        ]))
        actual_turnover = float(agg[0]["turnover"] if agg else 0)
    return render_template("business_plan.html", pg=pg, plan_doc=plan_doc, actual_turnover=actual_turnover)


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available


# ============================================================
# NEW: PG BUSINESS MONTHLY (Manual Section 6.1 + 6.2 + 6.4)
# - 6.1: Internal/member transactions summary + member involvement counts
# - 6.2: Market/outsider transactions summary
# - 6.4: Turnover = 6.1 total + 6.2 total (auto)
# NOTE: We store totals only (no breaking changes). Detailed member-wise
#       capture can be added later without changing existing schema.
# ============================================================

from services.kpi_engine import KPIEngine

# changes made by atlanta
@business_bp.route("/monthly_business/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def monthly_business(pg_id):
    db = current_app.mongo_db

    def wants_json():
        accept = (request.headers.get("Accept") or "").lower()
        content_type = (request.content_type or "").lower()
        return (
            request.is_json
            or "application/json" in accept
            or "application/json" in content_type
            or request.args.get("format") == "json"
        )

    def fval(src, key, default=0.0):
        try:
            return float(src.get(key, default) or default)
        except Exception:
            return float(default)

    def ival(src, key, default=0):
        try:
            return int(src.get(key, default) or default)
        except Exception:
            return int(default)

    def make_payload(pg, year, month, internal_doc, market_doc):
        turnover = KPIEngine.calculate_turnover(
            float(internal_doc.get("internal_total") or 0),
            float(market_doc.get("market_total") or market_doc.get("total_turnover") or 0)
        )

        total_members = db.pg_members.count_documents({"pg_id": pg_obj_id})
        pct_in = KPIEngine.calculate_member_percentage(
            int(internal_doc.get("members_input_count") or 0),
            int(total_members or 0)
        )
        pct_out = KPIEngine.calculate_member_percentage(
            int(internal_doc.get("members_output_count") or 0),
            int(total_members or 0)
        )

        return {
            "success": True,
            "pg": {
                "id": str(pg["_id"]),
                "name": pg.get("name") or pg.get("pg_name") or "Producer Group",
            },
            "year": year,
            "month": month,
            "internal": {
                "input_sold_to_members_value": float(internal_doc.get("input_sold_to_members_value") or 0),
                "product_bought_from_members_value": float(internal_doc.get("product_bought_from_members_value") or 0),
                "other_internal_value": float(internal_doc.get("other_internal_value") or 0),
                "members_input_count": int(internal_doc.get("members_input_count") or 0),
                "members_output_count": int(internal_doc.get("members_output_count") or 0),
                "notes": internal_doc.get("notes") or "",
                "internal_total": float(internal_doc.get("internal_total") or 0),
            },
            "market": {
                "input_procured_from_market_value": float(market_doc.get("input_procured_from_market_value") or 0),
                "input_sold_to_outsiders_value": float(market_doc.get("input_sold_to_outsiders_value") or 0),
                "product_sold_to_market_value": float(market_doc.get("product_sold_to_market_value") or 0),
                "other_market_value": float(market_doc.get("other_market_value") or 0),
                "notes": market_doc.get("notes") or "",
                "market_total": float(market_doc.get("market_total") or 0),
                "total_turnover": float(turnover or 0),
            },
            "summary": {
                "turnover": float(turnover or 0),
                "total_members": int(total_members or 0),
                "pct_in": float(pct_in or 0),
                "pct_out": float(pct_out or 0),
            },
        }

    pg_obj_id = _to_object_id(pg_id)
    if not pg_obj_id:
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        if wants_json():
            return jsonify({"success": False, "message": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    now = datetime.utcnow()

    if request.method == "POST":
        write_denied = _guard_business_write(pg)
        if write_denied:
            return write_denied

        if request.is_json:
            src = request.get_json(silent=True) or {}
            year = ival(src, "year", now.year)
            month = ival(src, "month", now.month)
        else:
            src = request.form
            year = int(request.args.get("year") or request.form.get("year") or now.year)
            month = int(request.args.get("month") or request.form.get("month") or now.month)

        internal = {
            "pg_id": pg_obj_id,
            "year": year,
            "month": month,
            "input_sold_to_members_value": fval(src, "input_sold_to_members_value"),
            "product_bought_from_members_value": fval(src, "product_bought_from_members_value"),
            "other_internal_value": fval(src, "other_internal_value"),
            "members_input_count": ival(src, "members_input_count"),
            "members_output_count": ival(src, "members_output_count"),
            "notes": (src.get("internal_notes") or "").strip(),
            "updated_at": datetime.utcnow(),
        }
        internal["internal_total"] = round(
            internal["input_sold_to_members_value"]
            + internal["product_bought_from_members_value"]
            + internal["other_internal_value"],
            2,
        )

        db.pg_business_monthly.update_one(
            {"pg_id": pg_obj_id, "year": year, "month": month},
            {"$set": internal},
            upsert=True,
        )

        market = {
            "pg_id": pg_obj_id,
            "year": year,
            "month": month,
            "input_procured_from_market_value": fval(src, "input_procured_from_market_value"),
            "input_sold_to_outsiders_value": fval(src, "input_sold_to_outsiders_value"),
            "product_sold_to_market_value": fval(src, "product_sold_to_market_value"),
            "other_market_value": fval(src, "other_market_value"),
            "notes": (src.get("market_notes") or "").strip(),
            "updated_at": datetime.utcnow(),
        }
        market["market_total"] = round(
            market["input_procured_from_market_value"]
            + market["input_sold_to_outsiders_value"]
            + market["product_sold_to_market_value"]
            + market["other_market_value"],
            2,
        )
        market["total_turnover"] = KPIEngine.calculate_turnover(
            internal["internal_total"], market["market_total"]
        )

        db.pg_market_transactions.update_one(
            {"pg_id": pg_obj_id, "year": year, "month": month},
            {"$set": market},
            upsert=True,
        )

        try:
            from app.services.workflow import generate_mpr_snapshot
            user = {
                "user_id": session.get("user_id"),
                "username": session.get("username"),
                "role": session.get("role"),
            }
            generate_mpr_snapshot(db, level="pg", ref_id=str(pg_id), year=year, month=month, user=user)
        except Exception:
            pass

        internal_doc = db.pg_business_monthly.find_one(
            {"pg_id": pg_obj_id, "year": year, "month": month}
        ) or {}
        market_doc = db.pg_market_transactions.find_one(
            {"pg_id": pg_obj_id, "year": year, "month": month}
        ) or {}

        if wants_json():
            payload = make_payload(pg, year, month, internal_doc, market_doc)
            payload["message"] = "Monthly business saved successfully."
            return jsonify(payload), 200

        flash("Monthly business (6.1 + 6.2) saved. Turnover auto-calculated.", "success")
        return redirect(url_for("business.monthly_business", pg_id=pg_id, year=year, month=month))

    year = int(request.args.get("year") or now.year)
    month = int(request.args.get("month") or now.month)

    internal_doc = db.pg_business_monthly.find_one(
        {"pg_id": pg_obj_id, "year": year, "month": month}
    ) or {}
    market_doc = db.pg_market_transactions.find_one(
        {"pg_id": pg_obj_id, "year": year, "month": month}
    ) or {}

    if wants_json():
        return jsonify(make_payload(pg, year, month, internal_doc, market_doc)), 200

    turnover = KPIEngine.calculate_turnover(
        float(internal_doc.get("internal_total") or 0),
        float(market_doc.get("market_total") or market_doc.get("total_turnover") or 0)
    )
    total_members = db.pg_members.count_documents({"pg_id": pg_obj_id})
    pct_in = KPIEngine.calculate_member_percentage(
        int(internal_doc.get("members_input_count") or 0),
        int(total_members or 0)
    )
    pct_out = KPIEngine.calculate_member_percentage(
        int(internal_doc.get("members_output_count") or 0),
        int(total_members or 0)
    )

    return render_template(
        "business_monthly.html",
        pg=pg,
        year=year,
        month=month,
        internal=internal_doc,
        market=market_doc,
        turnover=turnover,
        total_members=total_members,
        pct_in=pct_in,
        pct_out=pct_out,
    )


# ============================================================
# NEW: YEARLY INCOME & EXPENDITURE STATEMENT (Manual: Year-end P&L)
# - Aggregates monthly pg_income_expenditure docs for a FY/Year
# ============================================================

# changes made by atlanta
@business_bp.route("/income_expenditure_year/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def income_expenditure_year(pg_id):
    db = current_app.mongo_db

    def wants_json():
        accept = (request.headers.get("Accept") or "").lower()
        content_type = (request.content_type or "").lower()
        requested_with = (request.headers.get("X-Requested-With") or "").lower()
        return (
            request.is_json
            or "application/json" in accept
            or "application/json" in content_type
            or request.args.get("format") == "json"
            or requested_with == "xmlhttprequest"
        )

    def serialize_month(doc):
        return {
            "id": str(doc.get("_id", "")),
            "year": int(doc.get("year") or 0),
            "month": int(doc.get("month") or 0),
            "income": doc.get("income", {}) or {},
            "expenditure": doc.get("expenditure", {}) or {},
            "total_income": float(doc.get("total_income") or 0),
            "total_expenditure": float(doc.get("total_expenditure") or 0),
            "excess_income_over_expenditure": float(doc.get("excess_income_over_expenditure") or 0),
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
        }

    try:
        pg_obj_id = ObjectId(pg_id)
    except InvalidId:
        if wants_json():
            return jsonify({"success": False, "message": "Invalid PG ID."}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = _get_scoped_pg(db, pg_obj_id)
    if not pg:
        if wants_json():
            return jsonify({"success": False, "message": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    now = datetime.utcnow()
    year = int(request.args.get("year") or now.year)

    rows = list(
        db.pg_income_expenditure.find(
            {"pg_id": pg_obj_id, "year": year}
        ).sort([("month", 1)])
    )

    total_income = 0.0
    total_expenditure = 0.0
    by_head_income = {}
    by_head_exp = {}

    for r in rows:
        inc = r.get("income") or {}
        exp = r.get("expenditure") or {}

        for k, v in inc.items():
            try:
                by_head_income[k] = by_head_income.get(k, 0.0) + float(v or 0)
            except Exception:
                pass

        for k, v in exp.items():
            try:
                by_head_exp[k] = by_head_exp.get(k, 0.0) + float(v or 0)
            except Exception:
                pass

        try:
            total_income += float(r.get("total_income") or sum(float(x or 0) for x in inc.values()))
        except Exception:
            pass

        try:
            total_expenditure += float(r.get("total_expenditure") or sum(float(x or 0) for x in exp.values()))
        except Exception:
            pass

    total_income = round(total_income, 2)
    total_expenditure = round(total_expenditure, 2)
    surplus = round(total_income - total_expenditure, 2)

    if wants_json():
        return jsonify({
            "success": True,
            "pg": {
                "id": str(pg["_id"]),
                "name": pg.get("name") or pg.get("pg_name") or "Producer Group",
            },
            "year": year,
            "months": [serialize_month(r) for r in rows],
            "by_head_income": by_head_income,
            "by_head_expenditure": by_head_exp,
            "total_income": total_income,
            "total_expenditure": total_expenditure,
            "surplus": surplus,
        }), 200

    return render_template(
        "income_expenditure_year.html",
        pg=pg,
        year=year,
        months=rows,
        by_head_income=by_head_income,
        by_head_expenditure=by_head_exp,
        total_income=total_income,
        total_expenditure=total_expenditure,
        surplus=surplus,
    )