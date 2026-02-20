from flask import render_template, request, redirect, url_for, flash, current_app, session
from bson import ObjectId
from datetime import datetime

from . import finance_bp
from ..rbac import login_required, roles_required

@finance_bp.route("/pg_funds/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
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
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
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
@roles_required("PG_DATA_ENTRY")
def cashbook():
    # UI-only Cash Book screen (no backend logic changes)
    return render_template("cash_book.html")
