from flask import render_template, request, redirect, url_for, flash, current_app
from bson import ObjectId
from datetime import datetime

from . import business_bp
from ..rbac import login_required, roles_required

@business_bp.route("/income_expenditure/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def income_expenditure(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        year = int(request.form.get("year"))
        month = int(request.form.get("month"))

        income = {
            "startup_cost_received": float(request.form.get("startup_cost_received") or 0),
            "membership_fees": float(request.form.get("membership_fees") or 0),
            "interest_received_from_members": float(request.form.get("interest_received_from_members") or 0),
            "income_selling_product": float(request.form.get("income_selling_product") or 0),
            "income_selling_input_to_member": float(request.form.get("income_selling_input_to_member") or 0),
            "income_selling_input_to_outsider": float(request.form.get("income_selling_input_to_outsider") or 0),
            "other_income": float(request.form.get("other_income") or 0),
        }
        expenditure = {
            "establishment_cost_utilized": float(request.form.get("establishment_cost_utilized") or 0),
            "input_procurement": float(request.form.get("input_procurement") or 0),
            "interest_paid_against_loan": float(request.form.get("interest_paid_against_loan") or 0),
            "product_procurement": float(request.form.get("product_procurement") or 0),
            "recurring_expenditure": float(request.form.get("recurring_expenditure") or 0),
            "other_expenditure": float(request.form.get("other_expenditure") or 0),
        }

        total_income = sum(income.values())
        total_expenditure = sum(expenditure.values())
        excess_income = total_income - total_expenditure

        doc = {
            "pg_id": ObjectId(pg_id),
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
            {"pg_id": ObjectId(pg_id), "year": year, "month": month},
            {"$set": doc},
            upsert=True,
        )
        flash("Income–Expenditure saved.", "success")
        return redirect(url_for("business.income_expenditure", pg_id=pg_id))

    record = db.pg_income_expenditure.find_one({"pg_id": ObjectId(pg_id)}, sort=[("year", -1), ("month", -1)])
    return render_template("income_expenditure.html", pg=pg, record=record)
