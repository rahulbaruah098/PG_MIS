from services.audit_engine import AuditLogger
from flask import render_template, request, redirect, url_for, flash, current_app, session
from app.services.guards import require_unlocked_period
from bson import ObjectId
from datetime import datetime

from . import business_bp
from ..rbac import login_required, roles_required

@business_bp.route("/income_expenditure/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
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


# ============================================================
# NEW: STOCK / INVENTORY TRACKING
# - Opening/closing stock commodity-wise (monthly)
# - Movement ledger (purchase/sale/adjustment)
# ============================================================

@business_bp.route("/stock_register/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def stock_register(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        year = int(request.form.get("year") or datetime.utcnow().year)
        month = int(request.form.get("month") or datetime.utcnow().month)
        commodity = (request.form.get("commodity") or "").strip()
        opening_qty = float(request.form.get("opening_qty") or 0)
        opening_value = float(request.form.get("opening_value") or 0)
        closing_qty = float(request.form.get("closing_qty") or 0)
        closing_value = float(request.form.get("closing_value") or 0)
        valuation_method = request.form.get("valuation_method") or "avg"

        doc = {
            "pg_id": ObjectId(pg_id),
            "year": year,
            "month": month,
            "commodity": commodity,
            "opening_qty": opening_qty,
            "opening_value": opening_value,
            "closing_qty": closing_qty,
            "closing_value": closing_value,
            "valuation_method": valuation_method,
            "updated_at": datetime.utcnow(),
        }
        db.pg_stocks_monthly.update_one(
            {"pg_id": ObjectId(pg_id), "year": year, "month": month, "commodity": commodity},
            {"$set": doc},
            upsert=True
        )
        flash("Stock monthly saved.", "success")
        return redirect(url_for("business.stock_register", pg_id=pg_id))

    records = list(db.pg_stocks_monthly.find({"pg_id": ObjectId(pg_id)}).sort([("year", -1), ("month", -1)]).limit(200))
    movements = list(db.pg_stock_movements.find({"pg_id": ObjectId(pg_id)}).sort([("ts", -1)]).limit(100))
    return render_template("stock_register.html", pg=pg, records=records, movements=movements)

@business_bp.route("/stock_movement/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def stock_movement(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    doc = {
        "pg_id": ObjectId(pg_id),
        "ts": datetime.utcnow(),
        "commodity": (request.form.get("commodity") or "").strip(),
        "movement_type": request.form.get("movement_type") or "purchase",
        "qty": float(request.form.get("qty") or 0),
        "rate": float(request.form.get("rate") or 0),
        "amount": float(request.form.get("qty") or 0) * float(request.form.get("rate") or 0),
        "remarks": request.form.get("remarks") or "",
        "created_at": datetime.utcnow()
    }
    db.pg_stock_movements.insert_one(doc)
    flash("Stock movement added.", "success")
    return redirect(url_for("business.stock_register", pg_id=pg_id))


# ============================================================
# NEW: BUSINESS PLAN TRACKING (planned vs actual)
# ============================================================

@business_bp.route("/business_plan/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def business_plan(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        year = int(request.form.get("year") or datetime.utcnow().year)
        plan = {
            "turnover_target": float(request.form.get("turnover_target") or 0),
            "profit_target": float(request.form.get("profit_target") or 0),
            "member_coverage_target": int(request.form.get("member_coverage_target") or 0),
            "notes": request.form.get("notes") or "",
        }
        doc = {"pg_id": ObjectId(pg_id), "year": year, "plan": plan, "updated_at": datetime.utcnow()}
        db.pg_business_plans.update_one({"pg_id": ObjectId(pg_id), "year": year}, {"$set": doc}, upsert=True)
        flash("Business plan saved.", "success")
        return redirect(url_for("business.business_plan", pg_id=pg_id))

    # latest plan
    plan_doc = db.pg_business_plans.find_one({"pg_id": ObjectId(pg_id)}, sort=[("year", -1)])
    # actuals from market transactions (sum year)
    actual_turnover = 0.0
    if plan_doc:
        y = int(plan_doc.get("year") or datetime.utcnow().year)
        agg = list(db.pg_market_transactions.aggregate([
            {"$match": {"pg_id": ObjectId(pg_id), "year": y}},
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

@business_bp.route("/monthly_business/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def monthly_business(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # default period = current month
    now = datetime.utcnow()
    year = int(request.args.get("year") or request.form.get("year") or now.year)
    month = int(request.args.get("month") or request.form.get("month") or now.month)

    if request.method == "POST":
        # ---- 6.1 internal/member ----
        internal = {
            "pg_id": ObjectId(pg_id),
            "year": year,
            "month": month,

            # Value totals (₹)
            "input_sold_to_members_value": float(request.form.get("input_sold_to_members_value") or 0),
            "product_bought_from_members_value": float(request.form.get("product_bought_from_members_value") or 0),
            "other_internal_value": float(request.form.get("other_internal_value") or 0),

            # Member involvement (counts)
            "members_input_count": int(request.form.get("members_input_count") or 0),
            "members_output_count": int(request.form.get("members_output_count") or 0),

            # Optional notes
            "notes": request.form.get("internal_notes") or "",
            "updated_at": datetime.utcnow(),
        }
        internal_total = float(internal["input_sold_to_members_value"]) + float(internal["product_bought_from_members_value"]) + float(internal["other_internal_value"])
        internal["internal_total"] = round(internal_total, 2)

        db.pg_business_monthly.update_one(
            {"pg_id": ObjectId(pg_id), "year": year, "month": month},
            {"$set": internal},
            upsert=True,
        )

        # ---- 6.2 market/outsider ----
        market = {
            "pg_id": ObjectId(pg_id),
            "year": year,
            "month": month,

            "input_procured_from_market_value": float(request.form.get("input_procured_from_market_value") or 0),
            "input_sold_to_outsiders_value": float(request.form.get("input_sold_to_outsiders_value") or 0),
            "product_sold_to_market_value": float(request.form.get("product_sold_to_market_value") or 0),
            "other_market_value": float(request.form.get("other_market_value") or 0),

            "notes": request.form.get("market_notes") or "",
            "updated_at": datetime.utcnow(),
        }
        market_total = float(market["input_procured_from_market_value"]) + float(market["input_sold_to_outsiders_value"]) + float(market["product_sold_to_market_value"]) + float(market["other_market_value"])
        market["market_total"] = round(market_total, 2)

        total_turnover = KPIEngine.calculate_turnover(internal["internal_total"], market["market_total"])
        market["total_turnover"] = total_turnover

        db.pg_market_transactions.update_one(
            {"pg_id": ObjectId(pg_id), "year": year, "month": month},
            {"$set": market},
            upsert=True,
        )

        flash("Monthly business (6.1 + 6.2) saved. Turnover auto-calculated.", "success")

        # Best-effort: auto-generate MPR snapshot for this month
        try:
            from app.services.workflow import generate_mpr_snapshot
            user = {"user_id": session.get("user_id"), "username": session.get("username"), "role": session.get("role")}
            generate_mpr_snapshot(db, level="pg", ref_id=str(pg_id), year=year, month=month, user=user)
        except Exception:
            pass

        return redirect(url_for("business.monthly_business", pg_id=pg_id, year=year, month=month))

    internal_doc = db.pg_business_monthly.find_one({"pg_id": ObjectId(pg_id), "year": year, "month": month}) or {}
    market_doc = db.pg_market_transactions.find_one({"pg_id": ObjectId(pg_id), "year": year, "month": month}) or {}
    turnover = KPIEngine.calculate_turnover(float(internal_doc.get("internal_total") or 0), float(market_doc.get("market_total") or market_doc.get("total_turnover") or 0))

    # helpful derived % for view
    total_members = db.pg_members.count_documents({"pg_id": ObjectId(pg_id)})
    pct_in = KPIEngine.calculate_member_percentage(int(internal_doc.get("members_input_count") or 0), int(total_members or 0))
    pct_out = KPIEngine.calculate_member_percentage(int(internal_doc.get("members_output_count") or 0), int(total_members or 0))

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

@business_bp.route("/income_expenditure_year/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def income_expenditure_year(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    now = datetime.utcnow()
    year = int(request.args.get("year") or now.year)

    rows = list(db.pg_income_expenditure.find({"pg_id": ObjectId(pg_id), "year": year}).sort([("month", 1)]))

    # Aggregate totals across months
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
