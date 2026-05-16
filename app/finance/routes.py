from services.audit_engine import AuditLogger
import os
from flask import render_template, request, redirect, url_for, flash, current_app, session, jsonify
from app.services.guards import require_unlocked_period


def _sanitize_loan_payload(payload: dict) -> dict:
    """Backend enforcement of manual rules:
    - If status == 'discontinued' => only allow 'status' and 'reason_discontinued'
    - If status == 'closed' => block changes to principal/emi/outstanding; allow closure metadata.
    """
    if not isinstance(payload, dict):
        return payload
    status = (payload.get("status") or payload.get("loan_status") or "").strip().lower()
    if status == "discontinued":
        keep = {"status", "loan_status", "reason_discontinued", "discontinued_reason", "remarks"}
        return {k: v for k, v in payload.items() if k in keep}
    if status == "closed":
        # allow only closure-related metadata
        keep = {"status", "loan_status", "closed_on", "closure_date", "remarks", "closing_remarks"}
        return {k: v for k, v in payload.items() if k in keep}
    return payload

from bson import ObjectId
from datetime import datetime

from . import finance_bp
from ..rbac import login_required, roles_required

@finance_bp.route("/pg_funds/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_funds(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        fund_doc = {
            "pg_id": ObjectId(pg_id),
            "establishment_cost_received": request.form.get("establishment_cost_received") == "yes",
            "receiving_date": request.form.get("receiving_date"),
            "source": request.form.get("source"),
            "amount_received": float(request.form.get("amount_received") or 0),
            "total_utilized_amount": float(request.form.get("total_utilized_amount") or 0),
            "balance_amount": float(request.form.get("amount_received") or 0) - float(request.form.get("total_utilized_amount") or 0),
            "notes": request.form.get("notes"),
            "updated_at": datetime.utcnow(),
        }
        db.pg_funds.update_one(
            {"pg_id": ObjectId(pg_id)},
            {"$set": fund_doc},
            upsert=True,
        )
        flash("Fund details saved.", "success")
        return redirect(url_for("finance.pg_funds", pg_id=pg_id))

    fund = db.pg_funds.find_one({"pg_id": ObjectId(pg_id)})
    return render_template("pg_funds.html", pg=pg, fund=fund)

@finance_bp.route("/pg_loans/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_loans(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        loan_doc = {
            "pg_id": ObjectId(pg_id),
            "business_plan_submitted": request.form.get("business_plan_submitted") == "yes",
            "business_plan_date": request.form.get("business_plan_date"),
            "estimated_amount": float(request.form.get("estimated_amount") or 0),
            "sanction_amount": float(request.form.get("sanction_amount") or 0),
            "status": request.form.get("status"),
            "roi": float(request.form.get("roi") or 0),
            "tenure_months": int(request.form.get("tenure_months") or 0),
            "moratorium_period": int(request.form.get("moratorium_period") or 0),
            "principal_repaid": float(request.form.get("principal_repaid") or 0),
            "interest_paid": float(request.form.get("interest_paid") or 0),
            "total_utilized_loan_amount": float(request.form.get("total_utilized_loan_amount") or 0),
            "reason_discontinued": request.form.get("reason_discontinued"),
            "updated_at": datetime.utcnow(),
        }
        db.pg_loans.update_one(
            {"pg_id": ObjectId(pg_id)},
            {"$set": loan_doc},
            upsert=True,
        )
        flash("Loan details saved.", "success")
        return redirect(url_for("finance.pg_loans", pg_id=pg_id))

    loan = db.pg_loans.find_one({"pg_id": ObjectId(pg_id)})
    return render_template("pg_loans.html", pg=pg, loan=loan)



@finance_bp.route("/cashbook", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def cashbook():
    # Cash Book screen (data persists via /api/cashbook/<pg_id> in pg blueprint)
    pg_id = session.get("pg_id") or session.get("active_pg_id")
    return render_template("cash_book.html", pg_id=pg_id)


# ============================================================
# NEW: LOAN LIFECYCLE (PG + MEMBER) + REPAYMENTS + DASHBOARD
# - Does NOT remove existing /pg_loans route (kept for legacy)
# - Adds multiple loan accounts, disbursement, EMI schedule,
#   repayment tracking, overdue logic, and status (active/closed/NPA)
# ============================================================

def _amort_schedule(principal: float, annual_roi: float, tenure_months: int, start_date: str):
    """Simple amortization schedule (monthly).
    start_date: YYYY-MM-DD (1st due date will be next month same day if possible).
    Stores only essential fields for MIS dashboards.
    """
    from datetime import datetime
    import math

    principal = float(principal or 0)
    tenure_months = int(tenure_months or 0)
    annual_roi = float(annual_roi or 0)

    if principal <= 0 or tenure_months <= 0:
        return []

    r = (annual_roi/100.0)/12.0
    if r <= 0:
        emi = principal/tenure_months
    else:
        emi = principal * r * (1+r)**tenure_months / ((1+r)**tenure_months - 1)

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
    except Exception:
        sd = datetime.utcnow()

    schedule = []
    bal = principal
    for i in range(1, tenure_months+1):
        interest = bal * r
        principal_comp = emi - interest
        if principal_comp > bal:
            principal_comp = bal
        bal = bal - principal_comp
        due_date = sd.replace(day=min(sd.day, 28))
        # naive monthly increment
        month = due_date.month + (i-1)
        year = due_date.year + (month-1)//12
        month = (month-1)%12 + 1
        due_date = due_date.replace(year=year, month=month)
        schedule.append({
            "instalment_no": i,
            "due_date": due_date,
            "emi": round(emi,2),
            "principal": round(principal_comp,2),
            "interest": round(interest,2),
            "balance": round(bal,2),
            "is_paid": False,
            "paid_at": None,
            "paid_amount": 0.0,
        })
    return schedule

def _loan_summary_from_schedule(schedule):
    """Compute loan KPIs required by the manual.

    Outputs:
      - principal_outstanding
      - interest_due
      - installment tracking (paid/total, next_due, overdue)
      - amount outstanding (EMI-based)
    """
    from datetime import datetime
    now = datetime.utcnow()

    schedule = schedule or []

    total_emi = 0.0
    total_paid = 0.0
    principal_outstanding = 0.0
    interest_due = 0.0
    principal_due = 0.0

    next_due = None
    overdue_amt = 0.0
    paid_installments = 0
    total_installments = 0

    for x in schedule:
        total_installments += 1
        emi = float(x.get("emi") or 0)
        pr = float(x.get("principal") or 0)
        it = float(x.get("interest") or 0)
        total_emi += emi

        if x.get("is_paid"):
            paid_installments += 1
            total_paid += float(x.get("paid_amount") or emi or 0)
        else:
            principal_outstanding += pr
            # interest due = unpaid interest portions up to now (and also current due)
            if x.get("due_date") and x["due_date"] <= now:
                interest_due += it
                principal_due += pr
                overdue_amt += emi
            if next_due is None:
                next_due = x.get("due_date")

    outstanding_amount = max(total_emi - total_paid, 0.0)

    return {
        "total_payable": round(total_emi, 2),
        "total_paid": round(total_paid, 2),
        "outstanding_amount": round(outstanding_amount, 2),

        "principal_outstanding": round(principal_outstanding, 2),
        "interest_due": round(interest_due, 2),
        "principal_due": round(principal_due, 2),

        "installments_total": int(total_installments),
        "installments_paid": int(paid_installments),
        "installments_pending": int(max(total_installments - paid_installments, 0)),

        "next_due_date": next_due,
        "overdue_amount": round(overdue_amt, 2),
    }

@finance_bp.route("/loan_dashboard/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def loan_dashboard(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    loans = list(db.pg_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)]))
    mloans = list(db.pg_member_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)]).limit(50))

    # Basic KPIs
    total_outstanding = sum(float(l.get("outstanding_amount") or 0) for l in loans) + sum(float(l.get("outstanding_amount") or 0) for l in mloans)
    overdue = sum(float(l.get("overdue_amount") or 0) for l in loans) + sum(float(l.get("overdue_amount") or 0) for l in mloans)
    active_count = sum(1 for l in loans if (l.get("status") in ("active","ongoing"))) + sum(1 for l in mloans if (l.get("status") in ("active","ongoing")))

    return render_template("loan_dashboard.html", pg=pg, loans=loans, mloans=mloans, kpi={"outstanding": total_outstanding, "overdue": overdue, "active": active_count})

# changes made by atlanta 
@finance_bp.route("/loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_accounts(pg_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if is_json_request():
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST" and session.get("role") == "PG_DATA_ENTRY":
        if is_json_request():
            return jsonify({
                "ok": False,
                "message": "Loans can only be created/edited by CLF/Block authorities."
            }), 403
        flash("Loans can only be created/edited by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form

        loan_no = (payload.get("loan_no") or "").strip()
        principal = float(payload.get("principal") or 0)
        sanctioned = float(payload.get("sanction_amount") or 0)
        disbursed = float(payload.get("disbursed_amount") or 0)
        roi = float(payload.get("roi") or 0)
        tenure = int(payload.get("tenure_months") or 0)
        start_date = (
            payload.get("first_due_date")
            or payload.get("disbursement_date")
            or datetime.utcnow().strftime("%Y-%m-%d")
        )

        base_amt = disbursed if disbursed > 0 else (sanctioned if sanctioned > 0 else principal)
        schedule = _amort_schedule(base_amt, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": pg_obj_id,
            "loan_no": loan_no or f"PG-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "lender": payload.get("lender") or payload.get("source") or "",
            "purpose": payload.get("purpose") or "",
            "business_plan_submitted": payload.get("business_plan_submitted") == "yes",
            "business_plan_date": payload.get("business_plan_date") or None,
            "estimated_amount": principal,
            "sanction_amount": sanctioned,
            "disbursed_amount": disbursed,
            "disbursement_date": payload.get("disbursement_date") or None,
            "roi": roi,
            "tenure_months": tenure,
            "moratorium_months": int(payload.get("moratorium_months") or 0),
            "status": (payload.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        result = db.pg_loan_accounts.insert_one(doc)

        if is_json_request():
            return jsonify({
                "ok": True,
                "message": "Loan account created.",
                "loan_id": str(result.inserted_id),
            }), 201

        flash("Loan account created.", "success")
        return redirect(url_for("finance.loan_accounts", pg_id=pg_id))

    loans_raw = list(
        db.pg_loan_accounts.find({"pg_id": pg_obj_id}).sort([("created_at", -1)])
    )

    loans = []
    for i, l in enumerate(loans_raw, start=1):
        loans.append({
            "_id": str(l.get("_id")),
            "id": str(l.get("_id")),
            "sl": i,
            "loan_no": l.get("loan_no") or "",
            "lender": l.get("lender") or "",
            "purpose": l.get("purpose") or "",
            "status": (l.get("status") or "active").lower(),
            "estimated_amount": float(l.get("estimated_amount") or 0),
            "sanction_amount": float(l.get("sanction_amount") or 0),
            "disbursed_amount": float(l.get("disbursed_amount") or 0),
            "roi": float(l.get("roi") or 0),
            "tenure_months": int(l.get("tenure_months") or 0),
            "moratorium_months": int(l.get("moratorium_months") or 0),
            "outstanding_amount": float(l.get("outstanding_amount") or 0),
            "overdue_amount": float(l.get("overdue_amount") or 0),
            "total_paid": float(l.get("total_paid") or 0),
            "installments_total": int(l.get("installments_total") or 0),
            "installments_paid": int(l.get("installments_paid") or 0),
            "installments_pending": int(l.get("installments_pending") or 0),
            "next_due_date": serialize_dt(l.get("next_due_date")),
            "created_at": serialize_dt(l.get("created_at")),
            "updated_at": serialize_dt(l.get("updated_at")),
        })

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "loans": loans,
            "can_create": session.get("role") != "PG_DATA_ENTRY",
        })

    return render_template("loan_accounts.html", pg=pg, loans=loans_raw)

# changes made by atlanta
@finance_bp.route("/loan_account/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_account_view(loan_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        loan_obj_id = ObjectId(loan_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid loan ID"}), 400
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    loan = db.pg_loan_accounts.find_one({"_id": loan_obj_id})
    if not loan:
        if is_json_request():
            return jsonify({"ok": False, "message": "Loan not found"}), 404
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": loan.get("pg_id")}) if loan.get("pg_id") else None

    if request.method == "POST" and session.get("role") == "PG_DATA_ENTRY":
        if is_json_request():
            return jsonify({
                "ok": False,
                "message": "Loan repayments/updates can only be entered by CLF/Block authorities."
            }), 403
        flash("Loan repayments/updates can only be entered by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form

        amt = float(payload.get("paid_amount") or 0)
        inst_no = int(payload.get("instalment_no") or 0)
        paid_at = payload.get("paid_at") or datetime.utcnow().strftime("%Y-%m-%d")

        from datetime import datetime as _dt
        try:
            paid_dt = _dt.strptime(paid_at, "%Y-%m-%d")
        except Exception:
            paid_dt = _dt.utcnow()

        schedule = loan.get("schedule") or []

        for item in schedule:
            if int(item.get("instalment_no") or 0) == inst_no and not item.get("is_paid"):
                item["is_paid"] = True
                item["paid_at"] = paid_dt
                item["paid_amount"] = amt
                break

        summary = _loan_summary_from_schedule(schedule)

        status = (loan.get("status") or "active").strip().lower()
        if summary.get("overdue_amount", 0) > 0 and summary.get("next_due_date"):
            try:
                if (datetime.utcnow() - summary["next_due_date"]).days >= 90:
                    status = "npa"
            except Exception:
                pass
        if summary.get("outstanding_amount", 0) <= 0.01:
            status = "closed"

        db.pg_loan_repayments.insert_one({
            "loan_id": loan_obj_id,
            "pg_id": loan.get("pg_id"),
            "instalment_no": inst_no,
            "paid_amount": amt,
            "paid_at": paid_dt,
            "created_at": datetime.utcnow(),
        })

        db.pg_loan_accounts.update_one(
            {"_id": loan_obj_id},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )

        if is_json_request():
            return jsonify({"ok": True, "message": "Repayment saved."}), 200

        flash("Repayment saved.", "success")
        return redirect(url_for("finance.loan_account_view", loan_id=loan_id))

    repayments_raw = list(
        db.pg_loan_repayments.find({"loan_id": loan_obj_id}).sort([("paid_at", -1)])
    )

    repayments = [
        {
            "_id": str(r.get("_id")),
            "instalment_no": int(r.get("instalment_no") or 0),
            "paid_amount": float(r.get("paid_amount") or 0),
            "paid_at": serialize_dt(r.get("paid_at")),
            "created_at": serialize_dt(r.get("created_at")),
        }
        for r in repayments_raw
    ]

    schedule_json = []
    for s in loan.get("schedule") or []:
        schedule_json.append({
            "instalment_no": int(s.get("instalment_no") or 0),
            "due_date": serialize_dt(s.get("due_date")),
            "emi": float(s.get("emi") or 0),
            "is_paid": bool(s.get("is_paid")),
            "paid_amount": float(s.get("paid_amount") or 0),
            "paid_at": serialize_dt(s.get("paid_at")),
        })

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "loan": {
                "_id": str(loan.get("_id")),
                "loan_no": loan.get("loan_no") or "",
                "lender": loan.get("lender") or "",
                "purpose": loan.get("purpose") or "",
                "status": (loan.get("status") or "active").lower(),
                "estimated_amount": float(loan.get("estimated_amount") or 0),
                "sanction_amount": float(loan.get("sanction_amount") or 0),
                "disbursed_amount": float(loan.get("disbursed_amount") or 0),
                "roi": float(loan.get("roi") or 0),
                "tenure_months": int(loan.get("tenure_months") or 0),
                "moratorium_months": int(loan.get("moratorium_months") or 0),
                "outstanding_amount": float(loan.get("outstanding_amount") or 0),
                "overdue_amount": float(loan.get("overdue_amount") or 0),
                "total_paid": float(loan.get("total_paid") or 0),
                "installments_total": int(loan.get("installments_total") or 0),
                "installments_paid": int(loan.get("installments_paid") or 0),
                "installments_pending": int(loan.get("installments_pending") or 0),
                "next_due_date": serialize_dt(loan.get("next_due_date")),
                "created_at": serialize_dt(loan.get("created_at")),
                "updated_at": serialize_dt(loan.get("updated_at")),
                "schedule": schedule_json,
            },
            "repayments": repayments,
            "can_edit": session.get("role") != "PG_DATA_ENTRY",
        })

    return render_template("loan_account_view.html", pg=pg, loan=loan, repayments=repayments_raw)

#changes made atlanta
@finance_bp.route("/member_loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_accounts(pg_id):
    db = current_app.mongo_db

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if request.args.get("format") == "json" or request.headers.get("Accept", "").lower().find("application/json") >= 0:
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if request.args.get("format") == "json" or request.headers.get("Accept", "").lower().find("application/json") >= 0:
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    members_raw = list(
        db.pg_members.find({"pg_id": pg_obj_id}, {"name": 1, "member_name": 1}).limit(500)
    )

    def _member_name(m):
        return (m.get("name") or m.get("member_name") or "Member").strip()

    members = [
        {
            "_id": str(m["_id"]),
            "name": _member_name(m),
        }
        for m in members_raw
    ]

    is_json_request = (
        request.args.get("format") == "json"
        or "application/json" in (request.headers.get("Accept", "").lower())
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )

    # PG can view loans, but only CLF/Block authorities can create/update.
    if request.method == "POST" and session.get("role") == "PG_DATA_ENTRY":
        if is_json_request:
            return jsonify({
                "ok": False,
                "message": "Member loans can only be created/edited by CLF/Block authorities."
            }), 403
        flash("Member loans can only be created/edited by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form

        member_id = payload.get("member_id")
        principal = float(payload.get("principal") or 0)
        roi = float(payload.get("roi") or 0)
        tenure = int(payload.get("tenure_months") or 0)
        start_date = payload.get("first_due_date") or datetime.utcnow().strftime("%Y-%m-%d")

        schedule = _amort_schedule(principal, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": pg_obj_id,
            "member_id": ObjectId(member_id) if (member_id and ObjectId.is_valid(member_id)) else member_id,
            "loan_no": (payload.get("loan_no") or "").strip() or f"MB-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "purpose": payload.get("purpose") or "",
            "principal": principal,
            "roi": roi,
            "tenure_months": tenure,
            "status": (payload.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        result = db.pg_member_loan_accounts.insert_one(doc)

        if is_json_request:
            return jsonify({
                "ok": True,
                "message": "Member loan created.",
                "loan_id": str(result.inserted_id)
            }), 201

        flash("Member loan created.", "success")
        return redirect(url_for("finance.member_loan_accounts", pg_id=pg_id))

    loans_raw = list(
        db.pg_member_loan_accounts.find({"pg_id": pg_obj_id}).sort([("created_at", -1)])
    )

    member_map = {m["_id"]: m["name"] for m in members}

    def _serialize_dt(dt):
        if not dt:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)

    loans = []
    for i, l in enumerate(loans_raw, start=1):
        member_id_str = str(l.get("member_id")) if l.get("member_id") is not None else ""
        loans.append({
            "_id": str(l["_id"]),
            "sl": i,
            "loan_no": l.get("loan_no") or "",
            "member_id": member_id_str,
            "member_name": member_map.get(member_id_str, "Member"),
            "purpose": l.get("purpose") or "",
            "principal": float(l.get("principal") or 0),
            "roi": float(l.get("roi") or 0),
            "tenure_months": int(l.get("tenure_months") or 0),
            "status": (l.get("status") or "active").lower(),
            "outstanding_amount": float(l.get("outstanding_amount") or 0),
            "overdue_amount": float(l.get("overdue_amount") or 0),
            "total_paid": float(l.get("total_paid") or 0),
            "installments_total": int(l.get("installments_total") or 0),
            "installments_paid": int(l.get("installments_paid") or 0),
            "installments_pending": int(l.get("installments_pending") or 0),
            "next_due_date": _serialize_dt(l.get("next_due_date")),
            "created_at": _serialize_dt(l.get("created_at")),
            "updated_at": _serialize_dt(l.get("updated_at")),
        })

    if is_json_request:
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "members": members,
            "loans": loans,
            "can_create": session.get("role") != "PG_DATA_ENTRY",
        })

    return render_template("member_loan_accounts.html", pg=pg, loans=loans_raw, members=members_raw)

#changes made by atlanta
@finance_bp.route("/member_loan_view/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_view(loan_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        loan_obj_id = ObjectId(loan_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid loan ID"}), 400
        flash("Invalid loan ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    loan = db.pg_member_loan_accounts.find_one({"_id": loan_obj_id})
    if not loan:
        if is_json_request():
            return jsonify({"ok": False, "message": "Loan not found"}), 404
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": loan.get("pg_id")}) if loan.get("pg_id") else None
    member = None
    if loan.get("member_id") and ObjectId.is_valid(str(loan.get("member_id"))):
        member = db.pg_members.find_one({"_id": ObjectId(str(loan.get("member_id")))})

    if request.method == "POST" and session.get("role") == "PG_DATA_ENTRY":
        if is_json_request():
            return jsonify({
                "ok": False,
                "message": "View only: Loan entry/updates are handled by CLF/Block."
            }), 403
        flash("View only: Loan entry/updates are handled by CLF/Block.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form

        inst_no = int(payload.get("instalment_no") or 0)
        amt = float(payload.get("paid_amount") or 0)
        paid_at_raw = payload.get("paid_at")

        try:
            paid_dt = datetime.strptime(paid_at_raw, "%Y-%m-%d") if paid_at_raw else datetime.utcnow()
        except Exception:
            paid_dt = datetime.utcnow()

        schedule = loan.get("schedule") or []
        for row in schedule:
            if int(row.get("instalment_no") or 0) == inst_no:
                row["is_paid"] = True
                row["paid_amount"] = float(amt)
                row["paid_at"] = paid_dt
                break

        summary = _loan_summary_from_schedule(schedule)
        status = "closed" if float(summary.get("outstanding_amount") or 0) <= 0 else (loan.get("status") or "active")

        db.pg_member_loan_repayments.insert_one(
            {
                "loan_id": loan_obj_id,
                "pg_id": loan.get("pg_id"),
                "member_id": loan.get("member_id"),
                "instalment_no": inst_no,
                "paid_amount": amt,
                "paid_at": paid_dt,
                "created_at": datetime.utcnow(),
            }
        )

        db.pg_member_loan_accounts.update_one(
            {"_id": loan_obj_id},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )

        if is_json_request():
            return jsonify({
                "ok": True,
                "message": "Repayment saved.",
            }), 200

        flash("Repayment saved.", "success")
        return redirect(url_for("finance.member_loan_view", loan_id=loan_id))

    repayments_raw = list(
        db.pg_member_loan_repayments.find({"loan_id": loan_obj_id}).sort([("paid_at", -1)])
    )

    schedule = loan.get("schedule") or []
    repayments = [
        {
            "_id": str(r.get("_id")),
            "instalment_no": int(r.get("instalment_no") or 0),
            "paid_amount": float(r.get("paid_amount") or 0),
            "paid_at": serialize_dt(r.get("paid_at")),
            "created_at": serialize_dt(r.get("created_at")),
        }
        for r in repayments_raw
    ]

    schedule_json = []
    for s in schedule:
        schedule_json.append({
            "instalment_no": int(s.get("instalment_no") or 0),
            "due_date": serialize_dt(s.get("due_date")),
            "emi": float(s.get("emi") or 0),
            "is_paid": bool(s.get("is_paid")),
            "paid_amount": float(s.get("paid_amount") or 0),
            "paid_at": serialize_dt(s.get("paid_at")),
        })

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "loan": {
                "_id": str(loan.get("_id")),
                "loan_no": loan.get("loan_no") or "",
                "purpose": loan.get("purpose") or "",
                "principal": float(loan.get("principal") or 0),
                "roi": float(loan.get("roi") or 0),
                "tenure_months": int(loan.get("tenure_months") or 0),
                "status": (loan.get("status") or "active").lower(),
                "outstanding_amount": float(loan.get("outstanding_amount") or 0),
                "overdue_amount": float(loan.get("overdue_amount") or 0),
                "total_paid": float(loan.get("total_paid") or 0),
                "installments_total": int(loan.get("installments_total") or 0),
                "installments_paid": int(loan.get("installments_paid") or 0),
                "installments_pending": int(loan.get("installments_pending") or 0),
                "next_due_date": serialize_dt(loan.get("next_due_date")),
                "created_at": serialize_dt(loan.get("created_at")),
                "updated_at": serialize_dt(loan.get("updated_at")),
                "schedule": schedule_json,
            },
            "member": {
                "_id": str(member.get("_id")) if member else "",
                "name": (member.get("name") or member.get("member_name") or "Member") if member else "Member",
            },
            "repayments": repayments,
            "can_edit": session.get("role") != "PG_DATA_ENTRY",
        })

    return render_template(
        "member_loan_view.html",
        pg=pg,
        loan=loan,
        member=member,
        repayments=repayments_raw
    )


def _safe_float(value, default=0.0):
    try:
        return float(value or default)
    except Exception:
        return float(default)


def _grant_utilization_total(db, grant_id, include_rejected=False):
    grant_obj_id = grant_id if isinstance(grant_id, ObjectId) else ObjectId(str(grant_id))

    match = {"grant_id": grant_obj_id}
    if not include_rejected:
        match["status"] = {"$ne": "REJECTED"}

    rows = list(db.pg_grant_utilizations.aggregate([
        {"$match": match},
        {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
    ]))

    return round(float(rows[0]["utilized"] if rows else 0), 2)


def _sync_grant_utilization_summary(db, grant_id):
    """Sync utilization total and balance into pg_grants.

    This does not change your workflow.
    It only makes the parent grant record store the latest utilized/balance values.
    """
    grant_obj_id = grant_id if isinstance(grant_id, ObjectId) else ObjectId(str(grant_id))

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return {
            "utilized_amount": 0.0,
            "balance_amount": 0.0,
        }

    received = _safe_float(grant.get("amount_received"))
    utilized = _grant_utilization_total(db, grant_obj_id, include_rejected=False)
    balance = round(received - utilized, 2)

    db.pg_grants.update_one(
        {"_id": grant_obj_id},
        {
            "$set": {
                "utilized_amount": utilized,
                "balance_amount": balance,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    return {
        "utilized_amount": utilized,
        "balance_amount": balance,
    }


 # changes made by atlanta
@finance_bp.route("/grants/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grants(pg_id):
    db = current_app.mongo_db

    def wants_json():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            return str(v)[:10]
        except Exception:
            return str(v)

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if wants_json():
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if wants_json():
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form

        amount_received = _safe_float(payload.get("amount_received"))

        doc = {
            "pg_id": pg_obj_id,
            "category": payload.get("category") or "Infrastructure",
            "source": payload.get("source") or "",
            "release_date": payload.get("release_date") or None,
            "amount_received": amount_received,
            "utilized_amount": 0.0,
            "balance_amount": amount_received,
            "uc_status": payload.get("uc_status") or "pending",
            "uc_submitted_date": payload.get("uc_submitted_date") or None,
            "notes": payload.get("notes") or "",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        res = db.pg_grants.insert_one(doc)

        if wants_json():
            return jsonify({
                "ok": True,
                "message": "Grant added.",
                "grant_id": str(res.inserted_id),
            }), 201

        flash("Grant added.", "success")
        return redirect(url_for("finance.grants", pg_id=pg_id))

    grants_raw = list(db.pg_grants.find({"pg_id": pg_obj_id}).sort([("created_at", -1)]))

    grants_json = []
    for i, g in enumerate(grants_raw, start=1):
        summary = _sync_grant_utilization_summary(db, g.get("_id"))

        g["utilized_amount"] = summary["utilized_amount"]
        g["balance_amount"] = summary["balance_amount"]

        grants_json.append({
            "_id": str(g.get("_id")),
            "id": str(g.get("_id")),
            "sl": i,
            "category": g.get("category") or "",
            "source": g.get("source") or "",
            "release_date": serialize_dt(g.get("release_date")),
            "amount_received": float(g.get("amount_received") or 0),
            "utilized_amount": float(g.get("utilized_amount") or 0),
            "balance_amount": float(g.get("balance_amount") or 0),
            "uc_status": g.get("uc_status") or "pending",
            "uc_submitted_date": serialize_dt(g.get("uc_submitted_date")),
            "notes": g.get("notes") or "",
            "created_at": serialize_dt(g.get("created_at")),
            "updated_at": serialize_dt(g.get("updated_at")),
        })

    if wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "grants": grants_json,
        })

    return render_template("grants.html", pg=pg, grants=grants_raw)


# changes made by atlanta
@finance_bp.route("/grant/<grant_id>/update", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grant_update(grant_id):
    db = current_app.mongo_db

    try:
        grant_obj_id = ObjectId(grant_id)
    except Exception:
        return jsonify({"ok": False, "message": "Invalid grant ID"}), 400

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return jsonify({"ok": False, "message": "Grant not found"}), 404

    payload = request.get_json(silent=True) or request.form

    update_doc = {
        "category": payload.get("category") or grant.get("category") or "Infrastructure",
        "source": payload.get("source") or "",
        "release_date": payload.get("release_date") or None,
        "amount_received": float(payload.get("amount_received") or 0),
        "uc_status": payload.get("uc_status") or "pending",
        "uc_submitted_date": payload.get("uc_submitted_date") or None,
        "notes": payload.get("notes") or "",
        "updated_at": datetime.utcnow(),
    }

    db.pg_grants.update_one({"_id": grant_obj_id}, {"$set": update_doc})
    _sync_grant_utilization_summary(db, grant_obj_id)

    return jsonify({"ok": True, "message": "Grant updated."}), 200


# changes made by atlanta
@finance_bp.route("/grant/<grant_id>/delete", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grant_delete(grant_id):
    db = current_app.mongo_db

    try:
        grant_obj_id = ObjectId(grant_id)
    except Exception:
        return jsonify({"ok": False, "message": "Invalid grant ID"}), 400

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return jsonify({"ok": False, "message": "Grant not found"}), 404

    db.pg_grant_utilizations.delete_many({"grant_id": grant_obj_id})
    db.pg_grants.delete_one({"_id": grant_obj_id})

    return jsonify({"ok": True, "message": "Grant deleted."}), 200


@finance_bp.route("/grant/<grant_id>", methods=["GET", "POST"]) 
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def grant_view(grant_id):
    db = current_app.mongo_db

    def wants_json():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            return str(v)[:10]
        except Exception:
            return str(v)

    try:
        grant_obj_id = ObjectId(grant_id)
    except Exception:
        if wants_json():
            return jsonify({"ok": False, "message": "Invalid grant ID"}), 400
        flash("Invalid grant ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        if wants_json():
            return jsonify({"ok": False, "message": "Grant not found"}), 404
        flash("Grant not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": grant.get("pg_id")}) if grant.get("pg_id") else None

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form
        action = (payload.get("action") or "utilize").strip().lower()

        if action == "delete":
            db.pg_grant_utilizations.delete_many({"grant_id": grant_obj_id})
            db.pg_grants.delete_one({"_id": grant_obj_id})

            if wants_json():
                return jsonify({"ok": True, "message": "Grant deleted."}), 200

            flash("Grant deleted.", "success")
            return redirect(url_for("finance.grants", pg_id=str(pg.get("_id"))))

        if action == "update":
            amount_received = _safe_float(payload.get("amount_received"))

            update_doc = {
                "category": payload.get("category") or grant.get("category") or "Infrastructure",
                "source": payload.get("source") or "",
                "release_date": payload.get("release_date") or None,
                "amount_received": amount_received,
                "uc_status": payload.get("uc_status") or "pending",
                "uc_submitted_date": payload.get("uc_submitted_date") or None,
                "notes": payload.get("notes") or "",
                "updated_at": datetime.utcnow(),
            }

            db.pg_grants.update_one({"_id": grant_obj_id}, {"$set": update_doc})
            _sync_grant_utilization_summary(db, grant_obj_id)

            if wants_json():
                return jsonify({"ok": True, "message": "Grant updated."}), 200

            flash("Grant updated.", "success")
            return redirect(url_for("finance.grants", pg_id=str(pg.get("_id"))))
        role = session.get("role")

        head = (payload.get("head") or "").strip()
        amount = float(payload.get("amount") or 0)

        if not head:
            if wants_json():
                return jsonify({"ok": False, "message": "Head is required."}), 400
            flash("Head is required.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))


        if amount <= 0:
            if wants_json():
                return jsonify({"ok": False, "message": "Amount must be greater than 0."}), 400
            flash("Amount must be greater than 0.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        utilized_at = payload.get("utilized_at") or datetime.utcnow().strftime("%Y-%m-%d")
        try:
            u_dt = datetime.strptime(utilized_at, "%Y-%m-%d")
        except Exception:
            u_dt = datetime.utcnow()

        existing = list(
            db.pg_grant_utilizations.find({"grant_id": grant_obj_id}, {"amount": 1, "status": 1})
        )
        utilized_total = sum(float(x.get("amount") or 0) for x in existing)
        balance = float(grant.get("amount_received") or 0) - utilized_total

        if amount > balance + 1e-6:
            msg = f"Utilization exceeds available balance (Balance: ₹{balance:,.2f})."
            if wants_json():
                return jsonify({"ok": False, "message": msg}), 400
            flash(msg, "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        attachments = []
        try:
            from werkzeug.utils import secure_filename
            upload_dir = current_app.config.get("UPLOAD_FOLDER", "uploads")
            os.makedirs(upload_dir, exist_ok=True)

            files = request.files.getlist("attachments") if request.files else []
            for f in files:
                if not f or not getattr(f, "filename", ""):
                    continue
                fn = secure_filename(f.filename)
                if not fn:
                    continue
                stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
                saved = f"grant_{grant_id}_{stamp}_{fn}"
                path = os.path.join(upload_dir, saved)
                f.save(path)
                attachments.append(saved)
        except Exception:
            attachments = []

        util_doc = {
            "grant_id": grant_obj_id,
            "pg_id": grant.get("pg_id"),
            "utilized_at": u_dt,
            "head": head,
            "amount": amount,
            "remarks": payload.get("remarks") or "",
            "attachments": attachments,
            "status": "PENDING",
            "created_by": session.get("user_id"),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        res = db.pg_grant_utilizations.insert_one(util_doc)

        summary = _sync_grant_utilization_summary(db, grant_obj_id)

        if wants_json():
            return jsonify({
                "ok": True,
                "message": "Utilization entry saved.",
                "utilization_id": str(res.inserted_id),
                "utilized_amount": summary["utilized_amount"],
                "balance_amount": summary["balance_amount"],
            }), 201

        flash("Utilization entry saved.", "success")
        return redirect(url_for("finance.grant_view", grant_id=grant_id))

    utils_raw = list(
        db.pg_grant_utilizations.find({"grant_id": grant_obj_id}).sort([("utilized_at", -1), ("created_at", -1)])
    )

    utilized_total = sum(float(x.get("amount") or 0) for x in utils_raw)
    balance = round(float(grant.get("amount_received") or 0) - utilized_total, 2)

    utils = []
    for i, u in enumerate(utils_raw, start=1):
        utils.append({
            "_id": str(u.get("_id")),
            "sl": i,
            "utilized_at": serialize_dt(u.get("utilized_at")),
            "head": u.get("head") or "",
            "amount": float(u.get("amount") or 0),
            "remarks": u.get("remarks") or "",
            "status": (u.get("status") or "PENDING").upper(),
            "attachments": u.get("attachments") or [],
            "attachments_count": len(u.get("attachments") or []),
            "created_at": serialize_dt(u.get("created_at")),
            "updated_at": serialize_dt(u.get("updated_at")),
        })

    grant_json = {
        "_id": str(grant.get("_id")),
        "category": grant.get("category") or "",
        "source": grant.get("source") or "",
        "release_date": serialize_dt(grant.get("release_date")),
        "amount_received": float(grant.get("amount_received") or 0),
        "uc_status": grant.get("uc_status") or "pending",
        "uc_submitted_date": serialize_dt(grant.get("uc_submitted_date")),
        "notes": grant.get("notes") or "",
        "utilized_total": round(utilized_total, 2),
        "balance": balance,
        "created_at": serialize_dt(grant.get("created_at")),
        "updated_at": serialize_dt(grant.get("updated_at")),
    }

    if wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "grant": grant_json,
            "utils": utils,
            "role": session.get("role") or "",
            "can_edit": session.get("role") != "PG_DATA_ENTRY",
        }), 200

    return render_template(
        "grant_view.html",
        pg=pg,
        grant=grant,
        utils=utils_raw,
        utilized_total=utilized_total,
        balance=balance,
        role=session.get("role"),
    )

@finance_bp.route("/grant_utilization/<util_id>/approve", methods=["POST"])
@login_required
@roles_required("CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def approve_grant_utilization(util_id):
    db = current_app.mongo_db
    util = db.pg_grant_utilizations.find_one({"_id": ObjectId(util_id)})
    if not util:
        flash("Utilization entry not found.", "danger")
        return redirect(url_for("pg.pg_home"))
    role = session.get("role")
    cur = (util.get("status") or "PENDING").upper()

    next_status = None
    if role in ("CLF_MANAGER", "CLF_ADMIN") and cur == "PENDING":
        next_status = "CLF_APPROVED"
    elif role == "BLOCK_ADMIN" and cur in ("CLF_APPROVED", "PENDING"):
        next_status = "BLOCK_APPROVED"
    elif role == "DISTRICT_ADMIN" and cur in ("BLOCK_APPROVED", "CLF_APPROVED", "PENDING"):
        next_status = "DISTRICT_APPROVED"
    elif role in ("ADMIN", "SUPER_ADMIN"):
        next_status = "STATE_APPROVED"

    if not next_status:
        flash("Cannot approve at this stage. Please follow the approval chain.", "danger")
        return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))

    db.pg_grant_utilizations.update_one(
        {"_id": ObjectId(util_id)},
        {
            "$set": {
                "status": next_status,
                "approved_by": session.get("user_id"),
                "approved_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        },
    )

    _sync_grant_utilization_summary(db, util.get("grant_id"))

    flash(f"Utilization moved to {next_status}.", "success")
    return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))


@finance_bp.route("/grant_utilization/<util_id>/reject", methods=["POST"])
@login_required
@roles_required("CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def reject_grant_utilization(util_id):
    db = current_app.mongo_db
    util = db.pg_grant_utilizations.find_one({"_id": ObjectId(util_id)})
    if not util:
        flash("Utilization entry not found.", "danger")
        return redirect(url_for("pg.pg_home"))
    reason = (request.form.get("reason") or "").strip()
    db.pg_grant_utilizations.update_one(
        {"_id": ObjectId(util_id)},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": reason,
                "approved_by": session.get("user_id"),
                "approved_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        },
    )

    _sync_grant_utilization_summary(db, util.get("grant_id"))

    flash("Utilization rejected.", "warning")
    return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available
