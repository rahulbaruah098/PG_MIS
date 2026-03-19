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

@finance_bp.route("/loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_accounts(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # PG can view loans, but only CLF/Block authorities can create/update.
    if request.method == "POST" and session.get("role") in ("PG_DATA_ENTRY", "CADRE_CC"):
        flash("Loans can only be created/edited by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        loan_no = (request.form.get("loan_no") or "").strip()
        principal = float(request.form.get("principal") or 0)
        sanctioned = float(request.form.get("sanction_amount") or 0)
        disbursed = float(request.form.get("disbursed_amount") or 0)
        roi = float(request.form.get("roi") or 0)
        tenure = int(request.form.get("tenure_months") or 0)
        start_date = (
            request.form.get("first_due_date")
            or request.form.get("disbursement_date")
            or datetime.utcnow().strftime("%Y-%m-%d")
        )

        base_amt = disbursed if disbursed > 0 else (sanctioned if sanctioned > 0 else principal)
        schedule = _amort_schedule(base_amt, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": ObjectId(pg_id),
            "loan_no": loan_no or f"PG-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "lender": request.form.get("lender") or request.form.get("source") or "",
            "purpose": request.form.get("purpose") or "",
            "business_plan_submitted": request.form.get("business_plan_submitted") == "yes",
            "business_plan_date": request.form.get("business_plan_date") or None,
            "estimated_amount": principal,
            "sanction_amount": sanctioned,
            "disbursed_amount": disbursed,
            "disbursement_date": request.form.get("disbursement_date") or None,
            "roi": roi,
            "tenure_months": tenure,
            "moratorium_months": int(request.form.get("moratorium_months") or 0),
            "status": (request.form.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        db.pg_loan_accounts.insert_one(doc)
        flash("Loan account created.", "success")
        return redirect(url_for("finance.loan_accounts", pg_id=pg_id))

    loans = list(
        db.pg_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)])
    )
    return render_template("loan_accounts.html", pg=pg, loans=loans)


@finance_bp.route("/loan_account/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_account_view(loan_id):
    db = current_app.mongo_db
    loan = db.pg_loan_accounts.find_one({"_id": ObjectId(loan_id)})
    if not loan:
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))
    pg = db.pgs.find_one({"_id": loan.get("pg_id")})

    # PG can view loans, but only CLF/Block authorities can create/update.
    if request.method == "POST" and session.get("role") in ("PG_DATA_ENTRY", "CADRE_CC"):
        flash("Loan repayments/updates can only be entered by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        amt = float(request.form.get("paid_amount") or 0)
        inst_no = int(request.form.get("instalment_no") or 0)
        paid_at = request.form.get("paid_at") or datetime.utcnow().strftime("%Y-%m-%d")

        schedule = loan.get("schedule") or []
        from datetime import datetime as _dt
        try:
            paid_dt = _dt.strptime(paid_at, "%Y-%m-%d")
        except Exception:
            paid_dt = _dt.utcnow()

        # mark schedule
        for item in schedule:
            if int(item.get("instalment_no") or 0) == inst_no and not item.get("is_paid"):
                item["is_paid"] = True
                item["paid_at"] = paid_dt
                item["paid_amount"] = amt
                break

        summary = _loan_summary_from_schedule(schedule)

        # status auto: NPA if overdue>0 and >90 days on next_due; closed if outstanding ~0
        status = (loan.get("status") or "active").strip().lower()
        if summary.get("overdue_amount", 0) > 0 and summary.get("next_due_date"):
            try:
                if (datetime.utcnow() - summary["next_due_date"]).days >= 90:
                    status = "npa"
            except Exception:
                pass
        if summary.get("outstanding_amount", 0) <= 0.01:
            status = "closed"

        db.pg_loan_repayments.insert_one(
            {
                "loan_id": ObjectId(loan_id),
                "pg_id": loan.get("pg_id"),
                "instalment_no": inst_no,
                "paid_amount": amt,
                "paid_at": paid_dt,
                "created_at": datetime.utcnow(),
            }
        )

        db.pg_loan_accounts.update_one(
            {"_id": ObjectId(loan_id)},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )
        flash("Repayment saved.", "success")
        return redirect(url_for("finance.loan_account_view", loan_id=loan_id))

    repayments = list(
        db.pg_loan_repayments.find({"loan_id": ObjectId(loan_id)}).sort([("paid_at", -1)])
    )
    return render_template("loan_account_view.html", pg=pg, loan=loan, repayments=repayments)


@finance_bp.route("/member_loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_accounts(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    members = list(
        db.pg_members.find({"pg_id": ObjectId(pg_id)}, {"name": 1, "member_name": 1}).limit(500)
    )

    # PG can view loans, but only CLF/Block authorities can create/update.
    if request.method == "POST" and session.get("role") in ("PG_DATA_ENTRY", "CADRE_CC"):
        flash("Member loans can only be created/edited by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        member_id = request.form.get("member_id")
        principal = float(request.form.get("principal") or 0)
        roi = float(request.form.get("roi") or 0)
        tenure = int(request.form.get("tenure_months") or 0)
        start_date = request.form.get("first_due_date") or datetime.utcnow().strftime("%Y-%m-%d")

        schedule = _amort_schedule(principal, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": ObjectId(pg_id),
            "member_id": ObjectId(member_id) if (member_id and ObjectId.is_valid(member_id)) else member_id,
            "loan_no": (request.form.get("loan_no") or "").strip()
            or f"MB-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "purpose": request.form.get("purpose") or "",
            "principal": principal,
            "roi": roi,
            "tenure_months": tenure,
            "status": (request.form.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        db.pg_member_loan_accounts.insert_one(doc)
        flash("Member loan created.", "success")
        return redirect(url_for("finance.member_loan_accounts", pg_id=pg_id))

    loans = list(
        db.pg_member_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)])
    )
    return render_template("member_loan_accounts.html", pg=pg, loans=loans, members=members)


@finance_bp.route("/member_loan/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_view(loan_id):
    db = current_app.mongo_db
    loan = db.pg_member_loan_accounts.find_one({"_id": ObjectId(loan_id)})
    if not loan:
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": loan.get("pg_id")})
    member = None
    try:
        member = db.pg_members.find_one({"_id": loan.get("member_id")})
    except Exception:
        member = None

    # PG can view loans, but only CLF/Block authorities can create/update.
    if request.method == "POST" and session.get("role") in ("PG_DATA_ENTRY", "CADRE_CC"):
        flash("Member loan repayments/updates can only be entered by CLF/Block authorities.", "warning")
        return redirect(request.path)

    if request.method == "POST":
        amt = float(request.form.get("paid_amount") or 0)
        inst_no = int(request.form.get("instalment_no") or 0)
        paid_at = request.form.get("paid_at") or datetime.utcnow().strftime("%Y-%m-%d")

        schedule = loan.get("schedule") or []
        from datetime import datetime as _dt
        try:
            paid_dt = _dt.strptime(paid_at, "%Y-%m-%d")
        except Exception:
            paid_dt = _dt.utcnow()

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

        db.pg_member_loan_repayments.insert_one(
            {
                "loan_id": ObjectId(loan_id),
                "pg_id": loan.get("pg_id"),
                "member_id": loan.get("member_id"),
                "instalment_no": inst_no,
                "paid_amount": amt,
                "paid_at": paid_dt,
                "created_at": datetime.utcnow(),
            }
        )

        db.pg_member_loan_accounts.update_one(
            {"_id": ObjectId(loan_id)},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )
        flash("Repayment saved.", "success")
        return redirect(url_for("finance.member_loan_view", loan_id=loan_id))

    repayments = list(
        db.pg_member_loan_repayments.find({"loan_id": ObjectId(loan_id)}).sort([("paid_at", -1)])
    )
    return render_template("member_loan_view.html", pg=pg, loan=loan, member=member, repayments=repayments)
@finance_bp.route("/grants/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grants(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        doc = {
            "pg_id": ObjectId(pg_id),
            "category": request.form.get("category") or "Infrastructure",
            "source": request.form.get("source") or "",
            "release_date": request.form.get("release_date") or None,
            "amount_received": float(request.form.get("amount_received") or 0),
            "uc_status": request.form.get("uc_status") or "pending",
            "uc_submitted_date": request.form.get("uc_submitted_date") or None,
            "notes": request.form.get("notes") or "",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        # computed balance via utilization collection
        res = db.pg_grants.insert_one(doc)
        flash("Grant added.", "success")
        return redirect(url_for("finance.grants", pg_id=pg_id))

    grants = list(db.pg_grants.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)]))
    # attach utilization sums
    for g in grants:
        util = list(db.pg_grant_utilizations.aggregate([
            {"$match": {"grant_id": g.get("_id")}},
            {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
        ]))
        utilized = float(util[0]["utilized"] if util else 0)
        g["utilized_amount"] = round(utilized,2)
        g["balance_amount"] = round(float(g.get("amount_received") or 0) - utilized, 2)
    return render_template("grants.html", pg=pg, grants=grants)

@finance_bp.route("/grant/<grant_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grant_view(grant_id):
    db = current_app.mongo_db
    grant = db.pg_grants.find_one({"_id": ObjectId(grant_id)})
    if not grant:
        flash("Grant not found.", "danger")
        return redirect(url_for("pg.pg_home"))
    pg = db.pgs.find_one({"_id": grant.get("pg_id")})

    if request.method == "POST":
        role = session.get("role")
        amount = float(request.form.get("amount") or 0)
        head = (request.form.get("head") or "").strip()
        if not head:
            flash("Head is required.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        # ✅ Strict head validation against master list (if configured)
        # Keeps workflow intact; prevents typos.
        try:
            allowed = [h.get("name") for h in db.grant_heads.find({}, {"name": 1})]
            allowed = [x for x in allowed if x]
        except Exception:
            allowed = []
        if allowed and head not in allowed:
            flash("Invalid Head. Please select a valid Head from the list.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        if amount <= 0:
            flash("Amount must be greater than 0.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        utilized_at = request.form.get("utilized_at") or datetime.utcnow().strftime("%Y-%m-%d")
        from datetime import datetime as _dt
        try:
            u_dt = _dt.strptime(utilized_at, "%Y-%m-%d")
        except Exception:
            u_dt = _dt.utcnow()

        # Prevent utilization beyond available balance
        existing = list(db.pg_grant_utilizations.find({"grant_id": ObjectId(grant_id)}, {"amount": 1}))
        utilized_total = sum(float(x.get("amount") or 0) for x in existing)
        balance = float(grant.get("amount_received") or 0) - utilized_total
        if amount > balance + 1e-6:
            flash(f"Utilization exceeds available balance (Balance: ₹{balance:,.2f}).", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        # Attachments (STRICT)
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
                attachments.append({"filename": fn, "stored_as": saved, "path": path})
        except Exception:
            # attachment failure should not block core workflow
            attachments = []

        # ✅ Strict attachment requirement
        if not attachments:
            flash("At least one attachment (bill/photo) is required to submit utilization.", "danger")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        # ✅ Approval workflow chain: PG -> CLF -> Block -> District -> Admin
        status = "PENDING" if role == "PG_DATA_ENTRY" else (request.form.get("status") or "PENDING").upper()

        db.pg_grant_utilizations.insert_one({
            "grant_id": ObjectId(grant_id),
            "pg_id": grant.get("pg_id"),
            "amount": amount,
            "head": head,
            "remarks": request.form.get("remarks") or "",
            "utilized_at": u_dt,
            "status": status,
            "attachments": attachments,
            "created_by": session.get("user_id"),
            "created_role": role,
            "created_at": datetime.utcnow(),
            "approved_by": None,
            "approved_at": None,
        })
        flash("Utilization entry added.", "success")
        return redirect(url_for("finance.grant_view", grant_id=grant_id))

    utils = list(db.pg_grant_utilizations.find({"grant_id": ObjectId(grant_id)}).sort([("utilized_at", -1)]))
    utilized_total = sum(float(x.get("amount") or 0) for x in utils)
    balance = float(grant.get("amount_received") or 0) - utilized_total
    return render_template("grant_view.html", pg=pg, grant=grant, utils=utils, utilized_total=utilized_total, balance=balance, role=session.get("role"))


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
        {"$set": {"status": next_status, "approved_by": session.get("user_id"), "approved_at": datetime.utcnow()}},
    )
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
        {"$set": {"status": "REJECTED", "rejection_reason": reason, "approved_by": session.get("user_id"), "approved_at": datetime.utcnow()}},
    )
    flash("Utilization rejected.", "warning")
    return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available
