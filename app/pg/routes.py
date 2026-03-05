from services.audit_engine import AuditLogger
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from app.services.guards import require_unlocked_period
from flask import render_template, request, redirect, url_for, flash, current_app, session,jsonify, abort,g
from bson import ObjectId
from app.utils import safe_objectid
from datetime import datetime
from . import pg_bp
from ..rbac import login_required, roles_required
from ..services.workflow import ensure_pg_locked, create_change_request, add_notification, save_uploaded_document
from ..services.audit import log_audit


def _fmt_inr(amount):
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0
    # Format like ₹4,95,000 (no decimals for dashboard display)
    return "₹{:,.0f}".format(amt)


def _sum_cashbook_rows(rows):
    """
    Sums cashbook rows safely.
    Supports schemas where a row may have:
      - amount (cash)
      - bankAmount (bank)
    If you only use 'amount', bankAmount will just be 0.
    """
    total = 0.0
    for r in (rows or []):
        try:
            total += float(r.get("amount") or 0)
        except Exception:
            pass
        try:
            total += float(r.get("bankAmount") or 0)
        except Exception:
            pass
    return round(total, 2)



def _count_rows_from_register_doc(doc):
    """Best-effort row counter for generic register payloads."""
    if not doc:
        return 0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0
    # Common shapes: {rows:[...]}, {items:[...]}, {entries:[...]}
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return 0

def _sum_qty_from_rows(doc):
    """Best-effort quantity sum (kg) from rows having qty/quantity/weight_kg fields."""
    if not doc:
        return 0.0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0.0
    rows = None
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            rows = v
            break
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        for key in ("qty","quantity","weight","weight_kg","kg","stock_kg"):
            if key in r:
                try:
                    total += float(r.get(key) or 0)
                except Exception:
                    pass
                break
    return float(total)


def _pg_metrics(db, pg_id):
    """Compute dashboard metrics for a PG from existing MongoDB collections."""
    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        oid = None

    # If we can't resolve pg_id safely, return zeros so dashboard won't crash
    if not oid:
        return {
            'meetings_count': 0,
            'minutes_count': 0,
            'member_ledger_entries': 0,
            'assets_count': 0,
            'receipt_vouchers_count': 0,
            'input_rows_count': 0,
            'output_rows_count': 0,
            'total_stock_kg': 0.0,
        }

    # ---- PG Data Entry registers (counts + stock) ----
    meetings_count = 0
    minutes_count = 0
    member_ledger_entries = 0
    assets_count = 0
    receipt_vouchers_count = 0
    input_rows_count = 0
    output_rows_count = 0
    total_stock_kg = 0.0
    try:
        meetings_count = db.pg_meetings.count_documents({'pg_id': oid})
    except Exception:
        pass
    try:
        minutes_doc = db.pg_meeting_minutes.find_one({'pg_id': oid}, sort=[('updated_at', -1)])
        minutes_count = _count_rows_from_register_doc(minutes_doc) or (1 if minutes_doc else 0)
    except Exception:
        pass
    try:
        member_doc = db.pg_member_ledgers.find_one({'pg_id': oid}, sort=[('updated_at', -1)])
        member_ledger_entries = _count_rows_from_register_doc(member_doc)
    except Exception:
        pass
    try:
        asset_doc = db.pg_asset_registers.find_one({'pg_id': oid}, sort=[('updated_at', -1)])
        assets_count = _count_rows_from_register_doc(asset_doc)
    except Exception:
        pass
    try:
        receipt_vouchers_count = db.pg_receipt_vouchers.count_documents({'pg_id': oid})
    except Exception:
        pass
    try:
        input_doc = db.pg_input_registers.find_one({'pg_id': oid}, sort=[('updated_at', -1)])
        output_doc = db.pg_output_registers.find_one({'pg_id': oid}, sort=[('updated_at', -1)])
        input_rows_count = _count_rows_from_register_doc(input_doc)
        output_rows_count = _count_rows_from_register_doc(output_doc)
        total_stock_kg = max(0.0, _sum_qty_from_rows(input_doc) - _sum_qty_from_rows(output_doc))
    except Exception:
        pass
        return {
            "members_count": 0,
            "loans_count": 0,
            "outstanding_loans_amount": _fmt_inr(0),
            "categories_count": 0,
            "income_7d": _fmt_inr(0),
            "expense_7d": _fmt_inr(0),

            # ✅ Cash Book metrics
            "cashbook_receipts": _fmt_inr(0),
            "cashbook_payments": _fmt_inr(0),
            "cashbook_balance": _fmt_inr(0),
            "cashbook_period": {"year": None, "month": None},
        }

    members_count = db.pg_members.count_documents({"pg_id": oid})

    # Lakhpati Didi count (PG members flagged)
    lakhpati_count = 0
    try:
        lakhpati_count = db.pg_members.count_documents({"pg_id": oid, "lakh_pati_didi": True})
    except Exception:
        lakhpati_count = 0

    # Loans (legacy + new lifecycle)
    legacy_loans = list(db.pg_loans.find({"pg_id": oid}))
    new_pg_loans = list(db.pg_loan_accounts.find({"pg_id": oid}))
    new_mb_loans = list(db.pg_member_loan_accounts.find({"pg_id": oid}))

    loans_count = len(legacy_loans) + len(new_pg_loans) + len(new_mb_loans)
    outstanding = 0.0

    # legacy
    for ln in legacy_loans:
        for k in ("outstanding_amount", "outstanding", "balance", "amount"):
            if k in ln and ln.get(k) not in (None, ""):
                try:
                    outstanding += float(ln.get(k) or 0)
                except Exception:
                    pass

    # new lifecycle
    for ln in (new_pg_loans + new_mb_loans):
        try:
            outstanding += float(ln.get("outstanding_amount") or 0)
        except Exception:
            pass

    # Alerts (computed)
    try:
        from app.services.alerts import compute_pg_alerts
        alerts_count = len(compute_pg_alerts(db, str(oid)))
    except Exception:
        alerts_count = 0

    # Categories: try from PG profile if available, else 0
    pg_doc = db.pgs.find_one({"_id": oid}, {"categories": 1, "commodities": 1, "primary_activities": 1})
    categories_count = 0
    if pg_doc:
        for key in ("categories", "commodities", "primary_activities"):
            val = pg_doc.get(key)
            if isinstance(val, list):
                categories_count = max(categories_count, len(val))

    # Income/Expense last 7 days (if collection exists)
    income_7d = 0.0
    expense_7d = 0.0
    try:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=7)
        txns = list(db.pg_income_expenditure.find({"pg_id": oid, "created_at": {"$gte": since}}))
        for t in txns:
            amt = 0.0
            try:
                amt = float(t.get("amount") or 0)
            except Exception:
                amt = 0.0
            ttype = (t.get("type") or t.get("txn_type") or "").lower()
            if ttype in ("income", "receipt", "receipts", "credit"):
                income_7d += amt
            elif ttype in ("expense", "payment", "payments", "debit"):
                expense_7d += amt
    except Exception:
        pass

    # ------------------------------------------------------------
    # ✅ Cash Book (latest saved period from pg_cashbooks)
    # - Uses existing API storage schema: receipts[], payments[]
    # - Picks latest by (year desc, month desc, updated_at desc)
    # ------------------------------------------------------------
    cb_receipts = 0.0
    cb_payments = 0.0
    cb_balance = 0.0
    cb_year = None
    cb_month = None

    try:
        latest_cb = db.pg_cashbooks.find_one(
            {"pg_id": oid},
            sort=[("year", -1), ("month", -1), ("updated_at", -1)]
        )
        if latest_cb:
            cb_year = latest_cb.get("year")
            cb_month = latest_cb.get("month")
            cb_receipts = _sum_cashbook_rows(latest_cb.get("receipts"))
            cb_payments = _sum_cashbook_rows(latest_cb.get("payments"))
            cb_balance = round(cb_receipts - cb_payments, 2)
    except Exception:
        pass

    return {
        "members_count": members_count,
        "meetings_count": meetings_count,
        "minutes_count": minutes_count,
        "member_ledger_entries": member_ledger_entries,
        "assets_count": assets_count,
        "receipt_vouchers_count": receipt_vouchers_count,
        "input_rows_count": input_rows_count,
        "output_rows_count": output_rows_count,
        "total_stock_kg": f"{total_stock_kg:,.2f}",
        "loans_count": loans_count,
        "outstanding_loans_amount": _fmt_inr(outstanding),
        "categories_count": categories_count,
        "income_7d": _fmt_inr(income_7d),
        "expense_7d": _fmt_inr(expense_7d),

        # ✅ Cash Book metrics (latest period)
        "cashbook_receipts": _fmt_inr(cb_receipts),
        "cashbook_payments": _fmt_inr(cb_payments),
        "cashbook_balance": _fmt_inr(cb_balance),
        "cashbook_period": {"year": cb_year, "month": cb_month},

        # (optional) you can use this later if needed
        "alerts_count": alerts_count,
    }

def _set_pg_status(db, pg_id, status, extra=None):
    extra = extra or {}
    if status in ("active", "approved"):
        extra.setdefault("is_locked", True)
        extra.setdefault("approved_at", datetime.utcnow())
        extra.setdefault("approved_by", session.get("user_id"))
        extra.setdefault("approval_history", [])
    db.pgs.update_one(
        {"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))},
        {"$set": {"status": status, "updated_at": datetime.utcnow(), **extra}},
    )

def _current_user_dict():
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "role": session.get("role"),
    }



def _fmt_inr(amount):
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0
    # Format like ₹4,95,000 (no decimals for dashboard display)
    return "₹{:,.0f}".format(amt)


def _count_rows_from_register_doc(doc):
    """Best-effort row counter for generic register payloads."""
    if not doc:
        return 0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0
    # Common shapes: {rows:[...]}, {items:[...]}, {entries:[...]}
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return 0

def _sum_qty_from_rows(doc):
    """Best-effort quantity sum (kg) from rows having qty/quantity/weight_kg fields."""
    if not doc:
        return 0.0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0.0
    rows = None
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            rows = v
            break
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        for key in ("qty","quantity","weight","weight_kg","kg","stock_kg"):
            if key in r:
                try:
                    total += float(r.get(key) or 0)
                except Exception:
                    pass
                break
    return float(total)


def _pg_metrics(db, pg_id):
    """Compute dashboard metrics for a PG from existing MongoDB collections."""
    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        return {
            "members_count": 0,
            "lakhpati_count": 0,
            "loans_count": 0,
            "outstanding_loans_amount": _fmt_inr(0),
            "categories_count": 0,
            "income_7d": _fmt_inr(0),
            "expense_7d": _fmt_inr(0),
        }

    # ---- PG Data Entry registers (counts + stock) ----
    meetings_count = 0
    minutes_count = 0
    member_ledger_entries = 0
    assets_count = 0
    receipt_vouchers_count = 0
    input_rows_count = 0
    output_rows_count = 0
    total_stock_kg = 0.0

    try:
        meetings_count = db.pg_meetings.count_documents({"pg_id": oid})
    except Exception:
        pass

    try:
        minutes_doc = db.pg_meeting_minutes.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        minutes_count = _count_rows_from_register_doc(minutes_doc) or (1 if minutes_doc else 0)
    except Exception:
        pass

    try:
        member_doc = db.pg_member_ledgers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        member_ledger_entries = _count_rows_from_register_doc(member_doc)
    except Exception:
        pass

    try:
        asset_doc = db.pg_asset_registers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        assets_count = _count_rows_from_register_doc(asset_doc)
    except Exception:
        pass

    try:
        receipt_vouchers_count = db.pg_receipt_vouchers.count_documents({"pg_id": oid})
    except Exception:
        pass

    try:
        input_doc = db.pg_input_registers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        output_doc = db.pg_output_registers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        input_rows_count = _count_rows_from_register_doc(input_doc)
        output_rows_count = _count_rows_from_register_doc(output_doc)
        total_stock_kg = max(0.0, _sum_qty_from_rows(input_doc) - _sum_qty_from_rows(output_doc))
    except Exception:
        pass


    members_count = db.pg_members.count_documents({"pg_id": oid})

    # Lakhpati Didi count (PG members flagged)
    lakhpati_count = 0
    try:
        lakhpati_count = db.pg_members.count_documents({"pg_id": oid, "lakh_pati_didi": True})
    except Exception:
        lakhpati_count = 0

    # Loans (legacy + new lifecycle)
    legacy_loans = list(db.pg_loans.find({"pg_id": oid}))
    new_pg_loans = list(db.pg_loan_accounts.find({"pg_id": oid}))
    new_mb_loans = list(db.pg_member_loan_accounts.find({"pg_id": oid}))

    loans_count = len(legacy_loans) + len(new_pg_loans) + len(new_mb_loans)
    outstanding = 0.0
    # legacy
    for ln in legacy_loans:
        for k in ("outstanding_amount", "outstanding", "balance", "amount"):
            if k in ln and ln.get(k) not in (None, ""):
                try:
                    outstanding += float(ln.get(k) or 0)
                except Exception:
                    pass
    # new lifecycle
    for ln in (new_pg_loans + new_mb_loans):
        try:
            outstanding += float(ln.get("outstanding_amount") or 0)
        except Exception:
            pass

    # Alerts (computed)
    try:
        from app.services.alerts import compute_pg_alerts
        alerts_count = len(compute_pg_alerts(db, str(oid)))
    except Exception:
        alerts_count = 0


# Categories: try from PG profile if available, else 0
    pg_doc = db.pgs.find_one({"_id": oid}, {"categories": 1, "commodities": 1, "primary_activities": 1})
    categories_count = 0
    if pg_doc:
        for key in ("categories", "commodities", "primary_activities"):
            val = pg_doc.get(key)
            if isinstance(val, list):
                categories_count = max(categories_count, len(val))

    # Income/Expense last 7 days (if collection exists)
    income_7d = 0.0
    expense_7d = 0.0
    try:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=7)
        txns = list(db.pg_income_expenditure.find({"pg_id": oid, "created_at": {"$gte": since}}))
        for t in txns:
            amt = 0.0
            try:
                amt = float(t.get("amount") or 0)
            except Exception:
                amt = 0.0
            ttype = (t.get("type") or t.get("txn_type") or "").lower()
            if ttype in ("income", "receipt", "receipts", "credit"):
                income_7d += amt
            elif ttype in ("expense", "payment", "payments", "debit"):
                expense_7d += amt
    except Exception:
        pass

    # ---- Turnover (latest available month) ----
    turnover_latest = 0.0
    turnover_period = None
    try:
        latest = list(db.pg_market_transactions.find({"pg_id": oid}, {"year": 1, "month": 1}).sort([("year", -1), ("month", -1)]).limit(1))
        latest2 = list(db.pg_business_monthly.find({"pg_id": oid}, {"year": 1, "month": 1}).sort([("year", -1), ("month", -1)]).limit(1))
        cand = []
        if latest:
            cand.append((int(latest[0].get("year") or 0), int(latest[0].get("month") or 0)))
        if latest2:
            cand.append((int(latest2[0].get("year") or 0), int(latest2[0].get("month") or 0)))
        cand = [x for x in cand if x[0] and x[1]]
        if cand:
            y, m = sorted(cand, reverse=True)[0]
            from ..services.workflow import calc_turnover_and_stock
            metrics_tm = calc_turnover_and_stock(db, str(oid), y, m)
            turnover_latest = float(metrics_tm.get("total_turnover") or 0)
            turnover_period = f"{y}-{m:02d}"
    except Exception:
        pass

    # ---- Profit/Loss (latest available month) from income-expenditure ----
    profit_loss_latest = 0.0
    profit_period = None
    try:
        ie = db.pg_income_expenditure.find_one({"pg_id": oid}, sort=[("year", -1), ("month", -1)])
        if ie:
            profit_loss_latest = float(ie.get("excess_income_over_expenditure") or 0)
            profit_period = f"{int(ie.get('year') or 0)}-{int(ie.get('month') or 0):02d}" if ie.get("year") and ie.get("month") else None
    except Exception:
        pass

    profit_latest = max(profit_loss_latest, 0.0)
    loss_latest = abs(min(profit_loss_latest, 0.0))

    # ---- Grants ----
    grants_count = 0
    grants_received_total = 0.0
    try:
        grants = list(db.pg_grants.find({"pg_id": oid}, {"amount_received": 1}))
        grants_count = len(grants)
        grants_received_total = round(sum(float(g.get("amount_received") or 0) for g in grants), 2)
    except Exception:
        pass

    return {
        "members_count": members_count,
        "meetings_count": meetings_count,
        "minutes_count": minutes_count,
        "member_ledger_entries": member_ledger_entries,
        "assets_count": assets_count,
        "receipt_vouchers_count": receipt_vouchers_count,
        "input_rows_count": input_rows_count,
        "output_rows_count": output_rows_count,
        "total_stock_kg": f"{total_stock_kg:,.2f}",
        "lakhpati_count": lakhpati_count,
        "loans_count": loans_count,
        "outstanding_loans_amount": _fmt_inr(outstanding),
        "categories_count": categories_count,
        "income_7d": _fmt_inr(income_7d),
        "expense_7d": _fmt_inr(expense_7d),

        # Turnover + Profit/Loss (latest month)
        "turnover_latest": _fmt_inr(turnover_latest),
        "turnover_period": turnover_period,
        "profit_latest": _fmt_inr(profit_latest),
        "loss_latest": _fmt_inr(loss_latest),
        "profit_loss_latest": _fmt_inr(profit_loss_latest),
        "profit_period": profit_period,

        # Grants
        "has_grants": True if grants_count else False,
        "grants_count": grants_count,
        "grants_received_total": _fmt_inr(grants_received_total),
    }

@pg_bp.route("/home")
@login_required
def pg_home():
    db = current_app.mongo_db
    pg_id = session.get("pg_id")
    role = session.get("role")

    if role == "PG_DATA_ENTRY" and pg_id:
        pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
        metrics = _pg_metrics(db, pg_id)
        return render_template("dashboard_pg.html", pg=pg_doc, metrics=metrics)

    return redirect(url_for("reports.hierarchy_dashboard"))



@pg_bp.route("/profile")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "SUPER_ADMIN")
def pg_profile():
    db = current_app.mongo_db

    # If PG user, load their own PG
    pg_id = session.get("pg_id")
    pg_doc = None
    if pg_id:
        try:
            pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
        except Exception:
            pg_doc = None

    # Use your existing template name here:
    # - if your file is profile.html -> keep "profile.html"
    # - if your file is pg_profile.html -> change it
    return render_template("profile.html", pg=pg_doc)


@pg_bp.route("/view/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_view(pg_id):
    """Open a PG dashboard by id.

    - PG_DATA_ENTRY users can only open their own PG.
    - Other roles can open PGs within their jurisdiction.
    """
    db = current_app.mongo_db
    role = session.get("role")

    pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg_doc:
        flash("PG not found.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if role == "PG_DATA_ENTRY":
        if session.get("pg_id") != pg_id:
            flash("You cannot access this PG.", "danger")
            return redirect(url_for("pg.pg_home"))
        metrics = _pg_metrics(db, pg_id)
        return render_template("dashboard_pg.html", pg=pg_doc, metrics=metrics)

    # Scope check for other roles
    if session.get("clf_id") and str(pg_doc.get("clf_id")) != session.get("clf_id"):
        flash("This PG is not under your CLF.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))
    if session.get("block_id") and str(pg_doc.get("block_id")) != session.get("block_id"):
        flash("This PG is not under your Block.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))
    if session.get("district_id") and str(pg_doc.get("district_id")) != session.get("district_id"):
        flash("This PG is not under your District.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))
    if session.get("state_id") and str(pg_doc.get("state_id")) != session.get("state_id"):
        flash("This PG is not under your State.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    metrics = _pg_metrics(db, pg_id)
    return render_template("dashboard_pg.html", pg=pg_doc, metrics=metrics)


@pg_bp.route("/registration/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_registration(pg_id):

    db = current_app.mongo_db

    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    role = session.get("role")
    session_pg_id = session.get("pg_id")

    if role == "PG_DATA_ENTRY" and session_pg_id != pg_id:
        flash("You cannot edit this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    # ===============================
    # GET — Load Members by Village
    # ===============================
    if request.method == "GET":

        shg_members = list(db.shg_members_master.find(
            {"Village": pg.get("Village")}
        ))

        # ✅ Convert ObjectId inside pg.members → string
        safe_members = []

        if pg.get("members"):
            for m in pg["members"]:
                safe_members.append({
                    "member_id": str(m.get("member_id")),
                    "member_name": m.get("member_name"),
                    "shg_name": m.get("shg_name"),
                    "shg_code": m.get("shg_code"),
                    "role": m.get("role")
                })

        return render_template(
            "pg_registration.html",
            pg=pg,
            shg_members=shg_members,
            safe_members=safe_members
        )


    # ===============================
    # POST
    # ===============================

    # ===============================
    # REQUIRED FIELD VALIDATION
    # ===============================

    required_fields = {
        "PG Type": request.form.get("pg_type"),
        "Sector": request.form.get("sector"),
        "Formation Date": request.form.get("formation_date"),
        "Contact Number": request.form.get("contact_number"),
        "Bank Name": request.form.get("bank_name"),
        "Branch": request.form.get("branch"),
        "Account Number": request.form.get("account_number"),
        "IFSC": request.form.get("ifsc"),
        "Aggregation Centre Name": request.form.get("agg_centre_name"),
        "Aggregation Centre Address": request.form.get("agg_centre_address"),
    }

    for field_name, value in required_fields.items():
        if not value or str(value).strip() == "":
            flash(f"{field_name} is required.", "danger")
            return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # Contact number numeric validation
    if not request.form.get("contact_number").isdigit():
        flash("Contact number must be numeric.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    member_ids = request.form.getlist("member_ids[]")
    president_id = request.form.get("president_id")
    secretary_id = request.form.get("secretary_id")
    cashier_id = request.form.get("cashier_id")

    # ---- Basic validation first
    if not member_ids:
        flash("Please select at least one member.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # Prevent duplicates
    if len(member_ids) != len(set(member_ids)):
        flash("Duplicate members detected.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # Convert to ObjectId safely
    try:
        member_object_ids = [ObjectId(mid) for mid in member_ids]
    except:
        flash("Invalid member selection.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # Limit check
    if len(member_object_ids) > 40:
        flash("Maximum 40 members allowed.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # ===============================
    # Fetch Members (Village-level filter enforced in DB)
    # ===============================
    members_from_db = list(db.shg_members_master.find(
        {
            "_id": {"$in": member_object_ids},
            "Village": pg.get("Village")
        }
    ))

    if len(members_from_db) != len(member_object_ids):
        flash("Some members are invalid or do not belong to this village.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # ===============================
    # Role validation
    # ===============================
    if not president_id or not secretary_id or not cashier_id:
        flash("Please select President, Secretary and Cashier.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    if president_id not in member_ids or \
       secretary_id not in member_ids or \
       cashier_id not in member_ids:
        flash("Office bearers must be selected from chosen members.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    if len({president_id, secretary_id, cashier_id}) != 3:
        flash("President, Secretary and Cashier must be different members.", "danger")
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    # ===============================
    # Build Members Array
    # ===============================
    members_array = []

    for m in members_from_db:

        role_name = "member"

        if str(m["_id"]) == president_id:
            role_name = "president"
        elif str(m["_id"]) == secretary_id:
            role_name = "secretary"
        elif str(m["_id"]) == cashier_id:
            role_name = "cashier"

        members_array.append({
            "member_id": m["_id"],
            "member_name": m.get("Member Name"),
            "shg_name": m.get("SHG Name"),
            "shg_code": m.get("SHG Code"),
            "role": role_name
        })

    # ===============================
    # Prepare Update Data
    # ===============================
    data = {
        "pg_type": request.form.get("pg_type"),
        "sector": request.form.get("sector"),
        "formation_date": request.form.get("formation_date"),
        "office_bearers":{
                "president_id": ObjectId(president_id),
                "secretary_id": ObjectId(secretary_id),
                "cashier_id": ObjectId(cashier_id),
                "contact_number": request.form.get("contact_number"),
        },

        "bank_details": {
            "bank_name": request.form.get("bank_name"),
            "branch": request.form.get("branch"),
            "account_number": request.form.get("account_number"),
            "ifsc": request.form.get("ifsc"),
        },
        "aggregation_centre": {
            "name": request.form.get("agg_centre_name"),
            "address": request.form.get("agg_centre_address"),
        },
        "total_members": len(members_array),
        "members": members_array,
        "updated_at": datetime.utcnow(),
    }

    # ===============================
    # Lock Handling
    # ===============================
    if ensure_pg_locked(pg):

        create_change_request(
            db,
            collection="pgs",
            doc_id=pg["_id"],
            proposed_changes=data,
            user=_current_user_dict(),
            reason="PG is locked after approval; update submitted for approval.",
        )

        add_notification(
            db,
            to_role="ADMIN",
            title="PG update pending approval",
            body=f'PG "{pg.get("name")}" has an update request pending approval.',
            link=url_for("pg.pg_view", pg_id=str(pg["_id"])),
        )

        flash("PG is already approved/locked. Changes submitted for approval.", "info")
        return redirect(url_for("pg.pg_view", pg_id=pg_id))

    # ===============================
    # Save
    # ===============================
    db.pgs.update_one(
        {"_id": pg["_id"]},
        {"$set": data}
    )

    log_audit(
        db,
        action="update",
        collection="pgs",
        doc_id=pg["_id"],
        user=_current_user_dict(),
        after=data
    )

    flash("PG registration details updated.", "success")
    return redirect(url_for("pg.pg_registration", pg_id=pg_id))


@pg_bp.route("/submit/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN")
@require_unlocked_period(scope='pg')
def pg_submit_for_authorization(pg_id):
    """After data entry, submit PG for state authorization."""
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    role = session.get("role")
    if role == "PG_DATA_ENTRY" and session.get("pg_id") != pg_id:
        flash("You cannot submit this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    _set_pg_status(db, pg_id, "submitted", {"submitted_at": datetime.utcnow(), "submitted_by": session.get("user_id")})
    flash("PG submitted for State Authorization.", "success")
    return redirect(url_for("pg.pg_registration", pg_id=pg_id))


@pg_bp.route("/authorization", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_authorization():
    """State Authorization step.

    ADMIN (State Admin) sees only PGs under their state.
    SUPER_ADMIN can see all.
    """
    db = current_app.mongo_db
    state_id = session.get("state_id")

    if request.method == "POST":
        pg_id = request.form.get("pg_id")
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip()
        if not pg_id or action not in ("approve", "reject"):
            flash("Invalid request.", "danger")
            return redirect(url_for("pg.pg_authorization"))

        extra = {"authorized_at": datetime.utcnow(), "authorized_by": session.get("user_id")}
        if action == "approve":
            _set_pg_status(db, pg_id, "active", extra)
            flash("PG authorized and activated.", "success")
        else:
            _set_pg_status(db, pg_id, "rejected", {**extra, "rejection_reason": reason or None})
            flash("PG rejected.", "warning")

        return redirect(url_for("pg.pg_authorization"))

    q = {"status": "submitted"}
    if state_id and session.get("role") == "ADMIN":
        q["state_id"] = ObjectId(state_id)
    pgs = list(db.pgs.find(q).sort("submitted_at", -1))
    return render_template("pg_authorization.html", pgs=pgs)


# PG Members Management in membership register

@pg_bp.route("/members/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_members(pg_id):
    db = current_app.mongo_db

    try:
        pg_obj_id = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # Members selected during PG Registration (stored inside pgs.members)
    selected = pg.get("members") or []
    selected_ids = []

    for m in selected:
        mid = m.get("member_id")
        if not mid:
            continue
        try:
            selected_ids.append(ObjectId(mid) if not isinstance(mid, ObjectId) else mid)
        except Exception:
            pass

    # Load master records for these members
    master_by_id = {}
    if selected_ids:
        for doc in db.shg_members_master.find({"_id": {"$in": selected_ids}}):
            master_by_id[str(doc["_id"])] = doc

    # Load already-saved PG member documents (pg_members collection)
    existing_by_member = {}
    for doc in db.pg_members.find({"pg_id": pg_obj_id}):
        key = str(doc.get("member_id") or doc.get("_id"))
        existing_by_member[key] = doc

    def normalize_key(k: str) -> str:
        """Normalize field names so we can match variants safely."""
        if not isinstance(k, str):
            return ""
        return (
            k.strip()
             .lower()
             .replace("_", " ")
             .replace("-", " ")
             .replace(".", "")
        )

    def pick(master_doc, *keys):
        """
        Return first non-empty value among possible master field names.
        Also supports fuzzy match for slash-based keys like:
        'Father/Mother/Spouse Name'
        """
        if not master_doc:
            return None

        # 1) Exact keys first
        for k in keys:
            if k in master_doc:
                v = master_doc.get(k)
                if v not in (None, "", []):
                    return v

        # 2) Normalized direct match (handles small differences)
        norm_map = {}
        for mk, mv in master_doc.items():
            nk = normalize_key(mk)
            if nk and nk not in norm_map:
                norm_map[nk] = mv

        for k in keys:
            nk = normalize_key(k)
            if nk in norm_map:
                v = norm_map.get(nk)
                if v not in (None, "", []):
                    return v

        # 3) Fuzzy match for composite/slash keys
        # Example: searching spouse should match "father/mother/spouse name"
        for mk, mv in master_doc.items():
            if mv in (None, "", []):
                continue
            mkn = normalize_key(mk)
            # spouse/father/mother combos
            if any(normalize_key(x) in mkn for x in keys):
                return mv

        return None

    # ============================================================
    # ✅ INDIVIDUAL SAVE (Row-wise)
    # ============================================================
    if request.method == "POST":
        mid = (request.form.get("member_id") or "").strip()
        if not mid:
            flash("Member ID missing.", "danger")
            return redirect(url_for("pg.pg_members", pg_id=pg_id))

        try:
            mid_obj = ObjectId(mid)
        except Exception:
            flash("Invalid Member ID.", "danger")
            return redirect(url_for("pg.pg_members", pg_id=pg_id))

        master = master_by_id.get(mid)

        # Read single row fields
        contact = (request.form.get("contact") or "").strip()
        photo_id_number = (request.form.get("photo_id_number") or "").strip()
        bank_name = (request.form.get("bank_name") or "").strip()
        branch = (request.form.get("branch") or "").strip()
        account_number = (request.form.get("account_number") or "").strip()
        membership_fee_raw = (request.form.get("membership_fee_paid") or "").strip()
        lakh_raw = (request.form.get("lakh_pati_didi") or "").strip().lower()

        # Safe fee parse
        membership_fee_paid = None
        if membership_fee_raw != "":
            try:
                membership_fee_paid = float(membership_fee_raw)
            except Exception:
                membership_fee_paid = None

        # Existing doc (to prevent overwriting with empty)
        existing_doc = db.pg_members.find_one({"pg_id": pg_obj_id, "member_id": mid_obj}) or {}

        # Always store master snapshot fields (keeps consistent and future-proof)
        update_set = {
            "pg_id": pg_obj_id,
            "member_id": mid_obj,

            "name": pick(master, "Member Name", "Name", "Member_Name"),
            # ✅ FIX: includes your real key "Father/Mother/Spouse Name"
            "spouse_name": pick(
                master,
                "Father/Mother/Spouse Name",
                "Spouse Name",
                "Spouse/Husband Name",
                "Husband Name",
                "Father/Husband Name",
                "Father Name",
                "Mother Name"
            ),
            "category": pick(master, "Category", "Caste Category", "Social Category"),
            "shg_name": pick(master, "SHG Name", "SHG_Name"),
            "shg_code": pick(master, "SHG Code", "SHG_Code"),

            "updated_at": datetime.utcnow(),
        }

        # ✅ Do NOT overwrite with blank:
        # Only set when user typed something. Otherwise keep previous/master fallback.
        if contact != "":
            update_set["contact"] = contact
        elif not existing_doc.get("contact"):
            update_set["contact"] = pick(master, "Contact Number", "Mobile", "Mobile No", "Phone")

        if photo_id_number != "":
            update_set["photo_id_number"] = photo_id_number
        elif not existing_doc.get("photo_id_number"):
            update_set["photo_id_number"] = pick(
                master,
                "AADHAR/Voter/Govt ID No",
                "Aadhaar",
                "Aadhaar No",
                "Voter ID",
                "Govt ID No"
            )

        if bank_name != "":
            update_set["bank_name"] = bank_name
        # keep existing if blank

        if branch != "":
            update_set["branch"] = branch

        if account_number != "":
            update_set["account_number"] = account_number

        if membership_fee_paid is not None:
            update_set["membership_fee_paid"] = membership_fee_paid

        # ✅ Lakhpati Didi flag
        # Checkbox sends: on/1/true. If absent => False.
        update_set["lakh_pati_didi"] = True if lakh_raw in ("1", "true", "on", "yes") else False

        db.pg_members.update_one(
            {"pg_id": pg_obj_id, "member_id": mid_obj},
            {"$set": update_set, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True
        )

        flash("Member details saved successfully.", "success")
        return redirect(url_for("pg.pg_members", pg_id=pg_id))

    # ============================================================
    # Build rows in the same order as PG Registration
    # ============================================================
    rows = []
    for m in selected:
        mid = m.get("member_id")
        if not mid:
            continue

        mid_str = str(mid)
        master = master_by_id.get(mid_str)
        existing = existing_by_member.get(mid_str) or {}

        spouse_val = (
            existing.get("spouse_name")
            or pick(
                master,
                "Father/Mother/Spouse Name",
                "Spouse Name",
                "Spouse/Husband Name",
                "Husband Name",
                "Father/Husband Name",
                "Father Name",
                "Mother Name"
            )
        )

        rows.append({
            "member_id": mid_str,
            "name": existing.get("name") or pick(master, "Member Name", "Name", "Member_Name") or m.get("member_name"),
            "spouse_name": spouse_val,
            "category": existing.get("category") or pick(master, "Category", "Caste Category", "Social Category"),
            "shg_name": existing.get("shg_name") or pick(master, "SHG Name", "SHG_Name") or m.get("shg_name"),
            "contact": existing.get("contact") or pick(master, "Contact Number", "Mobile", "Mobile No", "Phone"),
            "photo_id_number": existing.get("photo_id_number") or pick(
                master,
                "AADHAR/Voter/Govt ID No",
                "Aadhaar",
                "Aadhaar No",
                "Voter ID",
                "Govt ID No"
            ),
            "bank_name": existing.get("bank_name") or "",
            "branch": existing.get("branch") or "",
            "account_number": existing.get("account_number") or "",
            "membership_fee_paid": existing.get("membership_fee_paid") if existing.get("membership_fee_paid") is not None else "",
            "lakh_pati_didi": True if existing.get("lakh_pati_didi") else False,
        })

    return render_template("pg_members.html", pg=pg, rows=rows)


@pg_bp.route("/lakhpati/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_lakhpati(pg_id):
    """List members marked as Lakhpati Didi for a PG."""
    db = current_app.mongo_db
    # Defensive: links can be generated with pg_id=None if session scope is missing.
    if not pg_id or str(pg_id).lower() == "none":
        pg_id = session.get("pg_id") or session.get("active_pg_id")

    if not pg_id or not ObjectId.is_valid(str(pg_id)):
        flash("PG scope not found. Please open a PG first or re-login.", "warning")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": ObjectId(str(pg_id))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # Scope restriction for PG user
    if session.get("role") == "PG_DATA_ENTRY" and str(session.get("pg_id")) != str(pg_id):
        flash("You cannot access this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    members = list(db.pg_members.find({"pg_id": ObjectId(str(pg_id)), "lakh_pati_didi": True}).sort([("name", 1)]))
    return render_template("lakhpati_didi.html", pg=pg, members=members)


@pg_bp.route("/export/<pg_id>/summary.csv")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def export_pg_summary_csv(pg_id):
    """Download PG summary (members + key KPIs) as CSV."""
    import csv
    from io import StringIO
    db = current_app.mongo_db
    if not pg_id or str(pg_id).lower() == "none" or not ObjectId.is_valid(str(pg_id)):
        abort(400)

    pg = db.pgs.find_one({"_id": ObjectId(str(pg_id))})
    if not pg:
        abort(404)

    if session.get("role") == "PG_DATA_ENTRY" and str(session.get("pg_id")) != str(pg_id):
        abort(403)

    # KPI snapshot
    kpi = _pg_metrics(db, pg_id)

    # Members
    mems = list(db.pg_members.find({"pg_id": ObjectId(str(pg_id))}).sort([("name", 1)]))

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["PG_ID", "PG_NAME", "TURNOVER_LATEST", "PROFIT_LOSS_LATEST", "GRANTS_RECEIVED", "LAKHPATI_DIDI_COUNT"])
    w.writerow([
        str(pg.get("_id")),
        pg.get("name") or "",
        kpi.get("turnover_latest") or "₹0",
        kpi.get("profit_latest") or "₹0",
        kpi.get("grants_received_total") or "₹0",
        kpi.get("lakhpati_count") or 0,
    ])
    w.writerow([])
    w.writerow(["MEMBER_NAME", "SPOUSE_NAME", "CATEGORY", "SHG_NAME", "CONTACT", "PHOTO_ID", "BANK", "BRANCH", "ACCOUNT", "FEE_PAID", "LAKHPATI_DIDI"])
    for m in mems:
        w.writerow([
            m.get("name") or "",
            m.get("spouse_name") or "",
            m.get("category") or "",
            m.get("shg_name") or "",
            m.get("contact") or "",
            m.get("photo_id_number") or "",
            m.get("bank_name") or "",
            m.get("branch") or "",
            m.get("account_number") or "",
            "Yes" if m.get("membership_fee_paid") else "No",
            "Yes" if m.get("lakh_pati_didi") else "No",
        ])

    from flask import Response
    filename = f"pg_{pg_id}_summary.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@pg_bp.route("/submit/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN")
@require_unlocked_period(scope='pg')
def submit_for_authorization(pg_id):
    """Submit a PG for state authorization.

    Workflow alignment:
    - CLF/PG user completes registration.
    - Then submits to the State Admin for authorization.
    """
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    # PG_DATA_ENTRY can only submit their own PG.
    if session.get("role") == "PG_DATA_ENTRY" and session.get("pg_id") != pg_id:
        flash("You cannot submit this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    _set_pg_status(db, pg_id, "submitted", {
        "submitted_at": datetime.utcnow(),
        "submitted_by": session.get("user_id"),
    })
    flash("Submitted to State for authorization.", "success")
    return redirect(url_for("pg.pg_registration", pg_id=pg_id))


@pg_bp.route("/authorization", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
@require_unlocked_period(scope='pg')
def state_authorization():
    """State Admin: approve/reject PG registrations."""
    db = current_app.mongo_db
    state_id = session.get("state_id")
    if not state_id:
        flash("No state scope found for your account.", "danger")
        return redirect(url_for("reports.state_dashboard"))

    if request.method == "POST":
        pg_id = request.form.get("pg_id")
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip() or None
        pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "state_id": ObjectId(state_id)})
        if not pg:
            flash("PG not found in your state.", "danger")
            return redirect(url_for("pg.state_authorization"))

        if action == "approve":
            _set_pg_status(db, pg_id, "active", {
                "authorized_at": datetime.utcnow(),
                "authorized_by": session.get("user_id"),
                "rejection_reason": None,
            })
            flash("PG authorized and activated.", "success")
        elif action == "reject":
            _set_pg_status(db, pg_id, "rejected", {
                "authorized_at": datetime.utcnow(),
                "authorized_by": session.get("user_id"),
                "rejection_reason": reason,
            })
            flash("PG rejected.", "warning")
        else:
            flash("Invalid action.", "danger")

        return redirect(url_for("pg.state_authorization"))

    submitted = list(db.pgs.find({
        "state_id": ObjectId(state_id),
        "status": {"$in": ["submitted", "rejected", "active", "draft"]},
    }).sort([("updated_at", -1)]).limit(300))

    return render_template("pg_authorization.html", pgs=submitted)


@pg_bp.route("/upload_document/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "CRCTA", "ANS", "CRCITARD")
@require_unlocked_period(scope='pg')
def pg_upload_document(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        f = request.files.get("document")
        doc_type = request.form.get("doc_type") or "registration_form"
        if not f or not f.filename:
            flash("Please choose a file.", "warning")
            return redirect(url_for("pg.pg_upload_document", pg_id=pg_id))

        doc = save_uploaded_document(current_app, pg_id=pg_id, file_storage=f, doc_type=doc_type, user=_current_user_dict())
        flash("Document uploaded successfully.", "success")
        return redirect(url_for("pg.pg_upload_document", pg_id=pg_id))

    docs = list(db.pg_documents.find({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}).sort("uploaded_at", -1))
    return render_template("pg_upload_document.html", pg=pg, docs=docs)

# ----------------------------
# PG Registers (UI menu pages)
# ----------------------------

@pg_bp.route('/registers/meeting-minutes')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def meeting_minute_book():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('meeting_minute_book.html', pg_id=pg_id)

@pg_bp.route('/registers/member-ledger')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def member_ledger():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('member_ledger.html', pg_id=pg_id)

@pg_bp.route('/registers/loan-ledger')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def loan_ledger():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('loan_ledger.html', pg_id=pg_id)

@pg_bp.route('/registers/receipt-voucher')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def receipt_voucher():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('receipt_voucher.html', pg_id=pg_id)

@pg_bp.route('/registers/input')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def input_register():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('input_register.html', pg_id=pg_id)

@pg_bp.route('/registers/asset')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def asset_register():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('asset_register.html', pg_id=pg_id)

@pg_bp.route('/registers/ledger-book')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def ledger_book():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('ledger_book.html', pg_id=pg_id)

@pg_bp.route('/registers/output')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def output_register():
    pg_id = session.get('pg_id') or session.get('active_pg_id')
    return render_template('output_register.html', pg_id=pg_id)



# ----------------------------
# PG Registers — DATA APIs (MongoDB + Audit)
# These APIs power the "templates only" register pages so data is persisted
# and higher roles can view PG activity (scope-respecting).
# ----------------------------

def _ctx_pg_id():
    """Return the active PG context.
    PG user => session['pg_id']
    Higher roles => session['active_pg_id'] (set via /pg/set_active/<pg_id>)
    """
    return session.get("pg_id") or session.get("active_pg_id") or request.args.get("pg_id") or request.form.get("pg_id")


def _enforce_pg_scope(pg_doc):
    """Abort with 403 if current user is not allowed to access this PG document."""
    role = session.get("role")

    # PG user can only see own PG
    if role == "PG_DATA_ENTRY":
        if str(pg_doc.get("_id")) != str(session.get("pg_id")):
            abort(403)

    # Hierarchy scoping for other roles (if ids exist in session)
    if session.get("clf_id") and str(pg_doc.get("clf_id")) != str(session.get("clf_id")):
        abort(403)
    if session.get("block_id") and str(pg_doc.get("block_id")) != str(session.get("block_id")):
        abort(403)
    if session.get("district_id") and str(pg_doc.get("district_id")) != str(session.get("district_id")):
        abort(403)
    if session.get("state_id") and str(pg_doc.get("state_id")) != str(session.get("state_id")):
        abort(403)


def _load_pg_or_404(db, pg_id):
    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        abort(400, "Invalid PG ID")
    pg_doc = db.pgs.find_one({"_id": oid})
    if not pg_doc:
        abort(404, "PG not found")
    _enforce_pg_scope(pg_doc)
    return pg_doc


def _upsert_pg_period_doc(db, *, collection: str, pg_id: str, year: int | None, month: int | None, payload: dict, user: dict):
    """Store a single period document for a PG. Creates or updates with audit logging."""
    q = {"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}
    if year is not None:
        q["year"] = int(year)
    if month is not None:
        q["month"] = int(month)

    before = db[collection].find_one(q)
    now = datetime.utcnow()

    doc = dict(payload or {})
    doc.update({
        "pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))),
        "year": int(year) if year is not None else None,
        "month": int(month) if month is not None else None,
        "updated_at": now,
    })
    if not before:
        doc["created_at"] = now
        res = db[collection].insert_one(doc)
        log_audit(db, action=f"{collection}:create", collection=collection, doc_id=res.inserted_id, user=user, after=doc, meta={"pg_id": pg_id, "year": year, "month": month})
        return res.inserted_id
    else:
        db[collection].update_one({"_id": before["_id"]}, {"$set": doc})
        log_audit(db, action=f"{collection}:update", collection=collection, doc_id=before["_id"], user=user, before=before, after=doc, meta={"pg_id": pg_id, "year": year, "month": month})
        return before["_id"]


def _get_period_from_args():
    year = request.args.get("year") or request.form.get("year")
    month = request.args.get("month") or request.form.get("month")
    try:
        year = int(year) if year not in (None, "", "null") else None
    except Exception:
        year = None
    try:
        month = int(month) if month not in (None, "", "null") else None
    except Exception:
        month = None
    return year, month


# ---------- Cash Book (used by finance.cashbook template) ----------
@pg_bp.route("/api/cashbook/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_cashbook(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()
    coll = "pg_cashbooks"

    if request.method == "GET":
        doc = db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "year": year, "month": month}) if (year and month) else db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}, sort=[("updated_at", -1)])
        if not doc:
            return jsonify({"receipts": [], "payments": [], "year": year, "month": month})
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return jsonify(doc)

    # POST (only PG should write)
    if session.get("role") != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}

    # ✅ Validation: Cash Book must balance before saving
    # If receipts grand total != payments grand total, do not accept/save.
    receipts = payload.get("receipts", [])
    payments = payload.get("payments", [])
    r_total = _sum_cashbook_rows(receipts)
    p_total = _sum_cashbook_rows(payments)
    if round(r_total, 2) != round(p_total, 2):
        return jsonify({
            "ok": False,
            "error": "Cash Book cannot be saved because Receipt total and Payment total are not equal.",
            "receipt_total": r_total,
            "payment_total": p_total,
        }), 400

    _upsert_pg_period_doc(db, collection=coll, pg_id=pg_id, year=year, month=month, payload={
        "receipts": receipts,
        "payments": payments,
        "meta": payload.get("meta", {}),
    }, user=_current_user_dict())
    return jsonify({"ok": True, "message": "Saved successfully"})



# ---------- Ledger Book ----------
@pg_bp.route("/ledger/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_ledger_book(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()
    coll = "pg_ledger_books"

    if request.method == "GET":
        doc = db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "year": year, "month": month}) if (year and month) else db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}, sort=[("updated_at", -1)])
        if not doc:
            return jsonify({"entries": [], "year": year, "month": month})
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return jsonify(doc)

    if session.get("role") != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}
    _upsert_pg_period_doc(db, collection=coll, pg_id=pg_id, year=year, month=month, payload={
        "entries": payload.get("entries", []),
        "meta": payload.get("meta", {}),
    }, user=_current_user_dict())
    return jsonify({"ok": True, "message": "Saved successfully"})


# ---------- Loan Ledger (stored per PG + loan_id) ----------
@pg_bp.route("/loan-ledger/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_loan_ledger(loan_id):
    db = current_app.mongo_db
    pg_id = _ctx_pg_id()
    if not pg_id:
        abort(400, "Missing PG context")
    _load_pg_or_404(db, pg_id)

    coll = "pg_loan_ledgers"
    year, month = _get_period_from_args()
    q = {"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "loan_id": str(loan_id)}
    if year is not None: q["year"] = year
    if month is not None: q["month"] = month

    if request.method == "GET":
        doc = db[coll].find_one(q) or db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "loan_id": str(loan_id)}, sort=[("updated_at", -1)])
        if not doc:
            return jsonify({"loan_id": str(loan_id), "entries": [], "year": year, "month": month})
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return jsonify(doc)

    if session.get("role") != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}
    before = db[coll].find_one(q)
    now = datetime.utcnow()
    doc = dict(payload)
    doc.update({
        "pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))),
        "loan_id": str(loan_id),
        "year": year,
        "month": month,
        "updated_at": now,
    })
    if not before:
        doc["created_at"] = now
        res = db[coll].insert_one(doc)
        log_audit(db, action=f"{coll}:create", collection=coll, doc_id=res.inserted_id, user=_current_user_dict(), after=doc, meta={"pg_id": pg_id, "loan_id": str(loan_id), "year": year, "month": month})
    else:
        db[coll].update_one({"_id": before["_id"]}, {"$set": doc})
        log_audit(db, action=f"{coll}:update", collection=coll, doc_id=before["_id"], user=_current_user_dict(), before=before, after=doc, meta={"pg_id": pg_id, "loan_id": str(loan_id), "year": year, "month": month})
    return jsonify({"ok": True, "message": "Saved successfully"})


# ---------- Receipt Voucher ----------
@pg_bp.route("/receipt-voucher/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_receipt_voucher(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()
    coll = "pg_receipt_vouchers"

    if request.method == "GET":
        doc = db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "year": year, "month": month}) if (year and month) else db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}, sort=[("updated_at", -1)])
        if not doc:
            return jsonify({"pg_section": {}, "member_section": {}, "year": year, "month": month})
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return jsonify(doc)

    if session.get("role") != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}
    _upsert_pg_period_doc(db, collection=coll, pg_id=pg_id, year=year, month=month, payload={
        "pg_section": payload.get("pg_section", {}),
        "member_section": payload.get("member_section", {}),
        "meta": payload.get("meta", {}),
    }, user=_current_user_dict())
    return jsonify({"ok": True, "message": "Saved successfully"})


# ---------- Generic Register Store (Input/Output/Asset/Meeting Minutes/Member Ledger) ----------
@pg_bp.route("/api/register/<name>/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_generic_register(name, pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()

    allowed = {
        "input": "pg_input_registers",
        "output": "pg_output_registers",
        "asset": "pg_asset_registers",
        "minutes": "pg_meeting_minutes",
        "member_ledger": "pg_member_ledgers",
    }
    if name not in allowed:
        abort(404)

    coll = allowed[name]

    if request.method == "GET":
        doc = db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "year": year, "month": month}) if (year and month) else db[coll].find_one({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}, sort=[("updated_at", -1)])
        if not doc:
            return jsonify({"data": {}, "year": year, "month": month})
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return jsonify(doc)

    if session.get("role") != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}
    _upsert_pg_period_doc(db, collection=coll, pg_id=pg_id, year=year, month=month, payload={
        "data": payload.get("data", payload),
    }, user=_current_user_dict())
    return jsonify({"ok": True, "message": "Saved successfully"})


@pg_bp.route("/profile/<pg_id>/data", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_profile_data(pg_id):
    """
    Fetch PG profile data as JSON for frontend profile modal.
    """

    db = current_app.mongo_db
    role = session.get("role")

    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        abort(400, "Invalid PG ID")

    pg = db.pgs.find_one({"_id": oid})
    if not pg:
        abort(404, "PG not found")

    # -----------------------------
    # Scope restriction (same logic as pg_view)
    # -----------------------------
    if role == "PG_DATA_ENTRY":
        if session.get("pg_id") != pg_id:
            abort(403)

    if session.get("clf_id") and str(pg.get("clf_id")) != session.get("clf_id"):
        abort(403)

    if session.get("block_id") and str(pg.get("block_id")) != session.get("block_id"):
        abort(403)

    if session.get("district_id") and str(pg.get("district_id")) != session.get("district_id"):
        abort(403)

    if session.get("state_id") and str(pg.get("state_id")) != session.get("state_id"):
        abort(403)

    # -------------------------------------------------
    # 🔥 Fetch Block Admin Name (CLF equivalent)
    # -------------------------------------------------
    block_admin_name = None

    block_id = pg.get("block_id")

    if block_id:
        block_admin = db.users.find_one({
            "role": "BLOCK_ADMIN",
            "block_id": block_id
        })

        if block_admin:
            block_admin_name = (
                block_admin.get("username")
                or block_admin.get("name")
            )

    # -----------------------------
    # Safe JSON Build (NO ObjectId)
    # -----------------------------
    response = {
        "pg_name": pg.get("name"),
        "activity": pg.get("sector"),
        "village": pg.get("Village"),
        "block": pg.get("Block"),
        "district": pg.get("District"),
        "state": pg.get("State"),
        "formation_date": pg.get("formation_date"),
        "total_members": pg.get("total_members", 0),

        # 🔥 Now showing Block Admin Name
        "clf_name": block_admin_name,

        "bank_details": {
            "bank_name": pg.get("bank_details", {}).get("bank_name"),
            "branch": pg.get("bank_details", {}).get("branch"),
            "account_number": pg.get("bank_details", {}).get("account_number"),
            "ifsc": pg.get("bank_details", {}).get("ifsc"),
        }
    }

    return jsonify(response)



# ============================================================
# NEW: MEETING & GOVERNANCE TRACKING
# - Meeting register, attendance, resolutions
# ============================================================

@pg_bp.route("/meetings/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def meeting_register(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    members = list(db.pg_members.find({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}, {"name":1, "member_name":1}).limit(500))

    if request.method == "POST":
        meeting_date = request.form.get("meeting_date")
        agenda = request.form.get("agenda") or ""
        resolution = request.form.get("resolution") or ""

        # attendance ids
        att = request.form.getlist("attendance")
        att_ids = []
        for mid in att:
            if ObjectId.is_valid(mid):
                att_ids.append(ObjectId(mid))
            else:
                att_ids.append(mid)

        doc = {
            "pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))),
            "meeting_date": meeting_date,
            "meeting_type": request.form.get("meeting_type") or "General",
            "agenda": agenda,
            "attendance": att_ids,
            "attendance_count": len(att_ids),
            "resolution": resolution,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        db.pg_meetings.insert_one(doc)
        flash("Meeting saved.", "success")
        return redirect(url_for("pg.meeting_register", pg_id=pg_id))

    meetings = list(db.pg_meetings.find({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}).sort([("meeting_date", -1)]).limit(200))
    return render_template("meeting_register.html", pg=pg, members=members, meetings=meetings)


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available

# ============================================================
# CLF/BLOCK: Active PG context helpers
# - Lets CLF/BLOCK work on one PG at a time using the sidebar links
# ============================================================

@pg_bp.route('/set_active/<pg_id>')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def set_active_pg(pg_id):
    """Set the active PG context in session (for CLF/BLOCK sidebar actions)."""
    db = current_app.mongo_db
    pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg_doc:
        flash('PG not found.', 'danger')
        return redirect(url_for('reports.hierarchy_dashboard'))

    role = session.get('role')

    # PG user can only set own PG
    if role == 'PG_DATA_ENTRY' and session.get('pg_id') != pg_id:
        flash('You cannot access this PG.', 'danger')
        return redirect(url_for('pg.pg_home'))

    # Scope checks for other roles (same as pg_dashboard)
    if role != 'PG_DATA_ENTRY':
        if session.get('clf_id') and str(pg_doc.get('clf_id')) != session.get('clf_id'):
            flash('This PG is not under your CLF.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('block_id') and str(pg_doc.get('block_id')) != session.get('block_id'):
            flash('This PG is not under your Block.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('district_id') and str(pg_doc.get('district_id')) != session.get('district_id'):
            flash('This PG is not under your District.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('state_id') and str(pg_doc.get('state_id')) != session.get('state_id'):
            flash('This PG is not under your State.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))

    session['active_pg_id'] = pg_id
    session['active_pg_name'] = pg_doc.get('name') or pg_doc.get('pg_name') or pg_doc.get('PG Name') or pg_id

    nxt = request.args.get('next')
    if nxt:
        return redirect(nxt)
    return redirect(url_for('pg.pg_view', pg_id=pg_id))


@pg_bp.route('/clear_active')
@login_required
@roles_required('PG_DATA_ENTRY','CLF_MANAGER','CLF_ADMIN','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def clear_active_pg():
    session.pop('active_pg_id', None)
    session.pop('active_pg_name', None)
    flash('Active PG cleared.', 'info')
    return redirect(url_for('reports.hierarchy_dashboard'))