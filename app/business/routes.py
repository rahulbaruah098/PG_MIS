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
@require_unlocked_period(scope='pg')
def stock_register(pg_id):
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

    def serialize_monthly(doc):
        return {
            "id": str(doc.get("_id", "")),
            "year": int(doc.get("year") or 0),
            "month": int(doc.get("month") or 0),
            "commodity": doc.get("commodity") or "",
            "opening_qty": float(doc.get("opening_qty") or 0),
            "opening_value": float(doc.get("opening_value") or 0),
            "closing_qty": float(doc.get("closing_qty") or 0),
            "closing_value": float(doc.get("closing_value") or 0),
            "valuation_method": doc.get("valuation_method") or "avg",
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
        }

    def serialize_movement(doc):
        return {
            "id": str(doc.get("_id", "")),
            "commodity": doc.get("commodity") or "",
            "movement_type": doc.get("movement_type") or "purchase",
            "qty": float(doc.get("qty") or 0),
            "rate": float(doc.get("rate") or 0),
            "amount": float(doc.get("amount") or 0),
            "remarks": doc.get("remarks") or "",
            "ts": doc.get("ts").isoformat() if doc.get("ts") else None,
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
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

    def build_payload(year=None, month=None):
        monthly_query = {"pg_id": pg_obj_id}
        if year is not None:
            monthly_query["year"] = int(year)
        if month is not None:
            monthly_query["month"] = int(month)

        records = list(
            db.pg_stocks_monthly.find(monthly_query)
            .sort([("year", -1), ("month", -1), ("updated_at", -1)])
            .limit(200)
        )

        movements = list(
            db.pg_stock_movements.find({"pg_id": pg_obj_id})
            .sort([("ts", -1)])
            .limit(100)
        )

        return {
            "success": True,
            "pg": {
                "id": str(pg["_id"]),
                "name": pg.get("pg_name") or pg.get("name") or "Producer Group",
            },
            "filters": {
                "year": int(year) if year is not None else None,
                "month": int(month) if month is not None else None,
            },
            "monthly_records": [serialize_monthly(r) for r in records],
            "movements": [serialize_movement(m) for m in movements],
        }

    if request.method == "POST":
        write_denied = _guard_business_write(pg)
        if write_denied:
            return write_denied

        src = request.get_json(silent=True) or {} if request.is_json else request.form

        year = ival(src, "year", datetime.utcnow().year)
        month = ival(src, "month", datetime.utcnow().month)
        commodity = (src.get("commodity") or "").strip()

        if not commodity:
            if wants_json():
                return jsonify({"success": False, "message": "Commodity is required."}), 400
            flash("Commodity is required.", "danger")
            return redirect(url_for("business.stock_register", pg_id=pg_id))

        doc = {
            "pg_id": pg_obj_id,
            "year": year,
            "month": month,
            "commodity": commodity,
            "opening_qty": fval(src, "opening_qty"),
            "opening_value": fval(src, "opening_value"),
            "closing_qty": fval(src, "closing_qty"),
            "closing_value": fval(src, "closing_value"),
            "valuation_method": (src.get("valuation_method") or "avg").strip().lower(),
            "updated_at": datetime.utcnow(),
        }

        db.pg_stocks_monthly.update_one(
            {"pg_id": pg_obj_id, "year": year, "month": month, "commodity": commodity},
            {"$set": doc},
            upsert=True,
        )

        if wants_json():
            payload = build_payload(year, month)
            payload["message"] = "Stock monthly saved successfully."
            return jsonify(payload), 200

        flash("Stock monthly saved.", "success")
        return redirect(url_for("business.stock_register", pg_id=pg_id))

    year = request.args.get("year")
    month = request.args.get("month")

    if wants_json():
        return jsonify(build_payload(year, month)), 200

    records = list(
        db.pg_stocks_monthly.find({"pg_id": pg_obj_id})
        .sort([("year", -1), ("month", -1)])
        .limit(200)
    )
    movements = list(
        db.pg_stock_movements.find({"pg_id": pg_obj_id})
        .sort([("ts", -1)])
        .limit(100)
    )
    return render_template("stock_register.html", pg=pg, records=records, movements=movements)

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