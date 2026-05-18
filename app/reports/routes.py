from services.audit_engine import AuditLogger
import os
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app, send_file
from datetime import datetime, timedelta
from app.services.guards import require_unlocked_period
from bson import ObjectId
from . import reports_bp
from ..rbac import login_required, roles_required
from ..rbac import permissions_required
from ..permissions import P_CHANGE_APPROVE, P_MPR_GENERATE, P_REPORT_EXPORT, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW
from ..services.workflow import decide_change_request, generate_mpr_snapshot
from openpyxl import Workbook
try:
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None
from app.services.filter_engine import FilterEngine
from app.services.report_service import ReportService
from app.services.kpi_service import KPIService
from app.utils import safe_objectid
from app.services.submissions import submit_to_clf, approve, reject, get_submission




def _geo_names_from_session(db, sess):
    """Resolve state/district/block names (strings) for SHG master filtering.

    The imported LokOS SHG master stores geo as strings (e.g., TRIPURA/DHALAI/AMBASSA),
    while app users store geo as ObjectId references into states/districts/blocks/clfs.
    """
    state_name = district_name = block_name = None

    state_id = sess.get("state_id")
    district_id = sess.get("district_id")
    block_id = sess.get("block_id")
    clf_id = sess.get("clf_id")

    try:
        if clf_id and not block_id:
            clf = db.clfs.find_one({"_id": ObjectId(clf_id)}, {"block_id": 1})
            if clf and clf.get("block_id"):
                block_id = str(clf["block_id"])
        if block_id and not district_id:
            blk = db.blocks.find_one({"_id": ObjectId(block_id)}, {"district_id": 1})
            if blk and blk.get("district_id"):
                district_id = str(blk["district_id"])
        if district_id and not state_id:
            dist = db.districts.find_one({"_id": ObjectId(district_id)}, {"state_id": 1})
            if dist and dist.get("state_id"):
                state_id = str(dist["state_id"])

        if state_id:
            st = db.states.find_one({"_id": ObjectId(state_id)}, {"name": 1, "code": 1})
            if st:
                # Prefer 'name' because LokOS exports typically use full name (e.g., TRIPURA)
                state_name = st.get("name") or st.get("code")

        if district_id:
            dist = db.districts.find_one({"_id": ObjectId(district_id)}, {"name": 1})
            if dist:
                district_name = dist.get("name")

        if block_id:
            blk = db.blocks.find_one({"_id": ObjectId(block_id)}, {"name": 1})
            if blk:
                block_name = blk.get("name")
    except Exception:
        # If resolution fails, just return None values; dashboards will fall back to overall counts.
        return None, None, None

    return state_name, district_name, block_name

# changes by atlanta
@reports_bp.route("/hub", methods=["GET"])
@login_required
@roles_required(
    "PG_DATA_ENTRY",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
    "CADRE_CC",
)
def reports_hub():
    """Unified download hub for reports (CSV/ZIP)."""

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if wants_json:
        return jsonify({
            "success": True,
            "title": "Reports Hub",
            "subtitle": "Download CSV/ZIP reports with your jurisdiction filters.",
            "filters": {
                "state": request.args.get("state", ""),
                "district": request.args.get("district", ""),
                "block": request.args.get("block", ""),
                "gp": request.args.get("gp", ""),
                "village": request.args.get("village", ""),
                "pg_name": request.args.get("pg_name", ""),
                "period": request.args.get("period", ""),
                "group_by": request.args.get("group_by", ""),
                "q": request.args.get("q", ""),
                "from": request.args.get("from", ""),
                "to": request.args.get("to", ""),
            },
            "period_options": [
                {"label": "All time", "value": ""},
                {"label": "Weekly", "value": "weekly"},
                {"label": "Monthly", "value": "monthly"},
                {"label": "6 Monthly", "value": "six_monthly"},
                {"label": "Yearly", "value": "yearly"},
            ],
            "group_by_options": [
                {"label": "None", "value": ""},
                {"label": "District", "value": "district"},
                {"label": "Block", "value": "block"},
                {"label": "Gram Panchayat", "value": "gp"},
                {"label": "Village", "value": "village"},
                {"label": "PG", "value": "pg"},
            ],
            "actions": {
                "open_overall": "/reports/pg-overall",
                "overall_zip": "/reports/pg-overall/download",
                "lakhpati_csv": "/reports/export/lakhpati.csv",
                "grants_csv": "/reports/export/grants.csv",
                "members_csv": "/reports/export/members.csv",
                "cashbook_csv": "/reports/export/cashbook.csv",
                "loans_csv": "/reports/export/loans.csv",
                "turnover_csv": "/reports/export/turnover.csv",
            },
            "quick_reports": [
                {
                    "title": "Lakhpati Didi",
                    "subtitle": "Member-level + grouped summary",
                    "path": "/reports/lakhpati",
                },
                {
                    "title": "Grants & Utilization",
                    "subtitle": "Grant received + utilization balance",
                    "path": "/reports/grants",
                },
                {
                    "title": "PG Overall Export",
                    "subtitle": "Everything about selected PGs (ZIP)",
                    "path": "/reports/pg-overall",
                },
            ],
            "tip": "Tip: Set Group By for summary tables. If Group By is blank, downloads give row-level data.",
        })

    return render_template("reports_hub.html")


# -------------------------------------------------------------------
# Reports Hub Dropdown Backend
# -------------------------------------------------------------------

@reports_bp.route("/hub/filter-options", methods=["GET"])
@login_required
@roles_required(
    "PG_DATA_ENTRY",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
    "CADRE_CC",
)
def reports_hub_filter_options():
    db = current_app.mongo_db

    level = (request.args.get("level") or "").strip()

    state = (request.args.get("state") or "").strip()
    district = (request.args.get("district") or "").strip()
    block = (request.args.get("block") or "").strip()
    gp = (request.args.get("gp") or "").strip()
    village = (request.args.get("village") or "").strip()

    allowed_levels = {"state", "district", "block", "gp", "village", "pg"}

    if level not in allowed_levels:
        return jsonify({
            "success": False,
            "message": "Invalid dropdown level.",
            "options": [],
        }), 400

    # This matches your PG creation insert:
    #
    # pg_insert = {
    #   "name": pg_name,
    #   "State": st,
    #   "District": dist_name,
    #   "Block": blk_name,
    #   "Gram Panchayat": gp,
    #   "Village": village,
    #   ...
    # }

    field_map = {
        "state": "State",
        "district": "District",
        "block": "Block",
        "gp": "Gram Panchayat",
        "village": "Village",
        "pg": "name",
    }

    mongo_filter = {}

    # Respect logged-in user's jurisdiction
    try:
        base_match = _pg_match_from_session(session)
        if base_match:
            mongo_filter.update(base_match)
    except Exception:
        pass

    # Apply selected dropdown filters
    if state:
        mongo_filter["State"] = state

    if district:
        mongo_filter["District"] = district

    if block:
        mongo_filter["Block"] = block

    if gp:
        mongo_filter["Gram Panchayat"] = gp

    if village:
        mongo_filter["Village"] = village

    selected_field = field_map[level]

    try:
        rows = db.pgs.distinct(selected_field, mongo_filter)

        options = sorted([
            str(row).strip()
            for row in rows
            if row is not None and str(row).strip()
        ])

        return jsonify({
            "success": True,
            "level": level,
            "options": options,
        })

    except Exception as e:
        current_app.logger.exception("Reports Hub dropdown loading failed")

        return jsonify({
            "success": False,
            "message": str(e),
            "level": level,
            "options": [],
        }), 500







def _apply_period(match: dict, field: str, period_filters: dict):
    start, end = FilterEngine.period_range(period_filters)
    if start and end:
        match[field] = {"$gte": start, "$lt": end}
    elif start:
        match[field] = {"$gte": start}
    elif end:
        match[field] = {"$lt": end}
    return match


def _pgs_in_scope(db, base_match: dict, filters: dict):
    match_pg = _pg_match_with_filters(db, base_match, filters)

    role = session.get("role")

    if role in ("PG_DATA_ENTRY", "CADRE_CC") and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(db.pgs.find(
        match_pg,
        {
            "name": 1,
            "State": 1,
            "District": 1,
            "Block": 1,
            "Gram Panchayat": 1,
            "Village": 1,
        }
    ))

    pg_by_id = {p["_id"]: p for p in pgs}

    return list(pg_by_id.keys()), pg_by_id





@reports_bp.route("/export/members.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_members_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    group_by = (request.args.get("group_by") or "").strip().lower()
    q = (request.args.get("q") or "").strip()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)

    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    if q:
        match["$or"] = [
            {"name": {"$regex": re.escape(q), "$options": "i"}},
            {"shg_name": {"$regex": re.escape(q), "$options": "i"}},
        ]

    # period filter (created_at)
    _apply_period(match, "created_at", dict(request.args))

    mem = io.StringIO()
    w = csv.writer(mem)

    if group_by:
        # Summary aggregation
        def gkey(pg):
            if group_by == "district":
                return pg.get("District") or ""
            if group_by == "block":
                return pg.get("Block") or ""
            if group_by == "gp":
                return pg.get("Gram Panchayat") or ""
            if group_by == "village":
                return pg.get("Village") or ""
            if group_by == "pg":
                return pg.get("name") or ""
            return ""

        agg = {}
        for mdoc in db.pg_members.find(match, {"pg_id": 1, "lakh_pati_didi": 1}):
            pg = pg_by_id.get(mdoc.get("pg_id")) or {}
            key = gkey(pg)
            if key not in agg:
                agg[key] = {"members": 0, "lakhpati": 0}
            agg[key]["members"] += 1
            if mdoc.get("lakh_pati_didi"):
                agg[key]["lakhpati"] += 1

        w.writerow(["Group", "Members Count", "Lakhpati Count"])
        for k in sorted(agg.keys()):
            w.writerow([k, agg[k]["members"], agg[k]["lakhpati"]])
    else:
        # Row-level export
        fields = ["PG Name","State","District","Block","Gram Panchayat","Village","Member Name","SHG Name","Category","Contact","Lakhpati Didi"]
        w.writerow(fields)
        for mdoc in db.pg_members.find(match).sort([("name", 1)]):
            pg = pg_by_id.get(mdoc.get("pg_id")) or {}
            w.writerow([
                pg.get("name") or "",
                pg.get("State") or "",
                pg.get("District") or "",
                pg.get("Block") or "",
                pg.get("Gram Panchayat") or "",
                pg.get("Village") or "",
                mdoc.get("name") or "",
                mdoc.get("shg_name") or "",
                mdoc.get("category") or "",
                mdoc.get("contact") or "",
                "Yes" if mdoc.get("lakh_pati_didi") else "No",
            ])

    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=members.csv"})


@reports_bp.route("/export/cashbook.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_cashbook_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    group_by = (request.args.get("group_by") or "").strip().lower()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)

    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    _apply_period(match, "updated_at", dict(request.args))

    mem = io.StringIO(); w = csv.writer(mem)

    def totals(arr):
        t = 0.0
        if isinstance(arr, list):
            for r in arr:
                try:
                    t += float((r or {}).get("amount") or 0)
                except Exception:
                    pass
        return t

    if group_by:
        def gkey(pg):
            if group_by == "district": return pg.get("District") or ""
            if group_by == "block": return pg.get("Block") or ""
            if group_by == "gp": return pg.get("Gram Panchayat") or ""
            if group_by == "village": return pg.get("Village") or ""
            if group_by == "pg": return pg.get("name") or ""
            return ""
        agg = {}
        for doc in db.pg_cashbooks.find(match, {"pg_id": 1, "receipts": 1, "payments": 1}):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            key = gkey(pg)
            if key not in agg:
                agg[key] = {"receipt_total": 0.0, "payment_total": 0.0, "entries": 0}
            agg[key]["receipt_total"] += totals(doc.get("receipts"))
            agg[key]["payment_total"] += totals(doc.get("payments"))
            agg[key]["entries"] += 1
        w.writerow(["Group","Cashbook Entries","Receipt Total","Payment Total"])
        for k in sorted(agg.keys()):
            w.writerow([k, agg[k]["entries"], round(agg[k]["receipt_total"],2), round(agg[k]["payment_total"],2)])
    else:
        w.writerow(["PG Name","State","District","Block","GP","Village","Year","Month","Receipt Total","Payment Total","Balanced?"])
        for doc in db.pg_cashbooks.find(match).sort([("year",-1),("month",-1)]):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            rt = totals(doc.get("receipts"))
            pt = totals(doc.get("payments"))
            w.writerow([
                pg.get("name") or "",
                pg.get("State") or "",
                pg.get("District") or "",
                pg.get("Block") or "",
                pg.get("Gram Panchayat") or "",
                pg.get("Village") or "",
                doc.get("year") or "",
                doc.get("month") or "",
                round(rt,2),
                round(pt,2),
                "YES" if abs(rt-pt) < 0.01 else "NO",
            ])

    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=cashbook.csv"})


@reports_bp.route("/export/loans.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_loans_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    group_by = (request.args.get("group_by") or "").strip().lower()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)

    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    _apply_period(match, "created_at", dict(request.args))

    mem = io.StringIO(); w = csv.writer(mem)

    if group_by:
        def gkey(pg):
            if group_by == "district": return pg.get("District") or ""
            if group_by == "block": return pg.get("Block") or ""
            if group_by == "gp": return pg.get("Gram Panchayat") or ""
            if group_by == "village": return pg.get("Village") or ""
            if group_by == "pg": return pg.get("name") or ""
            return ""
        agg = {}
        for doc in db.pg_member_loan_accounts.find(match, {"pg_id": 1, "principal_amount": 1, "outstanding_amount": 1, "status": 1}):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            key = gkey(pg)
            if key not in agg:
                agg[key] = {"loans": 0, "principal": 0.0, "outstanding": 0.0, "active": 0}
            agg[key]["loans"] += 1
            try: agg[key]["principal"] += float(doc.get("principal_amount") or 0)
            except Exception: pass
            try: agg[key]["outstanding"] += float(doc.get("outstanding_amount") or 0)
            except Exception: pass
            if (doc.get("status") or "").lower() in ("active","open"):
                agg[key]["active"] += 1
        w.writerow(["Group","Loans","Active Loans","Principal Total","Outstanding Total"])
        for k in sorted(agg.keys()):
            w.writerow([k, agg[k]["loans"], agg[k]["active"], round(agg[k]["principal"],2), round(agg[k]["outstanding"],2)])
    else:
        w.writerow(["PG Name","State","District","Block","GP","Village","Loan No","Member","Principal","Outstanding","Status","Created At"])
        for doc in db.pg_member_loan_accounts.find(match).sort([("created_at",-1)]):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            w.writerow([
                pg.get("name") or "",
                pg.get("State") or "",
                pg.get("District") or "",
                pg.get("Block") or "",
                pg.get("Gram Panchayat") or "",
                pg.get("Village") or "",
                doc.get("loan_no") or "",
                doc.get("member_name") or "",
                doc.get("principal_amount") or 0,
                doc.get("outstanding_amount") or 0,
                doc.get("status") or "",
                (doc.get("created_at").strftime("%Y-%m-%d") if doc.get("created_at") else ""),
            ])

    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=loans.csv"})


@reports_bp.route("/export/turnover.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_turnover_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    group_by = (request.args.get("group_by") or "").strip().lower()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)

    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    # Monthly dataset: use year/month based filtering if user uses from/to
    _apply_period(match, "updated_at", dict(request.args))

    mem = io.StringIO(); w = csv.writer(mem)

    def tv(doc):
        v = doc.get("turnover")
        if v is None: v = doc.get("total_turnover")
        try: return float(v or 0)
        except Exception: return 0.0

    if group_by:
        def gkey(pg):
            if group_by == "district": return pg.get("District") or ""
            if group_by == "block": return pg.get("Block") or ""
            if group_by == "gp": return pg.get("Gram Panchayat") or ""
            if group_by == "village": return pg.get("Village") or ""
            if group_by == "pg": return pg.get("name") or ""
            return ""
        agg = {}
        for doc in db.pg_business_monthly.find(match, {"pg_id": 1, "turnover": 1, "total_turnover": 1}):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            key = gkey(pg)
            if key not in agg:
                agg[key] = {"turnover": 0.0, "months": 0}
            agg[key]["turnover"] += tv(doc)
            agg[key]["months"] += 1
        w.writerow(["Group","Months","Turnover Total"])
        for k in sorted(agg.keys()):
            w.writerow([k, agg[k]["months"], round(agg[k]["turnover"],2)])
    else:
        w.writerow(["PG Name","State","District","Block","GP","Village","Year","Month","Turnover"])
        for doc in db.pg_business_monthly.find(match).sort([("year",-1),("month",-1)]):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            w.writerow([
                pg.get("name") or "",
                pg.get("State") or "",
                pg.get("District") or "",
                pg.get("Block") or "",
                pg.get("Gram Panchayat") or "",
                pg.get("Village") or "",
                doc.get("year") or "",
                doc.get("month") or "",
                round(tv(doc),2),
            ])

    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=turnover.csv"})


def _shg_filter_from_session(db, sess):
    """Build a Mongo query for shg_master scoped to the user's jurisdiction."""
    role = sess.get("role")
    if role in ("SUPER_ADMIN",):
        return {}

    state_name, district_name, block_name = _geo_names_from_session(db, sess)
    q = {}
    if state_name:
        q["State"] = state_name
    if district_name:
        q["District"] = district_name
    if block_name:
        q["Block"] = block_name
    return q


def _pg_match_from_session(sess):
    """Build a Mongo query for PGs scoped to the user's jurisdiction."""
    role = sess.get("role")
    state_id = sess.get("state_id")
    district_id = sess.get("district_id")
    block_id = sess.get("block_id")
    clf_id = sess.get("clf_id")

    q = {}
    try:
        if clf_id:
            q["clf_id"] = ObjectId(clf_id)
        elif block_id:
            q["block_id"] = ObjectId(block_id)
        elif district_id:
            q["district_id"] = ObjectId(district_id)
        elif state_id and role in ("ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN", "CLF_ADMIN", "CLF_MANAGER"):
            q["state_id"] = ObjectId(state_id)
    except Exception:
        return {}
    return q


def _aggregate_count(collection, pipeline):
    try:
        res = list(collection.aggregate(pipeline))
        return int(res[0]["count"]) if res else 0
    except Exception:
        return 0


def _top_counts(db, *, group_field, match_pg=None, limit=10):
    """Top bucket counts for PGs grouped by a PG geo field."""
    match_pg = match_pg or {}
    group_id = f"${group_field}"
    pipe = []
    if match_pg:
        pipe.append({"$match": match_pg})
    pipe += [
        {"$group": {"_id": group_id, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": int(limit)},
    ]
    buckets = list(db.pgs.aggregate(pipe))

    out = []
    for b in buckets:
        _id = b.get("_id")
        name = "Unknown" if _id is None else str(_id)
        if _id is not None:
            try:
                if group_field == "state_id":
                    doc = db.states.find_one({"_id": _id}, {"name": 1, "code": 1}) or {}
                    name = doc.get("name") or doc.get("code") or name
                elif group_field == "district_id":
                    doc = db.districts.find_one({"_id": _id}, {"name": 1}) or {}
                    name = doc.get("name") or name
                elif group_field == "block_id":
                    doc = db.blocks.find_one({"_id": _id}, {"name": 1}) or {}
                    name = doc.get("name") or name
                elif group_field == "clf_id":
                    doc = db.clfs.find_one({"_id": _id}, {"name": 1}) or {}
                    name = doc.get("name") or name
            except Exception:
                pass
        out.append({"name": name, "count": int(b.get("count") or 0)})
    return out


def _turnover_timeseries(db, *, match_pg=None, months=12):
    """Aggregate turnover over the last N months for PGs in scope."""
    match_pg = match_pg or {}

    base_pipe = [
        {"$lookup": {"from": "pgs", "localField": "pg_id", "foreignField": "_id", "as": "pg"}},
        {"$unwind": "$pg"},
    ]
    if match_pg:
        base_pipe.append({"$match": {f"pg.{k}": v for k, v in match_pg.items()}})

    latest = list(db.pg_market_transactions.aggregate(base_pipe + [
        {"$group": {"_id": {"year": "$year", "month": "$month"}}},
        {"$sort": {"_id.year": -1, "_id.month": -1}},
        {"$limit": 1},
    ]))
    if not latest:
        return {"labels": [], "values": [], "latest_label": None, "latest_value": 0}

    y = int(latest[0]["_id"]["year"])
    m = int(latest[0]["_id"]["month"])

    ym = []
    cy, cm = y, m
    for _ in range(int(months)):
        ym.append((cy, cm))
        cm -= 1
        if cm <= 0:
            cm = 12
            cy -= 1

    or_match = [{"year": yy, "month": mm} for yy, mm in ym]

    series = list(db.pg_market_transactions.aggregate(base_pipe + [
        {"$match": {"$or": or_match}},
        {"$group": {"_id": {"year": "$year", "month": "$month"}, "turnover": {"$sum": "$total_turnover"}}},
        {"$sort": {"_id.year": 1, "_id.month": 1}},
    ]))

    smap = {f"{int(s['_id']['year'])}-{int(s['_id']['month'])}": float(s.get('turnover') or 0) for s in series}
    ym_rev = list(reversed(ym))
    labels = [f"{yy}-{mm:02d}" for yy, mm in ym_rev]
    values = [round(float(smap.get(f"{yy}-{mm}", 0)), 2) for yy, mm in ym_rev]

    latest_label = f"{y}-{m:02d}"
    latest_value = round(float(smap.get(f"{y}-{m}", 0)), 2)
    return {"labels": labels, "values": values, "latest_label": latest_label, "latest_value": latest_value}



def _state_dashboard_pg_row(db, pg):
    """Small display row for PG detail tables on the state dashboard."""
    def _name(coll, oid):
        try:
            if oid:
                doc = db[coll].find_one({"_id": oid}, {"name": 1, "code": 1}) or {}
                return doc.get("name") or doc.get("code") or ""
        except Exception:
            pass
        return ""

    pg_id = str(pg.get("_id") or "")

    return {
        "id": pg_id,
        "name": pg.get("name") or pg.get("pg_name") or "-",
        "sector": pg.get("sector") or "-",
        "district": pg.get("District") or _name("districts", pg.get("district_id")) or "-",
        "block": pg.get("Block") or _name("blocks", pg.get("block_id")) or "-",
        "village": pg.get("Village") or pg.get("village") or "-",
        "gp": pg.get("Gram Panchayat") or pg.get("gp") or "-",
        "member_details_url": url_for("reports.state_dashboard_pg_members", pg_id=pg_id) if pg_id else "",
    }


def _state_dashboard_build_details(db, pg_match, pg_ids, detail_key, search_query="", preview_limit=None):
    """Build click-through details for State Dashboard KPI cards.

    search_query:
      Optional search text for PG details.

    preview_limit:
      - None = show all rows
      - 1 = show only first row in dashboard preview
    """
    detail_key = (detail_key or "").strip()
    search_query = (search_query or "").strip()

    if not detail_key:
        return None

    pg_projection = {
        "name": 1,
        "pg_name": 1,
        "sector": 1,
        "District": 1,
        "Block": 1,
        "Village": 1,
        "Gram Panchayat": 1,
        "district_id": 1,
        "block_id": 1,
    }

    def _pg_search_filter():
        if not search_query:
            return None

        safe_q = re.escape(search_query)
        return {
            "$or": [
                {"name": {"$regex": safe_q, "$options": "i"}},
                {"pg_name": {"$regex": safe_q, "$options": "i"}},
                {"sector": {"$regex": safe_q, "$options": "i"}},
                {"District": {"$regex": safe_q, "$options": "i"}},
                {"Block": {"$regex": safe_q, "$options": "i"}},
                {"Village": {"$regex": safe_q, "$options": "i"}},
                {"Gram Panchayat": {"$regex": safe_q, "$options": "i"}},
            ]
        }

    def _apply_preview(rows):
        total_count = len(rows)
        limited_rows = rows

        if preview_limit is not None:
            try:
                limit_value = int(preview_limit)
            except Exception:
                limit_value = 1

            if limit_value > 0:
                limited_rows = rows[:limit_value]

        return limited_rows, total_count

    if detail_key == "cadres":
        cadre_match = {"role": "CADRE_CC"}

        if pg_match.get("state_id"):
            cadre_match["state_id"] = pg_match["state_id"]
        if pg_match.get("district_id"):
            cadre_match["district_id"] = pg_match["district_id"]
        if pg_match.get("block_id"):
            cadre_match["block_id"] = pg_match["block_id"]

        cadres = list(
            db.users.find(
                cadre_match,
                {
                    "name": 1,
                    "username": 1,
                    "phone": 1,
                    "contact": 1,
                    "assigned_pg_ids": 1,
                },
            ).sort("name", 1)
        )

        rows = []
        pg_id_set = {str(x) for x in pg_ids}

        for c in cadres:
            assigned_ids = []

            for raw_id in (c.get("assigned_pg_ids") or []):
                try:
                    oid = raw_id if isinstance(raw_id, ObjectId) else ObjectId(str(raw_id))
                    if str(oid) in pg_id_set:
                        assigned_ids.append(oid)
                except Exception:
                    pass

            assigned_pgs = (
                list(db.pgs.find({"_id": {"$in": assigned_ids}}, pg_projection).sort("name", 1))
                if assigned_ids
                else []
            )

            rows.append({
                "name": c.get("name") or c.get("username") or "-",
                "username": c.get("username") or "-",
                "contact": c.get("phone") or c.get("contact") or c.get("contact_number") or "-",
                "assigned_count": len(assigned_pgs),
                "assigned_pgs": [_state_dashboard_pg_row(db, pg) for pg in assigned_pgs],
            })

        limited_rows, total_count = _apply_preview(rows)

        return {
            "type": "cadres",
            "title": "Cadre Details",
            "rows": limited_rows,
            "total_count": total_count,
            "shown_count": len(limited_rows),
            "has_more": total_count > len(limited_rows),
            "search_query": search_query,
            "detail_key": detail_key,
            "full_url": url_for("reports.state_dashboard_detail_full", detail=detail_key, q=search_query),
        }

    if detail_key.startswith("sector:"):
        sector = detail_key.split(":", 1)[1].strip()

        q = dict(pg_match)
        q["sector"] = sector

        search_filter = _pg_search_filter()
        if search_filter:
            q["$and"] = [search_filter]

        pgs = list(
            db.pgs.find(q, pg_projection).sort([
                ("District", 1),
                ("Block", 1),
                ("Village", 1),
                ("name", 1),
            ])
        )

        rows = [_state_dashboard_pg_row(db, pg) for pg in pgs]
        limited_rows, total_count = _apply_preview(rows)

        return {
            "type": "pgs",
            "title": f"{sector} Based PG Details",
            "rows": limited_rows,
            "total_count": total_count,
            "shown_count": len(limited_rows),
            "has_more": total_count > len(limited_rows),
            "search_query": search_query,
            "detail_key": detail_key,
            "full_url": url_for("reports.state_dashboard_detail_full", detail=detail_key, q=search_query),
        }

    if detail_key.startswith("ffs:"):
        module = detail_key.split(":", 1)[1].strip()

        agri_pg_match = dict(pg_match)
        agri_pg_match["sector"] = "Agri"

        agri_pg_ids = [p["_id"] for p in db.pgs.find(agri_pg_match, {"_id": 1})]

        module_pg_ids = (
            db.pg_members.distinct(
                "pg_id",
                {
                    "pg_id": {"$in": agri_pg_ids},
                    "agri_ffs_module": module,
                },
            )
            if agri_pg_ids
            else []
        )

        if module_pg_ids:
            q = {
                "_id": {"$in": module_pg_ids},
                "sector": "Agri",
            }

            search_filter = _pg_search_filter()
            if search_filter:
                q["$and"] = [search_filter]

            pgs = list(
                db.pgs.find(q, pg_projection).sort([
                    ("District", 1),
                    ("Block", 1),
                    ("Village", 1),
                    ("name", 1),
                ])
            )
        else:
            pgs = []

        rows = [_state_dashboard_pg_row(db, pg) for pg in pgs]
        limited_rows, total_count = _apply_preview(rows)

        return {
            "type": "pgs",
            "title": f"PGs Following {module}",
            "rows": limited_rows,
            "total_count": total_count,
            "shown_count": len(limited_rows),
            "has_more": total_count > len(limited_rows),
            "search_query": search_query,
            "detail_key": detail_key,
            "full_url": url_for("reports.state_dashboard_detail_full", detail=detail_key, q=search_query),
        }

    return None


def _num(value):
    """
    Safe number converter for dashboard charts.
    Keeps chart rendering safe even if MongoDB returns None/string values.
    """
    try:
        if value is None:
            return 0
        return float(value)
    except Exception:
        return 0


def _state_dashboard_chart_pack(
    states_total=0,
    districts_total=0,
    blocks_total=0,
    cadre_count=0,
    pg_count=0,
    member_count=0,
    total_turnover=0,
    total_profit=0,
    total_loss=0,
    pgs_with_grants=0,
    grants_total=0,
    lakhpati_total=0,
    shg_total=0,
    shg_active=0,
    sector_summary=None,
    ffs_summary=None,
):
    """
    Builds all chart datasets for State Dashboard KPI graphs.

    This only prepares data for frontend charts.
    It does not modify any existing KPI calculation or database logic.
    """

    sector_summary = sector_summary or []
    ffs_summary = ffs_summary or []

    kpi_labels = [
        "States",
        "Districts",
        "Blocks",
        "Cadres",
        "PGs",
        "Members",
        "Turnover",
        "Profit",
        "Loss",
        "PGs with Grants",
        "Grants Received",
        "Lakhpati Didi",
        "SHG Total",
        "SHG Active",
    ]

    kpi_values = [
        _num(states_total),
        _num(districts_total),
        _num(blocks_total),
        _num(cadre_count),
        _num(pg_count),
        _num(member_count),
        _num(total_turnover),
        _num(total_profit),
        _num(total_loss),
        _num(pgs_with_grants),
        _num(grants_total),
        _num(lakhpati_total),
        _num(shg_total),
        _num(shg_active),
    ]

    geography_labels = ["States", "Districts", "Blocks"]
    geography_values = [
        _num(states_total),
        _num(districts_total),
        _num(blocks_total),
    ]

    people_labels = ["Cadres", "PGs", "Members", "Lakhpati Didi"]
    people_values = [
        _num(cadre_count),
        _num(pg_count),
        _num(member_count),
        _num(lakhpati_total),
    ]

    finance_labels = ["Latest Turnover", "Total Profit", "Total Loss", "Grants Received"]
    finance_values = [
        _num(total_turnover),
        _num(total_profit),
        _num(total_loss),
        _num(grants_total),
    ]

    grant_labels = ["PGs with Grants", "Total PGs"]
    grant_values = [
        _num(pgs_with_grants),
        _num(pg_count),
    ]

    shg_labels = ["SHG Total", "SHG Active"]
    shg_values = [
        _num(shg_total),
        _num(shg_active),
    ]

    sector_labels = [str(item.get("label") or item.get("key") or "Unknown") for item in sector_summary]
    sector_values = [_num(item.get("count")) for item in sector_summary]

    ffs_labels = [str(item.get("label") or item.get("key") or "Unknown") for item in ffs_summary]
    ffs_values = [_num(item.get("count")) for item in ffs_summary]

    return {
        "all_kpis": {
            "labels": kpi_labels,
            "values": kpi_values,
        },
        "geography": {
            "labels": geography_labels,
            "values": geography_values,
        },
        "people": {
            "labels": people_labels,
            "values": people_values,
        },
        "finance": {
            "labels": finance_labels,
            "values": finance_values,
        },
        "grants": {
            "labels": grant_labels,
            "values": grant_values,
        },
        "shg": {
            "labels": shg_labels,
            "values": shg_values,
        },
        "sectors": {
            "labels": sector_labels,
            "values": sector_values,
        },
        "ffs": {
            "labels": ffs_labels,
            "values": ffs_values,
        },
    }

@reports_bp.route("/state_dashboard")
@login_required
@roles_required("SUPER_ADMIN", "ADMIN")
def state_dashboard():
    db = current_app.mongo_db
    role = session.get("role")

    pg_match = _pg_match_from_session(session)

    # Core counts
    pg_count = db.pgs.count_documents(pg_match)
    # Members: count by joining pg_members -> pgs so it is always correct
    member_count = _aggregate_count(db.pg_members, [
        {"$lookup": {"from": "pgs", "localField": "pg_id", "foreignField": "_id", "as": "pg"}},
        {"$unwind": "$pg"},
        *([{"$match": {f"pg.{k}": v for k, v in pg_match.items()}}] if pg_match else []),
        {"$count": "count"},
    ])

    # Geography master counts (within jurisdiction)
    if not pg_match:
        states_total = db.states.count_documents({})
        districts_total = db.districts.count_documents({})
        blocks_total = db.blocks.count_documents({})
    else:
        # scope by the most specific id we have
        if pg_match.get("district_id"):
            states_total = 1
            districts_total = 1
            # blocks under district
            blocks_total = db.blocks.count_documents({"district_id": pg_match["district_id"]})
        elif pg_match.get("state_id"):
            states_total = 1
            districts_total = db.districts.count_documents({"state_id": pg_match["state_id"]})
            dist_ids = [d["_id"] for d in db.districts.find({"state_id": pg_match["state_id"]}, {"_id": 1})]
            blocks_total = db.blocks.count_documents({"district_id": {"$in": dist_ids}}) if dist_ids else 0
        elif pg_match.get("block_id"):
            # derive district/state
            blk = db.blocks.find_one({"_id": pg_match["block_id"]}, {"district_id": 1})
            districts_total = 1 if blk else 0
            if blk and blk.get("district_id"):
                dist = db.districts.find_one({"_id": blk["district_id"]}, {"state_id": 1})
                states_total = 1 if dist else 0
            else:
                states_total = 0
            blocks_total = 1
        else:
            # clf-level or unknown
            states_total = db.states.count_documents({})
            districts_total = db.districts.count_documents({})
            blocks_total = db.blocks.count_documents({})

    # Cadre count within jurisdiction
    cadre_match = {"role": "CADRE_CC"}
    if pg_match.get("state_id"):
        cadre_match["state_id"] = pg_match["state_id"]
    if pg_match.get("district_id"):
        cadre_match["district_id"] = pg_match["district_id"]
    if pg_match.get("block_id"):
        cadre_match["block_id"] = pg_match["block_id"]
    cadre_count = db.users.count_documents(cadre_match)

    # SHG master counts (LokOS imported) scoped by user's jurisdiction (string geo)
    shg_q = _shg_filter_from_session(db, session)
    shg_total = db.shg_master.count_documents(shg_q)
    shg_active = db.shg_master.count_documents({**shg_q, "Status": "Active"})

    # --- Charts (PG counts by geo) ---
    pg_by_state = _top_counts(db, group_field="state_id", match_pg=pg_match, limit=10)
    pg_by_district = _top_counts(db, group_field="district_id", match_pg=pg_match, limit=10)
    pg_by_block = _top_counts(db, group_field="block_id", match_pg=pg_match, limit=10)

    # Turnover time-series (last 12 months)
    turnover_ts = _turnover_timeseries(db, match_pg=pg_match, months=12)
    total_turnover_latest = turnover_ts.get("latest_value") or 0
    turnover_period = turnover_ts.get("latest_label")

    # IDs of PGs in current scope (for cross-collection aggregation)
    pg_ids = [p["_id"] for p in db.pgs.find(pg_match, {"_id": 1})]

    #  Profit + Loss (cumulative) from Income–Expenditure (separate)
    total_profit = 0.0
    total_loss = 0.0
    if pg_ids:
        profit_data = list(db.pg_income_expenditure.aggregate([
            {"$match": {"pg_id": {"$in": pg_ids}}},
            {"$group": {
                "_id": None,
                "profit": {"$sum": {"$cond": [{"$gt": ["$excess_income_over_expenditure", 0]}, "$excess_income_over_expenditure", 0]}},
                "loss": {"$sum": {"$cond": [{"$lt": ["$excess_income_over_expenditure", 0]}, {"$abs": "$excess_income_over_expenditure"}, 0]}},
            }}
        ]))
        if profit_data:
            total_profit = float(profit_data[0].get("profit") or 0)
            total_loss = float(profit_data[0].get("loss") or 0)

    #  Grants (cumulative) + number of PGs that received any grants
    grants_total = 0.0
    pgs_with_grants = 0
    if pg_ids:
        grants_data = list(db.pg_grants.aggregate([
            {"$match": {"pg_id": {"$in": pg_ids}}},
            {"$group": {"_id": None, "grants_total": {"$sum": "$amount_received"}, "pgs_with_grants": {"$addToSet": "$pg_id"}}}
        ]))
        if grants_data:
            grants_total = float(grants_data[0].get("grants_total") or 0)
            pgs_with_grants = len(grants_data[0].get("pgs_with_grants") or [])

    #  Lakhpati Didi total (members)
    lakhpati_total = 0
    try:
        if pg_ids:
            lakhpati_total = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True})
    except Exception:
        lakhpati_total = 0

      # New State Dashboard KPI groups: PG sector and FFS module distribution
    sector_options = ["Agri", "ARDD", "Fishery"]
    sector_summary = []

    for sector in sector_options:
        q = dict(pg_match)
        q["sector"] = sector
        sector_summary.append({
            "key": sector,
            "label": sector,
            "count": db.pgs.count_documents(q),
            "detail_url": url_for("reports.state_dashboard", detail=f"sector:{sector}"),
        })

    ffs_modules = ["FFS Module 1", "FFS Module 2", "FFS Module 3", "FFS Module 4", "FFS Module 5"]
    ffs_summary = []

    agri_pg_match = dict(pg_match)
    agri_pg_match["sector"] = "Agri"

    agri_pg_ids = [p["_id"] for p in db.pgs.find(agri_pg_match, {"_id": 1})]

    for module in ffs_modules:
        cnt = 0

        try:
            cnt = len(
                db.pg_members.distinct(
                    "pg_id",
                    {
                        "pg_id": {"$in": agri_pg_ids},
                        "agri_ffs_module": module,
                    },
                )
            ) if agri_pg_ids else 0
        except Exception:
            cnt = 0

        ffs_summary.append({
            "key": module,
            "label": module,
            "count": cnt,
            "detail_url": url_for("reports.state_dashboard", detail=f"ffs:{module}"),
        })

    selected_detail = _state_dashboard_build_details(
        db,
        pg_match,
        pg_ids,
        request.args.get("detail"),
        search_query=request.args.get("q") or "",
        preview_limit=2,
    )

    # Top districts by SHG count (within scope if applicable)
    pipe = []
    if shg_q:
        pipe.append({"$match": shg_q})
    pipe += [
        {"$group": {"_id": "$District", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 12},
    ]
    top_districts = list(db.shg_master.aggregate(pipe))

    state_kpi_charts = _state_dashboard_chart_pack(
        states_total=states_total,
        districts_total=districts_total,
        blocks_total=blocks_total,
        cadre_count=cadre_count,
        pg_count=pg_count,
        member_count=member_count,
        total_turnover=total_turnover_latest,
        total_profit=total_profit,
        total_loss=total_loss,
        pgs_with_grants=pgs_with_grants,
        grants_total=grants_total,
        lakhpati_total=lakhpati_total,
        shg_total=shg_total,
        shg_active=shg_active,
        sector_summary=sector_summary,
        ffs_summary=ffs_summary,
    )

    charts = {
        "pg_by_state": {
            "labels": [x["name"] for x in pg_by_state],
            "values": [x["count"] for x in pg_by_state],
        },
        "pg_by_district": {
            "labels": [x["name"] for x in pg_by_district],
            "values": [x["count"] for x in pg_by_district],
        },
        "pg_by_block": {
            "labels": [x["name"] for x in pg_by_block],
            "values": [x["count"] for x in pg_by_block],
        },
        "turnover_ts": {
            "labels": turnover_ts.get("labels", []),
            "values": turnover_ts.get("values", []),
        },

        # New chart data for all State Dashboard KPIs
        "state_kpis": state_kpi_charts,
    }

    # Title label based on role/scope
    scope_label = "All States" if not pg_match else (
        "District" if pg_match.get("district_id") else
        "Block" if pg_match.get("block_id") else
        "State" if pg_match.get("state_id") else
        "Jurisdiction"
    )

    return render_template(
        "dashboard_state.html",
        role=role,
        scope_label=scope_label,
        pg_count=pg_count,
        member_count=member_count,
        total_turnover=total_turnover_latest,
        turnover_period=turnover_period,
        total_profit=total_profit,
        total_loss=total_loss,
        grants_total=grants_total,
        pgs_with_grants=pgs_with_grants,
        lakhpati_total=lakhpati_total,
        states_total=states_total,
        districts_total=districts_total,
        blocks_total=blocks_total,
        cadre_count=cadre_count,
        cadre_detail_url=url_for("reports.state_dashboard", detail="cadres"),
        sector_summary=sector_summary,
        ffs_summary=ffs_summary,
        selected_detail=selected_detail,
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top_districts=top_districts,
        charts=charts,
    )



@reports_bp.route("/state_dashboard/details")
@login_required
@roles_required("SUPER_ADMIN", "ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN", "CADRE_CC")
def state_dashboard_detail_full():
    db = current_app.mongo_db

    pg_match = _pg_match_from_session(session)
    pg_ids = [p["_id"] for p in db.pgs.find(pg_match, {"_id": 1})]

    detail_key = request.args.get("detail") or ""
    search_query = request.args.get("q") or ""

    selected_detail = _state_dashboard_build_details(
        db,
        pg_match,
        pg_ids,
        detail_key,
        search_query=search_query,
        preview_limit=None,
    )

    if not selected_detail:
        flash("No detail section selected.", "warning")
        return redirect(url_for("reports.state_dashboard"))

    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1

    try:
        per_page = int(request.args.get("per_page") or 10)
    except Exception:
        per_page = 10

    if page < 1:
        page = 1

    allowed_per_page = [10, 25, 50, 100]
    if per_page not in allowed_per_page:
        per_page = 10

    all_rows = selected_detail.get("rows") or []
    total_records = len(all_rows)
    total_pages = max(1, (total_records + per_page - 1) // per_page)

    if page > total_pages:
        page = total_pages

    start_index = (page - 1) * per_page
    end_index = start_index + per_page

    selected_detail["rows"] = all_rows[start_index:end_index]
    selected_detail["total_count"] = total_records
    selected_detail["shown_count"] = len(selected_detail["rows"])

    pagination = {
        "page": page,
        "per_page": per_page,
        "total_records": total_records,
        "total_pages": total_pages,
        "start_index": start_index,
        "end_index": min(end_index, total_records),
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "allowed_per_page": allowed_per_page,
    }

    return render_template(
        "dashboard_state_detail_full.html",
        selected_detail=selected_detail,
        search_query=search_query,
        detail_key=detail_key,
        pagination=pagination,
    )


@reports_bp.route("/state_dashboard/pg-members/<pg_id>")
@login_required
@roles_required("SUPER_ADMIN", "ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN", "CADRE_CC")
def state_dashboard_pg_members(pg_id):
    db = current_app.mongo_db

    if not pg_id or not ObjectId.is_valid(str(pg_id)):
        flash("Invalid PG selected.", "danger")
        return redirect(url_for("reports.state_dashboard"))

    pg_obj_id = ObjectId(str(pg_id))

    pg_match = _pg_match_from_session(session)
    pg_query = {"_id": pg_obj_id}

    if pg_match:
        pg_query.update(pg_match)

    pg = db.pgs.find_one(pg_query)

    if not pg:
        flash("PG not found or not available in your scope.", "danger")
        return redirect(url_for("reports.state_dashboard"))

    role = session.get("role")

    if role == "CADRE_CC":
        assigned_pg_ids = session.get("assigned_pg_ids") or []
        assigned_pg_ids = {str(x) for x in assigned_pg_ids}

        if assigned_pg_ids and str(pg_obj_id) not in assigned_pg_ids:
            flash("This PG is not assigned to you.", "danger")
            return redirect(url_for("reports.state_dashboard"))

    search_query = (request.args.get("q") or "").strip()

    raw_sector = str(pg.get("sector") or "").strip()
    sector_map = {
        "agri": "Agri",
        "ardd": "ARDD",
        "arrd": "ARDD",
        "fishery": "Fishery",
    }
    current_sector = sector_map.get(raw_sector.lower(), raw_sector)

    member_match = {
        "pg_id": pg_obj_id,
        "$or": [
            {"is_active": True},
            {"is_active": {"$exists": False}},
        ],
    }

    if search_query:
        safe_q = re.escape(search_query)

        search_fields = [
            {"name": {"$regex": safe_q, "$options": "i"}},
            {"spouse_name": {"$regex": safe_q, "$options": "i"}},
            {"category": {"$regex": safe_q, "$options": "i"}},
            {"shg_name": {"$regex": safe_q, "$options": "i"}},
            {"contact": {"$regex": safe_q, "$options": "i"}},
        ]

        if current_sector == "Agri":
            search_fields.extend([
                {"agri_crop": {"$regex": safe_q, "$options": "i"}},
                {"agri_ffs_module": {"$regex": safe_q, "$options": "i"}},
            ])
        elif current_sector == "ARDD":
            search_fields.extend([
                {"ardd_activity": {"$regex": safe_q, "$options": "i"}},
                {"ardd_unit": {"$regex": safe_q, "$options": "i"}},
            ])
        elif current_sector == "Fishery":
            search_fields.append({
                "fishery_activity": {"$regex": safe_q, "$options": "i"}
            })

        member_match["$and"] = [{"$or": search_fields}]

    members = list(
        db.pg_members.find(
            member_match,
            {
                "name": 1,
                "spouse_name": 1,
                "category": 1,
                "shg_name": 1,
                "contact": 1,
                "photo_id_number": 1,
                "membership_fee_paid": 1,
                "lakh_pati_didi": 1,
                "agri_crop": 1,
                "agri_ffs_module": 1,
                "ardd_activity": 1,
                "ardd_unit": 1,
                "fishery_activity": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        ).sort([("name", 1)])
    )

    for member in members:
        if current_sector != "Agri":
            member["agri_crop"] = ""
            member["agri_ffs_module"] = ""

        if current_sector != "ARDD":
            member["ardd_activity"] = ""
            member["ardd_unit"] = ""

        if current_sector != "Fishery":
            member["fishery_activity"] = ""

    return render_template(
        "dashboard_state_pg_members.html",
        pg=pg,
        members=members,
        search_query=search_query,
        current_sector=current_sector,
    )


@reports_bp.route("/hierarchy")
@login_required
def hierarchy_dashboard():
    db = current_app.mongo_db
    role = session.get("role")
    state_id = session.get("state_id")
    district_id = session.get("district_id")
    block_id = session.get("block_id")
    clf_id = session.get("clf_id")

    query = {}
    assigned_pg_ids = session.get("assigned_pg_ids") or []
    if role == "CADRE_CC":
        cadre_pg_oids = [ObjectId(x) for x in assigned_pg_ids if ObjectId.is_valid(str(x))]
        query["_id"] = {"$in": cadre_pg_oids or [ObjectId("000000000000000000000000")]}
    elif clf_id:
        query["clf_id"] = ObjectId(clf_id)
    elif block_id:
        query["block_id"] = ObjectId(block_id)
    elif district_id:
        query["district_id"] = ObjectId(district_id)
    elif state_id:
        query["state_id"] = ObjectId(state_id)

    pgs = list(db.pgs.find(query).sort([("created_at", -1)]).limit(200))
    if role == "CADRE_CC":
        cadre_count = 1
    else:
        cadre_match = {"role": "CADRE_CC"}
        if block_id:
            cadre_match["block_id"] = ObjectId(block_id)
        elif district_id:
            cadre_match["district_id"] = ObjectId(district_id)
        elif state_id:
            cadre_match["state_id"] = ObjectId(state_id)
        cadre_count = db.users.count_documents(cadre_match)
    pg_ids = [pg["_id"] for pg in pgs]
    pg_count = len(pg_ids)
    member_count = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}}) if pg_ids else 0

    #  Turnover / Profit-Loss / Grants / Lakhpati (scope)
    turnover_total = 0.0
    profit_total = 0.0
    loss_total = 0.0
    grants_total = 0.0
    pgs_with_grants = 0
    lakhpati_total = 0
    if pg_ids:
        td = list(db.pg_market_transactions.aggregate([
            {"$match": {"pg_id": {"$in": pg_ids}}},
            {"$group": {"_id": None, "turnover": {"$sum": "$total_turnover"}}}
        ]))
        turnover_total = float(td[0].get("turnover") if td else 0)

        pd = list(db.pg_income_expenditure.aggregate([
            {"$match": {"pg_id": {"$in": pg_ids}}},
            {"$group": {
                "_id": None,
                "profit": {"$sum": {"$cond": [{"$gt": ["$excess_income_over_expenditure", 0]}, "$excess_income_over_expenditure", 0]}},
                "loss": {"$sum": {"$cond": [{"$lt": ["$excess_income_over_expenditure", 0]}, {"$abs": "$excess_income_over_expenditure"}, 0]}},
            }}
        ]))
        if pd:
            profit_total = float(pd[0].get("profit") or 0)
            loss_total = float(pd[0].get("loss") or 0)

        gd = list(db.pg_grants.aggregate([
            {"$match": {"pg_id": {"$in": pg_ids}}},
            {"$group": {"_id": None, "grants_total": {"$sum": "$amount_received"}, "pgs": {"$addToSet": "$pg_id"}}}
        ]))
        if gd:
            grants_total = float(gd[0].get("grants_total") or 0)
            pgs_with_grants = len(gd[0].get("pgs") or [])

        try:
            lakhpati_total = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True})
        except Exception:
            lakhpati_total = 0

    # SHG master counts scoped by user's jurisdiction
    shg_q = _shg_filter_from_session(db, session)
    shg_total = db.shg_master.count_documents(shg_q)
    shg_active = db.shg_master.count_documents({**shg_q, "Status": "Active"})

    # Top blocks (or villages) for quick insight
    group_field = "$Block" if role in ("DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN") else "$Gram Panchayat"
    pipe = []
    if shg_q:
        pipe.append({"$match": shg_q})
    pipe += [
        {"$group": {"_id": group_field, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 12},
    ]
    shg_top = list(db.shg_master.aggregate(pipe))

    return render_template(
        "dashboard_hierarchy.html",
        role=role,
        pg_count=pg_count,
        member_count=member_count,
        turnover_total=turnover_total,
        profit_total=profit_total,
        loss_total=loss_total,
        grants_total=grants_total,
        pgs_with_grants=pgs_with_grants,
        lakhpati_total=lakhpati_total,
        cadre_count=cadre_count,
        pgs=pgs,
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top=shg_top,
    )




@reports_bp.route("/cadre_dashboard")
@login_required
@roles_required("CADRE_CC")
def cadre_dashboard():
    db = current_app.mongo_db
    assigned_pg_ids = session.get("assigned_pg_ids") or []
    assigned_oids = [ObjectId(x) for x in assigned_pg_ids if ObjectId.is_valid(str(x))]
    pgs = []
    if assigned_oids:
        pgs = list(db.pgs.find({"_id": {"$in": assigned_oids}}).sort([("name", 1)]))
    # clear active context on landing so dashboard remains PG-list only until a PG is opened
    session.pop("active_pg_id", None)
    session.pop("active_pg_name", None)
    session.pop("pg_id", None)
    return render_template("dashboard_cadre.html", pgs=pgs, assigned_pg_count=len(pgs))

@reports_bp.route("/cadre/assigned-pgs")
@roles_required("CADRE_CC")
def cadre_assigned_pgs():
    db = current_app.mongo_db

    assigned_ids = [ObjectId(x) for x in (session.get("assigned_pg_ids") or []) if ObjectId.is_valid(x)]
    pgs = []
    if assigned_ids:
        pgs = list(db.pgs.find({"_id": {"$in": assigned_ids}}).sort("name", 1))

    return render_template(
        "cadre_assigned_pgs.html",
        pgs=pgs,
        assigned_pg_count=len(pgs)
    )

@reports_bp.route("/export/scope.csv")
@login_required
def export_scope_csv():
    """Download a CSV of PGs within the current user's scope.

    Requirement:
    - Reports of every PG (district/block/GP/village wise) should have downloadable CSV.
    - This provides a single export that respects the session scope.
    """
    import csv
    from io import StringIO
    from flask import Response

    if mode == "summary" and group_by:
        bucket = {}
        for g in grants:
            pg = pg_by_id.get(g.get("pg_id")) or {}
            key = _bucket_key(pg) or "Unknown"
            util = list(db.pg_grant_utilizations.aggregate([
                {"$match": {"grant_id": g["_id"]}},
                {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
            ]))
            utilized = float(util[0]["utilized"]) if util else 0.0
            received = float(g.get("amount_received") or 0.0)
            bal = received - utilized
            cur = bucket.get(key) or {"received": 0.0, "utilized": 0.0, "balance": 0.0, "grants": 0}
            cur["received"] += received
            cur["utilized"] += utilized
            cur["balance"] += bal
            cur["grants"] += 1
            bucket[key] = cur

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([group_by, "#Grants", "Total Received", "Total Utilized", "Total Balance"])
        for k in sorted(bucket.keys()):
            cur = bucket[k]
            writer.writerow([k, cur["grants"], round(cur["received"], 2), round(cur["utilized"], 2), round(cur["balance"], 2)])
        output.seek(0)
        filename = f"grants_summary_{group_by.replace(' ','_').lower()}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})

    db = current_app.mongo_db
    pg_match = _pg_match_from_session(session)

    pgs = list(db.pgs.find(pg_match).sort([("name", 1)]))
    pg_ids = [p["_id"] for p in pgs]

    # Pre-aggregate grants + lakhpati counts
    grants_by_pg = {}
    for g in db.pg_grants.find({"pg_id": {"$in": pg_ids}}, {"pg_id": 1, "amount_received": 1}):
        k = str(g.get("pg_id"))
        grants_by_pg[k] = grants_by_pg.get(k, 0.0) + float(g.get("amount_received") or 0)

    lakhpati_by_pg = {}
    for row in db.pg_members.aggregate([
        {"$match": {"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True}},
        {"$group": {"_id": "$pg_id", "count": {"$sum": 1}}}
    ]):
        lakhpati_by_pg[str(row["_id"])] = int(row.get("count") or 0)

    # Profit/Loss cumulative by PG
    profit_by_pg = {}
    for row in db.pg_income_expenditure.aggregate([
        {"$match": {"pg_id": {"$in": pg_ids}}},
        {"$group": {"_id": "$pg_id", "profit_loss": {"$sum": "$excess_income_over_expenditure"}}}
    ]):
        profit_by_pg[str(row["_id"])] = float(row.get("profit_loss") or 0)

    # Turnover cumulative by PG
    turnover_by_pg = {}
    for row in db.pg_market_transactions.aggregate([
        {"$match": {"pg_id": {"$in": pg_ids}}},
        {"$group": {"_id": "$pg_id", "turnover": {"$sum": "$total_turnover"}}}
    ]):
        turnover_by_pg[str(row["_id"])] = float(row.get("turnover") or 0)

    def inr(v):
        try:
            return "₹" + f"{float(v or 0):,.2f}"
        except Exception:
            return "₹0.00"

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        "PG_ID", "PG_NAME", "STATE_ID", "DISTRICT_ID", "BLOCK_ID", "CLF_ID",
        "GP", "VILLAGE",
        "TURNOVER_TOTAL", "PROFIT_LOSS_TOTAL",
        "HAS_GRANTS", "GRANTS_RECEIVED_TOTAL",
        "LAKHPATI_DIDI_COUNT",
    ])
    for p in pgs:
        pid = str(p.get("_id"))
        gt = float(grants_by_pg.get(pid, 0.0))
        w.writerow([
            pid,
            p.get("name") or "",
            str(p.get("state_id") or ""),
            str(p.get("district_id") or ""),
            str(p.get("block_id") or ""),
            str(p.get("clf_id") or ""),
            p.get("GP") or p.get("gp") or "",
            p.get("Village") or p.get("village") or "",
            inr(turnover_by_pg.get(pid, 0.0)),
            inr(profit_by_pg.get(pid, 0.0)),
            "Yes" if gt > 0 else "No",
            inr(gt),
            lakhpati_by_pg.get(pid, 0),
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pg_scope_report.csv"},
    )

#changes by atlanta
@reports_bp.route("/pg_mpr/<pg_id>")
@login_required
def pg_mpr(pg_id):
    from flask import request, jsonify

    db = current_app.mongo_db

    selected_year = request.args.get("year")
    selected_month = request.args.get("month")
    view_mode = (request.args.get("view") or "period").strip().lower()

    query = {
        "level": "pg",
        "ref_id": pg_id
    }

    if view_mode == "all":
        # View All Time button only
        pass
    else:
        # Selected period mode must NEVER fall back to all data
        try:
            query["year"] = int(selected_year)
            query["month"] = int(selected_month)
        except Exception:
            query["year"] = -1
            query["month"] = -1

    snapshots = list(
        db.mpr_snapshots.find(query).sort([("year", -1), ("month", -1)])
    )

    def _safe_num(v):
        try:
            if v is None or v == "":
                return 0
            return float(v)
        except Exception:
            return 0

    normalized = []
    for i, row in enumerate(snapshots, start=1):
        normalized.append({
            "_id": str(row.get("_id")),
            "sl_no": i,
            "pg_id": pg_id,
            "year": row.get("year") or "",
            "month": row.get("month") or "",
            "turnover": _safe_num(
                row.get("turnover")
                or row.get("total_turnover")
                or row.get("business_turnover")
                or 0
            ),
            "input": _safe_num(
                row.get("input")
                or row.get("input_cost")
                or row.get("total_input")
                or 0
            ),
            "output": _safe_num(
                row.get("output")
                or row.get("output_value")
                or row.get("total_output")
                or 0
            ),
            "raw": {
                k: (str(v) if hasattr(v, "__class__") and v.__class__.__name__ == "ObjectId" else v)
                for k, v in row.items()
            }
        })

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if wants_json:
        return jsonify({
            "success": True,
            "pg_id": pg_id,
            "view": view_mode,
            "year": query.get("year"),
            "month": query.get("month"),
            "count": len(normalized),
            "snapshots": normalized
        })

    return render_template(
        "pg_mpr.html",
        snapshots=snapshots,
        selected_year=query.get("year"),
        selected_month=query.get("month"),
        view_mode=view_mode,
    )


@reports_bp.route("/pending_changes")
@login_required
@permissions_required(P_CHANGE_APPROVE)
def pending_changes():
    db = current_app.mongo_db
    pending = list(db.change_requests.find({"status": "pending"}).sort("created_at", -1).limit(200))
    return render_template("pending_changes.html", pending=pending)

@reports_bp.route("/change_request/<request_id>/decide", methods=["POST"])
@login_required
@permissions_required(P_CHANGE_APPROVE)
@require_unlocked_period(scope='pg')
def decide_change(request_id):
    from flask import request as flask_request, redirect, url_for, flash
    approve = flask_request.form.get("decision") == "approve"
    note = flask_request.form.get("note") or None
    user = {"user_id": session.get("user_id"), "username": session.get("username"), "role": session.get("role")}
    cr = decide_change_request(current_app.mongo_db, request_id=request_id, approve=approve, user=user, note=note)
    if not cr:
        flash("Change request not found.", "danger")
    else:
        flash(f"Change request {cr.get('status')}.", "success" if approve else "warning")
    return redirect(url_for("reports.pending_changes"))

@reports_bp.route("/generate_mpr/pg/<pg_id>", methods=["POST"])
@login_required
@permissions_required(P_MPR_GENERATE)
@require_unlocked_period(scope='pg')
def generate_mpr_pg(pg_id):
    from flask import request as flask_request, redirect, url_for, flash
    year = int(flask_request.form.get("year"))
    month = int(flask_request.form.get("month"))
    user = {"user_id": session.get("user_id"), "username": session.get("username"), "role": session.get("role")}
    snap = generate_mpr_snapshot(current_app.mongo_db, level="pg", ref_id=pg_id, year=year, month=month, user=user)
    flash("MPR snapshot generated.", "success")
    return redirect(url_for("reports.pg_mpr", pg_id=pg_id, year=year, month=month))

@reports_bp.route("/export/pg_mpr/<pg_id>")
@login_required
@permissions_required(P_REPORT_EXPORT)
def export_pg_mpr(pg_id):
    from flask import request as flask_request, send_file, abort
    fmt = (flask_request.args.get("format") or "xlsx").lower()
    year = int(flask_request.args.get("year") or datetime.utcnow().year)
    month = int(flask_request.args.get("month") or datetime.utcnow().month)

    db = current_app.mongo_db
    snap = db.mpr_snapshots.find_one({"level": "pg", "ref_id": ObjectId(pg_id), "year": year, "month": month}) or {}
    metrics = snap.get("metrics", {})

    if fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "PG MPR"
        ws.append(["PG ID", pg_id])
        ws.append(["Year", year])
        ws.append(["Month", month])
        ws.append([])
        ws.append(["Metric", "Value"])
        for k, v in metrics.items():
            ws.append([k, v])
        out_path = os.path.join(current_app.config.get("UPLOAD_FOLDER", "uploads"), f"pg_mpr_{pg_id}_{year}_{month}.xlsx")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        wb.save(out_path)
        return send_file(out_path, as_attachment=True, download_name=os.path.basename(out_path))

    if fmt == "pdf":
        if canvas is None:
            abort(500, description="reportlab is not installed. Add reportlab to requirements.txt.")
        out_path = os.path.join(current_app.config.get("UPLOAD_FOLDER", "uploads"), f"pg_mpr_{pg_id}_{year}_{month}.pdf")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        c = canvas.Canvas(out_path)
        y = 800
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, f"PG MPR Report — {pg_id} — {year}-{month:02d}")
        y -= 30
        c.setFont("Helvetica", 11)
        for k, v in metrics.items():
            c.drawString(50, y, f"{k}: {v}")
            y -= 16
            if y < 80:
                c.showPage()
                y = 800
                c.setFont("Helvetica", 11)
        c.save()
        return send_file(out_path, as_attachment=True, download_name=os.path.basename(out_path))

    abort(400, description="Unsupported format. Use xlsx or pdf.")


# ============================================================
# PG GRADATION SYSTEM - Manual Compliant Quarterly Scoring
# As per PG MIS Manual Chapter 5: PG Gradation Format
# Total Marks: 100
# Grade:
#   A / Excellent: >= 75, but turnover condition must also satisfy 75%
#   B / Average: 60 to 74.9
#   C / Poor: < 60
# ============================================================

def _gradation_float(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _gradation_int(value, default=0):
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _gradation_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "yes", "true", "on", "y")


def _gradation_quarter_months(quarter):
    return {
        1: (1, 3),
        2: (4, 6),
        3: (7, 9),
        4: (10, 12),
    }.get(int(quarter or 1), (1, 3))


def _gradation_pg_query(pg_oid):
    return {"pg_id": {"$in": [pg_oid, str(pg_oid)]}}


def _gradation_period_query(pg_oid, year, start_month, end_month):
    return {
        "$and": [
            _gradation_pg_query(pg_oid),
            {
                "$or": [
                    {
                        "year": int(year),
                        "month": {
                            "$gte": int(start_month),
                            "$lte": int(end_month),
                        },
                    },
                    {
                        "meeting_date": {
                            "$regex": f"^{int(year)}-"
                        }
                    },
                ]
            }
        ]
    }


def _gradation_get_month_from_doc(doc):
    try:
        if doc.get("month"):
            return int(doc.get("month"))

        meeting_date = str(doc.get("meeting_date") or "")
        if len(meeting_date) >= 7 and "-" in meeting_date:
            return int(meeting_date.split("-")[1])

        data = doc.get("data") or {}
        minutes = data.get("minutes") or {}
        date_iso = str(minutes.get("dateISO") or "")
        if len(date_iso) >= 7 and "-" in date_iso:
            return int(date_iso.split("-")[1])
    except Exception:
        return None

    return None


def _gradation_get_date_key(doc):
    meeting_date = str(doc.get("meeting_date") or "").strip()
    if meeting_date:
        return meeting_date

    data = doc.get("data") or {}
    minutes = data.get("minutes") or {}
    date_iso = str(minutes.get("dateISO") or "").strip()
    if date_iso:
        return date_iso[:10]

    return str(doc.get("_id") or "")


def _gradation_attendance_count(doc):
    count = _gradation_int(doc.get("attendance_count"), 0)
    if count > 0:
        return count

    attendance = doc.get("attendance")
    if isinstance(attendance, list):
        return len(attendance)

    data = doc.get("data") or {}
    members = data.get("members")
    if isinstance(members, list):
        return len(members)

    return 0


def _gradation_collection_count(db, collection_name, query):
    try:
        return db[collection_name].count_documents(query)
    except Exception:
        return 0


def _gradation_sum_turnover(db, pg_oid, year, start_month, end_month):
    """
    Turnover source:
    1. pg_market_transactions.total_turnover
    2. pg_market_transactions.turnover
    3. pg_business_monthly.turnover / total_turnover if available
    """

    total = 0.0

    try:
        rows = list(db.pg_market_transactions.find({
            **_gradation_pg_query(pg_oid),
            "year": int(year),
            "month": {
                "$gte": int(start_month),
                "$lte": int(end_month),
            }
        }))

        for row in rows:
            total += _gradation_float(
                row.get("total_turnover")
                or row.get("turnover")
                or row.get("market_total")
                or 0
            )
    except Exception:
        pass

    try:
        rows = list(db.pg_business_monthly.find({
            **_gradation_pg_query(pg_oid),
            "year": int(year),
            "month": {
                "$gte": int(start_month),
                "$lte": int(end_month),
            }
        }))

        for row in rows:
            total += _gradation_float(
                row.get("total_turnover")
                or row.get("turnover")
                or 0
            )
    except Exception:
        pass

    return round(total, 2)


def _gradation_meeting_metrics(db, pg_oid, year, start_month, end_month):
    """
    Calculates:
    - unique meetings held in the quarter
    - total attendance count in those meetings
    """

    meetings_by_date = {}

    for collection_name in ("pg_meetings", "pg_meeting_minutes"):
        try:
            docs = list(db[collection_name].find(_gradation_period_query(
                pg_oid,
                year,
                start_month,
                end_month
            )))
        except Exception:
            docs = []

        for doc in docs:
            month = _gradation_get_month_from_doc(doc)
            if not month or not (int(start_month) <= int(month) <= int(end_month)):
                continue

            key = _gradation_get_date_key(doc)
            if not key:
                continue

            existing = meetings_by_date.get(key) or {}
            old_attendance = _gradation_attendance_count(existing)
            new_attendance = _gradation_attendance_count(doc)

            if new_attendance >= old_attendance:
                meetings_by_date[key] = doc

    meetings_held = len(meetings_by_date)
    attendance_total = sum(_gradation_attendance_count(doc) for doc in meetings_by_date.values())

    return {
        "meetings_held": meetings_held,
        "attendance_total": attendance_total,
    }


def _gradation_manual_inputs_from_request():
    """
    Manual fields needed because the PG MIS manual has indicators that
    may not be fully derivable from existing transaction tables.
    """

    data = request.get_json(silent=True) if request.is_json else None
    src = data if isinstance(data, dict) else request.form

    return {
        "subcommittee_meetings": _gradation_int(src.get("subcommittee_meetings"), 0),
        "standard_pop_members": _gradation_int(src.get("standard_pop_members"), 0),
        "office_infrastructure_status": str(src.get("office_infrastructure_status") or "not_possessing_operating").strip(),
        "business_plan_status": str(src.get("business_plan_status") or "functioning_not_as_per_plan").strip(),
        "training_conducted": _gradation_bool(src.get("training_conducted")),
        "grievance_disposal_days": _gradation_int(src.get("grievance_disposal_days"), 0),

        # Optional override. If not submitted, backend auto-detects record keeping.
        "record_keeping": str(src.get("record_keeping") or "").strip().lower(),

        # Optional remarks for future audit/history display.
        "remarks": str(src.get("remarks") or "").strip(),
    }


def _gradation_subcommittee_marks(count):
    count = _gradation_int(count, 0)

    if count >= 5:
        return 10
    if count == 4:
        return 8
    if count == 3:
        return 6
    if count == 2:
        return 4
    if count == 1:
        return 2

    return 0


def _gradation_office_marks(status):
    status = str(status or "").strip().lower()

    mapping = {
        "functional": 5,
        "possessing_functional": 5,
        "possessing_and_functional": 5,

        "possessing_not_operational": 4,
        "not_operational": 4,

        "not_possessing_operating": 3,
        "no_office_but_operating": 3,

        "irregular": 0,
        "irregular_operation": 0,
    }

    return mapping.get(status, 0)


def _gradation_business_plan_marks(status):
    status = str(status or "").strip().lower()

    if status in ("as_per_plan", "operating_as_per_plan", "yes"):
        return 10

    if status in ("not_as_per_plan", "functioning_not_as_per_plan", "partial"):
        return 8

    return 0


def _gradation_grievance_marks(days):
    days = _gradation_int(days, 0)

    if days <= 0:
        return 0
    if days <= 7:
        return 10
    if days <= 10:
        return 8
    if days <= 15:
        return 6

    return 0


def _compute_gradation(db, pg_id: str, year: int, quarter: int, manual_inputs=None):
    """
    Manual-compliant PG Gradation.

    Indicator total:
    1. Regularity of Meeting - 10
    2. Member Attendance - 10
    3. Sub-committee Meeting - 10
    4. Record Keeping - 10
    5. Turnover - 20
    6. Standard PoP Members - 5
    7. Office & Infrastructure - 5
    8. Business Plan - 10
    9. Training / Exposure Visit - 10
    10. Grievance Redressal - 10
    """

    pg_oid = ObjectId(pg_id)
    year = int(year)
    quarter = int(quarter)
    start_month, end_month = _gradation_quarter_months(quarter)

    manual_inputs = manual_inputs or {}

    total_members = db.pg_members.count_documents({
        **_gradation_pg_query(pg_oid),
        "$or": [
            {"is_active": True},
            {"is_active": {"$exists": False}},
        ]
    })

    meeting_metrics = _gradation_meeting_metrics(
        db=db,
        pg_oid=pg_oid,
        year=year,
        start_month=start_month,
        end_month=end_month,
    )

    meetings_held = _gradation_int(meeting_metrics.get("meetings_held"), 0)
    attendance_total = _gradation_int(meeting_metrics.get("attendance_total"), 0)

    expected_meetings = 6
    expected_attendance = total_members * meetings_held

    meeting_regular_percent = 0.0
    if expected_meetings > 0:
        meeting_regular_percent = min((meetings_held / expected_meetings) * 100, 100)

    attendance_percent = 0.0
    if expected_attendance > 0:
        attendance_percent = min((attendance_total / expected_attendance) * 100, 100)

    turnover = _gradation_sum_turnover(
        db=db,
        pg_oid=pg_oid,
        year=year,
        start_month=start_month,
        end_month=end_month,
    )

    turnover_target = 300000.0
    turnover_percent = 0.0
    if turnover_target > 0:
        turnover_percent = min((turnover / turnover_target) * 100, 100)

    standard_pop_members = _gradation_int(manual_inputs.get("standard_pop_members"), 0)
    standard_pop_percent = 0.0
    if total_members > 0:
        standard_pop_percent = min((standard_pop_members / total_members) * 100, 100)

    subcommittee_meetings = _gradation_int(manual_inputs.get("subcommittee_meetings"), 0)

    record_override = str(manual_inputs.get("record_keeping") or "").strip().lower()
    if record_override in ("yes", "1", "true", "recorded"):
        record_keeping_done = True
    elif record_override in ("no", "0", "false", "not_recorded"):
        record_keeping_done = False
    else:
        transaction_count = 0
        period_query = {
            **_gradation_pg_query(pg_oid),
            "year": year,
            "month": {
                "$gte": start_month,
                "$lte": end_month,
            }
        }

        for collection_name in (
            "pg_market_transactions",
            "pg_business_monthly",
            "pg_receipt_vouchers",
            "pg_input_registers",
            "pg_output_registers",
            "pg_asset_registers",
            "pg_stocks_monthly",
        ):
            transaction_count += _gradation_collection_count(db, collection_name, period_query)

        record_keeping_done = transaction_count > 0

    office_status = manual_inputs.get("office_infrastructure_status")
    business_plan_status = manual_inputs.get("business_plan_status")
    training_conducted = _gradation_bool(manual_inputs.get("training_conducted"))
    grievance_days = _gradation_int(manual_inputs.get("grievance_disposal_days"), 0)

    indicators = [
        {
            "key": "meeting_regularity",
            "sl_no": 1,
            "indicator": "Regularity of Meeting (Monthly twice)",
            "max_marks": 10,
            "formula": "(Total meetings held during the period / Total meetings to be held during the period) × 100",
            "metric": f"{meetings_held} of {expected_meetings} meetings",
            "percentage": round(meeting_regular_percent, 2),
            "marks_obtained": round(min((meeting_regular_percent * 10) / 100, 10), 2),
        },
        {
            "key": "member_attendance",
            "sl_no": 2,
            "indicator": "Regularity of Members Attendance",
            "max_marks": 10,
            "formula": "(Total members attended meetings / Total PG members × meetings held) × 100",
            "metric": f"{attendance_total} attendance out of {expected_attendance}",
            "percentage": round(attendance_percent, 2),
            "marks_obtained": round(min((attendance_percent * 10) / 100, 10), 2),
        },
        {
            "key": "subcommittee_meeting",
            "sl_no": 3,
            "indicator": "Sub-committee Meeting Regularity",
            "max_marks": 10,
            "formula": ">=5 meetings = 10, 4 = 8, 3 = 6, 2 = 4, 1 = 2, else 0",
            "metric": f"{subcommittee_meetings} sub-committee meetings",
            "percentage": None,
            "marks_obtained": _gradation_subcommittee_marks(subcommittee_meetings),
        },
        {
            "key": "record_keeping",
            "sl_no": 4,
            "indicator": "Record Keeping",
            "max_marks": 10,
            "formula": "If transactions are recorded in the period = 10, else 0",
            "metric": "Transactions recorded" if record_keeping_done else "No transaction record found",
            "percentage": None,
            "marks_obtained": 10 if record_keeping_done else 0,
        },
        {
            "key": "turnover",
            "sl_no": 5,
            "indicator": "Turnover",
            "max_marks": 20,
            "formula": "Total turnover in 3 months / 300000 × 100",
            "metric": f"₹{turnover:,.2f} of ₹3,00,000 target",
            "percentage": round(turnover_percent, 2),
            "marks_obtained": round(min((turnover_percent * 20) / 100, 20), 2),
        },
        {
            "key": "standard_pop",
            "sl_no": 6,
            "indicator": "% of PG Members linked with Standard PoP",
            "max_marks": 5,
            "formula": "(PG members practicing standard PoP / Total PG members) × 100",
            "metric": f"{standard_pop_members} of {total_members} members",
            "percentage": round(standard_pop_percent, 2),
            "marks_obtained": round(min((standard_pop_percent * 5) / 100, 5), 2),
        },
        {
            "key": "office_infrastructure",
            "sl_no": 7,
            "indicator": "PG having its own functional office & infrastructure",
            "max_marks": 5,
            "formula": "Functional = 5, available but not operational = 4, no office but operating = 3, irregular = 0",
            "metric": str(office_status or ""),
            "percentage": None,
            "marks_obtained": _gradation_office_marks(office_status),
        },
        {
            "key": "business_plan",
            "sl_no": 8,
            "indicator": "Business Plan",
            "max_marks": 10,
            "formula": "Operating as per business plan = 10, functioning but not as per business plan = 8",
            "metric": str(business_plan_status or ""),
            "percentage": None,
            "marks_obtained": _gradation_business_plan_marks(business_plan_status),
        },
        {
            "key": "training_exposure",
            "sl_no": 9,
            "indicator": "Training programme / exposure visit conducted",
            "max_marks": 10,
            "formula": "Training programme and exposure conducted = 10",
            "metric": "Conducted" if training_conducted else "Not conducted",
            "percentage": None,
            "marks_obtained": 10 if training_conducted else 0,
        },
        {
            "key": "grievance_redressal",
            "sl_no": 10,
            "indicator": "Grievance Redressal Mechanism in place for PG Members",
            "max_marks": 10,
            "formula": "7 days = 10, 10 days = 8, 15 days = 6, more than 15 days = 0",
            "metric": f"{grievance_days} days" if grievance_days else "Not entered",
            "percentage": None,
            "marks_obtained": _gradation_grievance_marks(grievance_days),
        },
    ]

    total_score = round(sum(_gradation_float(i.get("marks_obtained")) for i in indicators), 2)

    turnover_condition_met = turnover_percent >= 75

    if total_score >= 75 and turnover_condition_met:
        grade = "A"
        grade_label = "Excellent"
    elif total_score >= 60:
        grade = "B"
        grade_label = "Average"
    else:
        grade = "C"
        grade_label = "Poor"

    breakdown = {
        item["key"]: item["marks_obtained"]
        for item in indicators
    }

    return {
        "year": year,
        "quarter": quarter,
        "quarter_label": f"Q{quarter}",
        "period": {
            "start_month": start_month,
            "end_month": end_month,
        },
        "score": total_score,
        "total_score": total_score,
        "grade": grade,
        "grade_label": grade_label,
        "turnover_condition_met": turnover_condition_met,
        "turnover_percent": round(turnover_percent, 2),
        "breakdown": breakdown,
        "indicators": indicators,
        "manual_inputs": manual_inputs,
        "metrics": {
            "total_members": total_members,
            "meetings_held": meetings_held,
            "expected_meetings": expected_meetings,
            "attendance_total": attendance_total,
            "expected_attendance": expected_attendance,
            "meeting_regular_percent": round(meeting_regular_percent, 2),
            "attendance_percent": round(attendance_percent, 2),
            "turnover": turnover,
            "turnover_target": turnover_target,
            "turnover_percent": round(turnover_percent, 2),
            "standard_pop_members": standard_pop_members,
            "standard_pop_percent": round(standard_pop_percent, 2),
            "subcommittee_meetings": subcommittee_meetings,
            "record_keeping_done": record_keeping_done,
            "office_infrastructure_status": office_status,
            "business_plan_status": business_plan_status,
            "training_conducted": training_conducted,
            "grievance_disposal_days": grievance_days,
        }
    }


@reports_bp.route("/gradation/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "CADRE_CC")
@require_unlocked_period(scope='pg')
def gradation(pg_id):
    db = current_app.mongo_db

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
        or request.is_json
    )

    if not ObjectId.is_valid(pg_id):
        if wants_json:
            return jsonify({"success": False, "message": "Invalid PG id"}), 400
        flash("Invalid PG id.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg_oid = ObjectId(pg_id)
    pg = db.pgs.find_one({"_id": pg_oid})

    if not pg:
        if wants_json:
            return jsonify({"success": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    year = _gradation_int(request.values.get("year"), datetime.utcnow().year)
    quarter = _gradation_int(
        request.values.get("quarter"),
        ((datetime.utcnow().month - 1) // 3 + 1)
    )

    if quarter not in (1, 2, 3, 4):
        quarter = ((datetime.utcnow().month - 1) // 3 + 1)

    existing_snap = db.pg_gradation_snapshots.find_one({
        "pg_id": pg_oid,
        "year": year,
        "quarter": quarter
    })

    if request.method == "POST" and session.get("role") in ("PG_DATA_ENTRY", "CADRE_CC"):
        if wants_json:
            return jsonify({
                "success": False,
                "message": "Gradation can only be submitted by CLF/Block authorities."
            }), 403

        flash("Gradation can only be submitted by CLF/Block authorities.", "warning")
        return redirect(url_for("reports.gradation", pg_id=pg_id, year=year, quarter=quarter))

    if request.method == "POST":
        manual_inputs = _gradation_manual_inputs_from_request()

        snap = _compute_gradation(
            db=db,
            pg_id=pg_id,
            year=year,
            quarter=quarter,
            manual_inputs=manual_inputs
        )

        now = datetime.utcnow()

        snap.update({
            "pg_id": pg_oid,
            "pg_name": pg.get("name") or pg.get("pg_name") or "",
            "updated_at": now,
        })

        if not existing_snap:
            snap["created_at"] = now

        db.pg_gradation_snapshots.update_one(
            {
                "pg_id": pg_oid,
                "year": year,
                "quarter": quarter,
            },
            {
                "$set": snap,
                "$setOnInsert": {
                    "created_at": now,
                }
            },
            upsert=True
        )

        if wants_json:
            return jsonify({
                "success": True,
                "message": "PG Gradation saved successfully as per manual format.",
                "pg_id": pg_id,
                "pg_name": pg.get("name") or pg.get("pg_name") or "",
                "year": year,
                "quarter": quarter,
                "snapshot": {
                    "year": snap.get("year"),
                    "quarter": snap.get("quarter"),
                    "score": snap.get("score"),
                    "total_score": snap.get("total_score"),
                    "grade": snap.get("grade"),
                    "grade_label": snap.get("grade_label"),
                    "turnover_condition_met": snap.get("turnover_condition_met"),
                    "turnover_percent": snap.get("turnover_percent"),
                    "breakdown": snap.get("breakdown", {}),
                    "indicators": snap.get("indicators", []),
                    "metrics": snap.get("metrics", {}),
                    "manual_inputs": snap.get("manual_inputs", {}),
                    "updated_at": snap.get("updated_at").isoformat() if snap.get("updated_at") else None,
                }
            })

        flash("PG Gradation saved successfully as per manual format.", "success")
        return redirect(url_for("reports.gradation", pg_id=pg_id, year=year, quarter=quarter))

    manual_inputs = (existing_snap or {}).get("manual_inputs") or {}

    computed = _compute_gradation(
        db=db,
        pg_id=pg_id,
        year=year,
        quarter=quarter,
        manual_inputs=manual_inputs
    )

    src = existing_snap or computed

    if wants_json:
        return jsonify({
            "success": True,
            "pg_id": pg_id,
            "pg_name": pg.get("name") or pg.get("pg_name") or "",
            "year": year,
            "quarter": quarter,
            "has_saved_snapshot": bool(existing_snap),
            "can_compute": session.get("role") not in ("PG_DATA_ENTRY", "CADRE_CC"),
            "snapshot": {
                "year": src.get("year"),
                "quarter": src.get("quarter"),
                "score": src.get("score") or src.get("total_score"),
                "total_score": src.get("total_score") or src.get("score"),
                "grade": src.get("grade"),
                "grade_label": src.get("grade_label"),
                "turnover_condition_met": src.get("turnover_condition_met"),
                "turnover_percent": src.get("turnover_percent"),
                "breakdown": src.get("breakdown", {}),
                "indicators": src.get("indicators", []),
                "metrics": src.get("metrics", {}),
                "manual_inputs": src.get("manual_inputs", {}),
                "updated_at": src.get("updated_at").isoformat() if src.get("updated_at") else None,
            }
        })

    return render_template(
        "gradation.html",
        pg=pg,
        snap=existing_snap,
        computed=computed,
        src=src,
        year=year,
        quarter=quarter
    )


# ============================================================
# NEW: PERIOD LOCKING (Month Freeze)
# - Used by admin/manager for monthly closure
# ============================================================

from app.services.periods import is_period_locked, lock_period, unlock_period

@reports_bp.route("/period_lock/<scope>/<ref_id>", methods=["GET","POST"])
@login_required
@roles_required("CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def period_lock(scope, ref_id):
    db = current_app.mongo_db
    year = int(request.values.get("year") or datetime.utcnow().year)
    month = int(request.values.get("month") or datetime.utcnow().month)

    locked = is_period_locked(db, scope=scope, ref_id=ref_id, year=year, month=month)

    if request.method == "POST":
        action = request.form.get("action")
        user = {"id": str(session.get("user_id")), "name": session.get("username"), "role": session.get("role")}
        note = request.form.get("note") or None
        if action == "lock":
            lock_period(db, scope=scope, ref_id=ref_id, year=year, month=month, user=user, note=note)
            flash("Period locked.", "success")
        elif action == "unlock":
            unlock_period(db, scope=scope, ref_id=ref_id, year=year, month=month, user=user, note=note)
            flash("Period unlocked.", "success")
        return redirect(url_for("reports.period_lock", scope=scope, ref_id=ref_id, year=year, month=month))

    return render_template("period_lock.html", scope=scope, ref_id=ref_id, year=year, month=month, locked=locked)


# ============================================================
# NEW: SECTOR-WISE ANALYTICS (Agriculture/Livestock/Non-farm...)
# ============================================================

@reports_bp.route("/sector_analytics", methods=["GET"])
@login_required
@roles_required("BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "CLF_MANAGER","CLF_ADMIN")
def sector_analytics():
    db = current_app.mongo_db
    year = int(request.args.get("year") or datetime.utcnow().year)

    pipeline = [
        {"$match": {"year": year}},
        {"$lookup": {"from": "pgs", "localField": "pg_id", "foreignField": "_id", "as": "pg"}},
        {"$unwind": "$pg"},
        {"$group": {"_id": {"sector": {"$ifNull": ["$pg.sector", "Unassigned"]}}, "turnover": {"$sum": "$total_turnover"}, "pgs": {"$addToSet": "$pg._id"}}},
        {"$project": {"sector": "$_id.sector", "turnover": 1, "pg_count": {"$size": "$pgs"}}},
        {"$sort": {"turnover": -1}}
    ]
    rows = list(db.pg_market_transactions.aggregate(pipeline))
    return render_template("sector_analytics.html", year=year, rows=rows)




# ============================================================
# MONTHLY MODULE WORKFLOW SUBMISSION / APPROVAL
# Flow:
#   SUBMITTED -> CLF -> BLOCK -> DISTRICT -> STATE/COMPLETE
# Stored in:
#   monthly_module_submissions
# ============================================================

def _workflow_wants_json():
    return (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


def _workflow_payload_value(key, default=""):
    data = request.get_json(silent=True) if request.is_json else None
    if isinstance(data, dict) and key in data:
        return data.get(key, default)
    return request.form.get(key, request.args.get(key, default))


@reports_bp.route("/workflow/submit/<pg_id>/<int:year>/<int:month>/<module>", methods=["POST"])
@login_required
@roles_required(
    "PG_DATA_ENTRY",
    "CADRE_CC",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
)
def submit_monthly(pg_id, year, month, module):
    wants_json = _workflow_wants_json()

    try:
        result = submit_to_clf(
            pg_id=pg_id,
            year=year,
            month=month,
            module=module,
            submitted_by=session.get("user_id") or session.get("email") or session.get("username"),
            remarks=_workflow_payload_value("remarks", "") or _workflow_payload_value("note", ""),
        )

        if wants_json:
            return jsonify({
                "success": True,
                "message": result.get("message", "Monthly module submitted successfully."),
                "already_submitted": result.get("already_submitted", False),
                "submission": result.get("submission"),
            })

        flash(result.get("message", "Monthly module submitted successfully."), "success")
        return redirect(request.referrer or url_for("reports.pg_mpr", pg_id=pg_id))

    except Exception as e:
        if wants_json:
            return jsonify({
                "success": False,
                "message": str(e),
            }), 400

        flash(str(e), "danger")
        return redirect(request.referrer or url_for("pg.pg_home"))


@reports_bp.route("/workflow/approve/<pg_id>/<int:year>/<int:month>/<module>/<level>", methods=["POST"])
@login_required
@roles_required(
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "STATE_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
)
def approve_monthly(pg_id, year, month, module, level):
    wants_json = _workflow_wants_json()

    try:
        result = approve(
            pg_id=pg_id,
            year=year,
            month=month,
            module=module,
            level=level,
            approved_by=session.get("user_id") or session.get("email") or session.get("username"),
            remarks=_workflow_payload_value("remarks", "") or _workflow_payload_value("remark", ""),
        )

        if wants_json:
            return jsonify({
                "success": True,
                "message": result.get("message", "Monthly module approved successfully."),
                "already_approved": result.get("already_approved", False),
                "submission": result.get("submission"),
            })

        flash(result.get("message", "Monthly module approved successfully."), "success")
        return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))

    except Exception as e:
        if wants_json:
            return jsonify({
                "success": False,
                "message": str(e),
            }), 400

        flash(str(e), "danger")
        return redirect(request.referrer or url_for("pg.pg_home"))


@reports_bp.route("/workflow/reject/<pg_id>/<int:year>/<int:month>/<module>/<level>", methods=["POST"])
@login_required
@roles_required(
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "STATE_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
)
def reject_monthly(pg_id, year, month, module, level):
    wants_json = _workflow_wants_json()

    try:
        reason = (
            _workflow_payload_value("reason", "")
            or _workflow_payload_value("remarks", "")
            or _workflow_payload_value("remark", "")
            or "Rejected"
        )

        result = reject(
            pg_id=pg_id,
            year=year,
            month=month,
            module=module,
            level=level,
            reason=reason,
            rejected_by=session.get("user_id") or session.get("email") or session.get("username"),
        )

        if wants_json:
            return jsonify({
                "success": True,
                "message": result.get("message", "Monthly module rejected successfully."),
                "submission": result.get("submission"),
            })

        flash(result.get("message", "Monthly module rejected successfully."), "warning")
        return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))

    except Exception as e:
        if wants_json:
            return jsonify({
                "success": False,
                "message": str(e),
            }), 400

        flash(str(e), "danger")
        return redirect(request.referrer or url_for("pg.pg_home"))


@reports_bp.route("/workflow/status/<pg_id>/<int:year>/<int:month>/<module>", methods=["GET"])
@login_required
@roles_required(
    "PG_DATA_ENTRY",
    "CADRE_CC",
    "CLF_MANAGER",
    "CLF_ADMIN",
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "STATE_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
)
def workflow_status(pg_id, year, month, module):
    wants_json = _workflow_wants_json()

    try:
        submission = get_submission(
            pg_id=pg_id,
            year=year,
            month=month,
            module=module,
        )

        if wants_json:
            return jsonify({
                "success": True,
                "pg_id": pg_id,
                "year": year,
                "month": month,
                "module": module,
                "submission": submission,
            })

        if not submission:
            flash("No workflow submission found for this module and period.", "info")
        else:
            flash(f"Current workflow status: {submission.get('status')}", "info")

        return redirect(request.referrer or url_for("pg.pg_home"))

    except Exception as e:
        if wants_json:
            return jsonify({
                "success": False,
                "message": str(e),
            }), 400

        flash(str(e), "danger")
        return redirect(request.referrer or url_for("pg.pg_home"))


@reports_bp.route("/clf/console")
@roles_required("CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def clf_console():

    from datetime import datetime
    from bson import ObjectId

    db = current_app.mongo_db

    role = session.get("role")
    clf_id = session.get("clf_id")
    block_id = session.get("block_id")

    year = request.args.get("year")
    month = request.args.get("month")
    view_mode = (request.args.get("view") or "period").strip().lower()

    is_all_time = view_mode == "all"

    try:
        now = datetime.utcnow()

        if is_all_time:
            year = None
            month = None
        else:
            year = int(year) if year else now.year
            month = int(month) if month else now.month

    except Exception:
        now = datetime.utcnow()
        year, month = now.year, now.month
        is_all_time = False

    # =========================
    # Resolve PG scope
    # =========================
    if clf_id:
        pgs_q = {"clf_id": ObjectId(clf_id)}

    elif block_id:
        pgs_q = {"block_id": ObjectId(block_id)}

    else:
        pgs_q = {}

    pgs = list(db.pgs.find(pgs_q, {"name": 1}).sort("name", 1))

    modules = ["mpr", "business", "loans", "stock", "finance"]

    rows = []

    def _has_period_data(pg_oid, pg_id, module, year, month):
        """
        Checks real module collections also, not only db.submissions.
        This prevents the console from becoming blank when data exists
        but submission row is not created yet.
        """
        pg_match_or = [
            {"pg_id": pg_oid},
            {"pg_id": pg_id},
            {"ref_id": pg_id},
            {"ref_id": pg_oid},
        ]

        base_q = {
            "$or": pg_match_or,
            "year": year,
            "month": month,
        }

        try:
            y_int = int(year)
            m_int = int(month)

            period_start = datetime(y_int, m_int, 1)

            if m_int == 12:
                period_end = datetime(y_int + 1, 1, 1)
            else:
                period_end = datetime(y_int, m_int + 1, 1)

            date_q = {
                "$or": pg_match_or,
                "created_at": {
                    "$gte": period_start,
                    "$lt": period_end,
                }
            }

            updated_q = {
                "$or": pg_match_or,
                "updated_at": {
                    "$gte": period_start,
                    "$lt": period_end,
                }
            }

        except Exception:
            date_q = None
            updated_q = None

        try:
            if module == "mpr":
                return bool(
                    db.mpr_snapshots.find_one({
                        "$or": [
                            {"ref_id": pg_id},
                            {"ref_id": pg_oid},
                            {"pg_id": pg_id},
                            {"pg_id": pg_oid},
                        ],
                        "year": year,
                        "month": month,
                    })
                )

            if module == "business":
                return bool(
                    db.pg_business_monthly.find_one(base_q)
                    or db.pg_market_transactions.find_one(base_q)

                    or (date_q and db.pg_business_monthly.find_one(date_q))
                    or (date_q and db.pg_market_transactions.find_one(date_q))

                    or (updated_q and db.pg_business_monthly.find_one(updated_q))
                    or (updated_q and db.pg_market_transactions.find_one(updated_q))
                )

            if module == "loans":
                return bool(
                    db.pg_loan_accounts.find_one(base_q)
                    or db.pg_member_loan_accounts.find_one(base_q)
                    or db.pg_loans.find_one(base_q)

                    or (date_q and db.pg_loan_accounts.find_one(date_q))
                    or (date_q and db.pg_member_loan_accounts.find_one(date_q))
                    or (date_q and db.pg_loans.find_one(date_q))

                    or (updated_q and db.pg_loan_accounts.find_one(updated_q))
                    or (updated_q and db.pg_member_loan_accounts.find_one(updated_q))
                    or (updated_q and db.pg_loans.find_one(updated_q))
                )

            if module == "stock":
                return bool(
                    db.pg_input_procurement.find_one(base_q)
                    or db.pg_output_marketing.find_one(base_q)
                    or db.pg_stock_register.find_one(base_q)

                    or (date_q and db.pg_input_procurement.find_one(date_q))
                    or (date_q and db.pg_output_marketing.find_one(date_q))
                    or (date_q and db.pg_stock_register.find_one(date_q))

                    or (updated_q and db.pg_input_procurement.find_one(updated_q))
                    or (updated_q and db.pg_output_marketing.find_one(updated_q))
                    or (updated_q and db.pg_stock_register.find_one(updated_q))
                )

            if module == "finance":
                return bool(
                    db.pg_cashbooks.find_one(base_q)
                    or db.pg_receipt_vouchers.find_one(base_q)
                    or db.pg_payment_vouchers.find_one(base_q)
                    or db.pg_income_expenditure.find_one(base_q)

                    or (date_q and db.pg_cashbooks.find_one(date_q))
                    or (date_q and db.pg_receipt_vouchers.find_one(date_q))
                    or (date_q and db.pg_payment_vouchers.find_one(date_q))
                    or (date_q and db.pg_income_expenditure.find_one(date_q))

                    or (updated_q and db.pg_cashbooks.find_one(updated_q))
                    or (updated_q and db.pg_receipt_vouchers.find_one(updated_q))
                    or (updated_q and db.pg_payment_vouchers.find_one(updated_q))
                    or (updated_q and db.pg_income_expenditure.find_one(updated_q))
                )

        except Exception:
            return False

        return False

    def _has_any_data(pg_oid, pg_id, module):
        """
        All Time mode helper. Shows rows if any real module data exists,
        even if db.submissions is not available.
        """
        pg_match_or = [
            {"pg_id": pg_oid},
            {"pg_id": pg_id},
            {"ref_id": pg_id},
            {"ref_id": pg_oid},
        ]

        try:
            if module == "mpr":
                return bool(
                    db.mpr_snapshots.find_one({
                        "$or": [
                            {"ref_id": pg_id},
                            {"ref_id": pg_oid},
                            {"pg_id": pg_id},
                            {"pg_id": pg_oid},
                        ]
                    })
                )

            if module == "business":
                return bool(
                    db.pg_business_monthly.find_one({"$or": pg_match_or})
                    or db.pg_market_transactions.find_one({"$or": pg_match_or})
                )

            if module == "loans":
                return bool(
                    db.pg_loan_accounts.find_one({"$or": pg_match_or})
                    or db.pg_member_loan_accounts.find_one({"$or": pg_match_or})
                    or db.pg_loans.find_one({"$or": pg_match_or})
                )

            if module == "stock":
                return bool(
                    db.pg_input_procurement.find_one({"$or": pg_match_or})
                    or db.pg_output_marketing.find_one({"$or": pg_match_or})
                    or db.pg_stock_register.find_one({"$or": pg_match_or})
                )

            if module == "finance":
                return bool(
                    db.pg_cashbooks.find_one({"$or": pg_match_or})
                    or db.pg_receipt_vouchers.find_one({"$or": pg_match_or})
                    or db.pg_payment_vouchers.find_one({"$or": pg_match_or})
                    or db.pg_income_expenditure.find_one({"$or": pg_match_or})
                )

        except Exception:
            return False

        return False

    for pg in pgs:

        pg_id = str(pg["_id"])
        pg_oid = pg["_id"]

        for module in modules:

            submission_query = {
                "$or": [
                    {"pg_id": pg_id},
                    {"pg_id": pg_oid},
                ],
                "module": {
                    "$in": [
                        module,
                        module.upper(),
                        module.lower(),
                    ]
                }
            }

            if not is_all_time:
                submission_query["year"] = year
                submission_query["month"] = month

            sub = (
                db.submissions
                .find_one(
                    submission_query,
                    sort=[("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)]
                )
                or {}
            )

            if not is_all_time:
                has_data = bool(sub) or _has_period_data(pg_oid, pg_id, module, year, month)
                if not has_data:
                    continue
            else:
                has_data = bool(sub) or _has_any_data(pg_oid, pg_id, module)
                if not has_data:
                    continue

            rows.append({
                "pg_id": pg_id,
                "pg_name": pg.get("name","(PG)"),
                "module": module,
                "status": sub.get("status","Data Available" if not sub else "—")
            })

    return render_template(
        "clf_console.html",
        rows=rows,
        year=year,
        month=month,
        is_all_time=is_all_time,
    )

# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available


# ============================================================
# NEW: KPI DRILL-DOWN (Manual: click KPI → PG list → member list)
# ============================================================

@reports_bp.route("/kpi/pgs")
@login_required
def kpi_pgs():
    db = current_app.mongo_db
    metric = (request.args.get("metric") or "turnover").lower()
    year = int(request.args.get("year") or datetime.utcnow().year)
    month = int(request.args.get("month") or datetime.utcnow().month)

    # Scope PGs by user jurisdiction
    state_id = session.get("state_id")
    district_id = session.get("district_id")
    block_id = session.get("block_id")
    clf_id = session.get("clf_id")

    q = {}
    if clf_id:
        q["clf_id"] = ObjectId(clf_id)
    elif block_id:
        q["block_id"] = ObjectId(block_id)
    elif district_id:
        q["district_id"] = ObjectId(district_id)
    elif state_id:
        q["state_id"] = ObjectId(state_id)

    pgs = list(db.pgs.find(q, {"name": 1, "pg_name": 1, "auto_id": 1}).limit(1000))
    pg_map = {str(p["_id"]): p for p in pgs}
    pg_ids = [p["_id"] for p in pgs]

    # Pull MPR snapshots for these PGs and month
    snaps = list(db.mpr_snapshots.find({"level": "pg", "ref_id": {"$in": [str(x) for x in pg_ids] + pg_ids}, "year": year, "month": month}))

    # normalize ref_id to string
    snap_map = {}
    for s in snaps:
        rid = s.get("ref_id")
        rid = str(rid)
        snap_map[rid] = s

    rows = []
    for p in pgs:
        pid = str(p["_id"])
        s = snap_map.get(pid) or {}
        metrics = s.get("metrics") or {}
        val = 0
        if metric == "turnover":
            val = float(metrics.get("total_turnover") or s.get("monthly_turnover") or 0)
        elif metric == "stock":
            val = float(metrics.get("closing_stock_value") or s.get("closing_stock_value") or 0)
        elif metric == "overdue":
            val = float(metrics.get("loan_overdue_total") or 0)
        elif metric == "outstanding":
            val = float(metrics.get("loan_outstanding_total") or 0)
        elif metric == "members_input":
            val = float(metrics.get("members_input_pct") or 0)
        elif metric == "members_output":
            val = float(metrics.get("members_output_pct") or 0)
        rows.append({
            "pg_id": pid,
            "name": p.get("name") or p.get("pg_name") or p.get("auto_id") or pid,
            "value": round(val,2),
            "has_snapshot": bool(s),
        })

    rows.sort(key=lambda r: r["value"], reverse=True)
    return render_template("kpi_pgs.html", metric=metric, year=year, month=month, rows=rows)

@reports_bp.route("/kpi/members")
@login_required
def kpi_members():
    db = current_app.mongo_db
    pg_id = request.args.get("pg_id")
    metric = (request.args.get("metric") or "members_input").lower()
    if not pg_id or not ObjectId.is_valid(pg_id):
        flash("PG not provided.", "warning")
        return redirect(url_for("reports.hierarchy_dashboard"))

    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    members = list(db.pg_members.find({"pg_id": ObjectId(pg_id)}, {"name":1, "member_name":1, "gender":1, "caste":1, "phone":1}).limit(2000))

    # Note: current implementation stores only counts. Member-wise participation can be linked later
    # by storing member ids in pg_business_monthly details without changing these routes.
    return render_template("kpi_members.html", pg=pg, metric=metric, members=members)




# ============================================================
# Lakhpati Didi — Report + CSV Export (all roles)
# ============================================================
def _period_range(period: str):
    now = datetime.utcnow()
    period = (period or "").lower().strip()
    if period in ("weekly", "week", "7d"):
        return now - timedelta(days=7), now
    if period in ("monthly", "month", "30d"):
        return now - timedelta(days=30), now
    if period in ("6monthly", "halfyear", "6m", "180d"):
        return now - timedelta(days=183), now
    if period in ("yearly", "year", "12m", "365d"):
        return now - timedelta(days=365), now
    return None, None

def _distinct_pg_values(db, match_pg, field):
    try:
        vals = db.pgs.distinct(field, match_pg or {})
        vals = [v for v in vals if isinstance(v, str) and v.strip()]
        return sorted(set(vals))
    except Exception:
        return []


def _pg_match_with_filters(db, base_match: dict, filters: dict):
    match = dict(base_match or {})

    state = (filters.get("State") or filters.get("state") or "").strip()
    district = (filters.get("District") or filters.get("district") or "").strip()
    block = (filters.get("Block") or filters.get("block") or "").strip()
    gp = (filters.get("Gram Panchayat") or filters.get("gp") or "").strip()
    village = (filters.get("Village") or filters.get("village") or "").strip()
    pg_name = (filters.get("pg_name") or "").strip()

    if state:
        match["State"] = state

    if district:
        match["District"] = district

    if block:
        match["Block"] = block

    if gp:
        match["Gram Panchayat"] = gp

    if village:
        match["Village"] = village

    if pg_name:
        match["name"] = pg_name

    return match






#changes by atlanta
@reports_bp.route("/lakhpati-didi", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def lakhpati_report():
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    search_q = (request.args.get("q") or "").strip()
    period = (request.args.get("period") or "").strip()
    group_by = (request.args.get("group_by") or "").strip()

    match_pg = _pg_match_with_filters(db, base_match, filters)

    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC") and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(
        db.pgs.find(
            match_pg,
            {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}
        ).sort([("name", 1)])
    )
    pg_by_id = {p["_id"]: p for p in pgs}
    pg_ids = list(pg_by_id.keys())

    start, end = _period_range(period)
    member_match = {"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True} if pg_ids else {"pg_id": {"$in": []}, "lakh_pati_didi": True}
    if start and end:
        member_match["created_at"] = {"$gte": start, "$lte": end}
    if search_q:
        member_match["name"] = {"$regex": re.escape(search_q), "$options": "i"}

    members = list(db.pg_members.find(member_match).sort([("created_at", -1)]).limit(2000))

    rows = []
    for idx, m in enumerate(members, start=1):
        pg = pg_by_id.get(m.get("pg_id")) or {}
        rows.append({
            "_id": str(m.get("_id")),
            "sl_no": idx,
            "member_name": m.get("name") or "",
            "spouse_father_mother": m.get("spouse_name") or "",
            "contact": m.get("contact") or m.get("phone") or "",
            "pg_name": pg.get("name") or "",
            "state": pg.get("State") or "",
            "district": pg.get("District") or "",
            "block": pg.get("Block") or "",
            "gp": pg.get("Gram Panchayat") or "",
            "village": pg.get("Village") or "",
            "created_at": (m.get("created_at").strftime("%Y-%m-%d") if m.get("created_at") else ""),
        })

    dd_match = dict(base_match or {})
    states = _distinct_pg_values(db, dd_match, "State")
    districts = _distinct_pg_values(
        db,
        {**dd_match, **({"State": filters["State"]} if filters["State"] else {})},
        "District"
    )
    blocks = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"]}.items() if v})},
        "Block"
    )
    gps = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"]}.items() if v})},
        "Gram Panchayat"
    )
    villages = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"], "Gram Panchayat": filters["Gram Panchayat"]}.items() if v})},
        "Village"
    )

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if wants_json:
        return jsonify({
            "success": True,
            "title": "Lakhpati Didi — Report",
            "subtitle": "Filter by State → District → Block → GP → Village → PG • Search member name • Download CSV",
            "filters": {
                "state": filters["State"],
                "district": filters["District"],
                "block": filters["Block"],
                "gp": filters["Gram Panchayat"],
                "village": filters["Village"],
                "pg_name": filters["pg_name"],
                "q": search_q,
                "period": period,
                "group_by": group_by,
            },
            "dropdowns": {
                "states": states,
                "districts": districts,
                "blocks": blocks,
                "gps": gps,
                "villages": villages,
            },
            "rows": rows,
            "total": len(rows),
            "download_path": "/reports/export/lakhpati.csv",
        })

    web_rows = []
    for r in rows:
        web_rows.append({
            "Member Name": r["member_name"],
            "Spouse/Father/Mother": r["spouse_father_mother"],
            "Contact": r["contact"],
            "PG Name": r["pg_name"],
            "State": r["state"],
            "District": r["district"],
            "Block": r["block"],
            "Gram Panchayat": r["gp"],
            "Village": r["village"],
            "Created At": r["created_at"],
        })

    return render_template(
        "reports_lakhpati.html",
        rows=web_rows,
        filters=filters,
        q=search_q,
        period=period,
        states=states,
        districts=districts,
        blocks=blocks,
        gps=gps,
        villages=villages,
        total=len(web_rows),
        group_by=group_by,
    )

@reports_bp.route("/export/lakhpati.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_lakhpati_csv():
    # Reuse the same logic as page, but return CSV
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    search_q = (request.args.get("q") or "").strip()
    period = (request.args.get("period") or "").strip()

    match_pg = _pg_match_with_filters(db, base_match, filters)
    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC") and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}))
    pg_by_id = {p["_id"]: p for p in pgs}
    pg_ids = list(pg_by_id.keys())

    start, end = _period_range(period)
    member_match = {"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True} if pg_ids else {"pg_id": {"$in": []}, "lakh_pati_didi": True}
    if start and end:
        member_match["created_at"] = {"$gte": start, "$lte": end}
    if search_q:
        member_match["name"] = {"$regex": re.escape(search_q), "$options": "i"}

    members = list(db.pg_members.find(member_match).sort([("created_at", -1)]))

    # Optional: grouped summary mode
    mode = (request.args.get("mode") or "").strip().lower()  # summary | detail
    group_by = (request.args.get("group_by") or "").strip()  # State/District/Block/Gram Panchayat/Village/PG

    def _bucket_key(pg_doc):
        if not pg_doc:
            return ""
        if group_by.lower() in ("pg", "pg_name", "pgname"):
            return pg_doc.get("name") or ""
        if group_by in ("State", "District", "Block", "Gram Panchayat", "Village"):
            return pg_doc.get(group_by) or ""
        return ""

    if mode == "summary" and group_by:
        bucket = {}
        for m in members:
            pg = pg_by_id.get(m.get("pg_id")) or {}
            key = _bucket_key(pg) or "Unknown"
            bucket[key] = bucket.get(key, 0) + 1

        import csv
        from io import StringIO
        from flask import Response

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([group_by, "Lakhpati Didi Count"])
        for k in sorted(bucket.keys()):
            writer.writerow([k, bucket[k]])
        output.seek(0)
        filename = f"lakhpati_didi_summary_{group_by.replace(' ','_').lower()}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})

    import csv
    from io import StringIO
    from flask import Response
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Member Name","Spouse/Father/Mother","Contact","PG Name","State","District","Block","Gram Panchayat","Village","Created At"])
    for m in members:
        pg = pg_by_id.get(m.get("pg_id")) or {}
        writer.writerow([
            m.get("name") or "",
            m.get("spouse_name") or "",
            m.get("contact") or m.get("phone") or "",
            pg.get("name") or "",
            pg.get("State") or "",
            pg.get("District") or "",
            pg.get("Block") or "",
            pg.get("Gram Panchayat") or "",
            pg.get("Village") or "",
            (m.get("created_at").strftime("%Y-%m-%d") if m.get("created_at") else ""),
        ])
    output.seek(0)
    filename = f"lakhpati_didi_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})

# ============================================================
# Grants — Report + CSV Export (all roles)
# ============================================================

#changes by atlanta
@reports_bp.route("/grants", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def grants_report():
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    period = (request.args.get("period") or "").strip()
    # Used only for enabling the Summary CSV download button in the UI
    group_by = (request.args.get("group_by") or "").strip()

    match_pg = _pg_match_with_filters(db, base_match, filters)
    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC") and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}))
    pg_by_id = {p["_id"]: p for p in pgs}
    pg_ids = list(pg_by_id.keys())

    start, end = _period_range(period)

    grant_match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    if start and end:
        grant_match["created_at"] = {"$gte": start, "$lte": end}

    grants = list(db.pg_grants.find(grant_match).sort([("created_at", -1)]).limit(2000))

    # attach utilization sums and balance
    rows = []
    total_received = 0.0
    total_utilized = 0.0
    for g in grants:
        util = list(db.pg_grant_utilizations.aggregate([
            {"$match": {"grant_id": g["_id"]}},
            {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
        ]))
        utilized = float(util[0]["utilized"]) if util else 0.0
        received = float(g.get("amount_received") or 0.0)
        total_received += received
        total_utilized += utilized
        balance = received - utilized

        pg = pg_by_id.get(g.get("pg_id")) or {}

        rows.append({
            "PG Name": pg.get("name") or "",
            "State": pg.get("State") or "",
            "District": pg.get("District") or "",
            "Block": pg.get("Block") or "",
            "Gram Panchayat": pg.get("Gram Panchayat") or "",
            "Village": pg.get("Village") or "",
            "Category": g.get("category") or "",
            "Source": g.get("source") or "",
            "Release Date": g.get("release_date") or "",
            "Amount Received": received,
            "Utilized": utilized,
            "Balance": balance,
            "UC Status": g.get("uc_status") or "",
            "Created At": (g.get("created_at").strftime("%Y-%m-%d") if g.get("created_at") else ""),
        })

    dd_match = dict(base_match or {})
    states = _distinct_pg_values(db, dd_match, "State")
    districts = _distinct_pg_values(db, {**dd_match, **({"State": filters["State"]} if filters["State"] else {})}, "District")
    blocks = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"]}.items() if v})}, "Block")
    gps = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"]}.items() if v})}, "Gram Panchayat")
    villages = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"], "Gram Panchayat": filters["Gram Panchayat"]}.items() if v})}, "Village")

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    total_count = len(rows)
    total_received = round(total_received, 2)
    total_utilized = round(total_utilized, 2)
    total_balance = round(total_received - total_utilized, 2)

    if wants_json:
        return jsonify({
            "success": True,
            "title": "Grants & Utilization Report",
            "subtitle": "Filter by State, District, Block, GP, Village and PG. Download detailed or summary CSV directly in the mobile app.",
            "filters": {
                "state": filters["State"],
                "district": filters["District"],
                "block": filters["Block"],
                "gp": filters["Gram Panchayat"],
                "village": filters["Village"],
                "pg_name": filters["pg_name"],
                "period": period,
                "group_by": group_by,
            },
            "dropdowns": {
                "states": states,
                "districts": districts,
                "blocks": blocks,
                "gps": gps,
                "villages": villages,
            },
            "totals": {
                "rows": total_count,
                "received": total_received,
                "utilized": total_utilized,
                "balance": total_balance,
            },
            "rows": rows,
            "download_path": "/reports/export/grants.csv",
            "download_url": url_for(
                "reports.export_grants_csv",
                state=filters["State"],
                district=filters["District"],
                block=filters["Block"],
                gp=filters["Gram Panchayat"],
                village=filters["Village"],
                pg_name=filters["pg_name"],
                period=period,
                group_by=group_by,
                _external=True,
            ),
        })

    return render_template(
        "reports_grants.html",
        rows=rows,
        filters=filters,
        period=period,
        states=states,
        districts=districts,
        blocks=blocks,
        gps=gps,
        villages=villages,
        total=total_count,
        total_received=total_received,
        total_utilized=total_utilized,
        total_balance=total_balance,
        group_by=group_by,
    )

@reports_bp.route("/export/grants.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_grants_csv():
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }
    period = (request.args.get("period") or "").strip()

    match_pg = _pg_match_with_filters(db, base_match, filters)
    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC") and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}))
    pg_by_id = {p["_id"]: p for p in pgs}
    pg_ids = list(pg_by_id.keys())

    start, end = _period_range(period)
    grant_match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    if start and end:
        grant_match["created_at"] = {"$gte": start, "$lte": end}

    grants = list(db.pg_grants.find(grant_match).sort([("created_at", -1)]))

    # Optional: grouped summary mode
    mode = (request.args.get("mode") or "").strip().lower()  # summary | detail
    group_by = (request.args.get("group_by") or "").strip()  # State/District/Block/Gram Panchayat/Village/PG

    def _bucket_key(pg_doc):
        if not pg_doc:
            return ""
        if group_by.lower() in ("pg", "pg_name", "pgname"):
            return pg_doc.get("name") or ""
        if group_by in ("State", "District", "Block", "Gram Panchayat", "Village"):
            return pg_doc.get(group_by) or ""
        return ""

    import csv
    from io import StringIO
    from flask import Response
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["PG Name","State","District","Block","Gram Panchayat","Village","Category","Source","Release Date","Amount Received","Utilized","Balance","UC Status","Created At"])
    for g in grants:
        util = list(db.pg_grant_utilizations.aggregate([
            {"$match": {"grant_id": g["_id"]}},
            {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
        ]))
        utilized = float(util[0]["utilized"]) if util else 0.0
        received = float(g.get("amount_received") or 0.0)
        balance = received - utilized
        pg = pg_by_id.get(g.get("pg_id")) or {}
        writer.writerow([
            pg.get("name") or "",
            pg.get("State") or "",
            pg.get("District") or "",
            pg.get("Block") or "",
            pg.get("Gram Panchayat") or "",
            pg.get("Village") or "",
            g.get("category") or "",
            g.get("source") or "",
            g.get("release_date") or "",
            received,
            utilized,
            balance,
            g.get("uc_status") or "",
            (g.get("created_at").strftime("%Y-%m-%d") if g.get("created_at") else ""),
        ])
    output.seek(0)
    filename = f"grants_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})


# ============================================================
# PG Overall Export (ZIP with multiple CSVs) — all roles
# ============================================================

def _safe_oid(v):
    v = str(v) if v is not None else ""
    return v if ObjectId.is_valid(v) else None

# changes by atlanta
@reports_bp.route("/pg-overall", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN","CADRE_CC")
def pg_overall_report():
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }

    match_pg = _pg_match_with_filters(db, base_match, filters)

    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        sid = _safe_oid(session.get("pg_id") or session.get("active_pg_id"))
        if sid:
            match_pg["_id"] = ObjectId(sid)

    pgs = list(
        db.pgs.find(
            match_pg,
            {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}
        ).sort([("name", 1)]).limit(5000)
    )

    dd_match = dict(base_match or {})
    states = _distinct_pg_values(db, dd_match, "State")
    districts = _distinct_pg_values(
        db,
        {**dd_match, **({"State": filters["State"]} if filters["State"] else {})},
        "District"
    )
    blocks = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"]}.items() if v})},
        "Block"
    )
    gps = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"]}.items() if v})},
        "Gram Panchayat"
    )
    villages = _distinct_pg_values(
        db,
        {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"], "Gram Panchayat": filters["Gram Panchayat"]}.items() if v})},
        "Village"
    )

    pg_names = _distinct_pg_values(
        db,
        {
            **dd_match,
            **({
                k: v
                for k, v in {
                    "State": filters["State"],
                    "District": filters["District"],
                    "Block": filters["Block"],
                    "Gram Panchayat": filters["Gram Panchayat"],
                    "Village": filters["Village"],
                }.items()
                if v
            })
        },
        "name"
    )

    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if wants_json:
        return jsonify({
            "success": True,
            "title": "PG Overall Export",
            "subtitle": "Download a complete PG snapshot as a ZIP (Members, Cashbook, Ledger, Loans, Meetings, Grants, Utilization).",
            "filters": {
                "state": filters["State"],
                "district": filters["District"],
                "block": filters["Block"],
                "gp": filters["Gram Panchayat"],
                "village": filters["Village"],
                "pg_name": filters["pg_name"],
            },
            "dropdowns": {
                "states": states,
                "districts": districts,
                "blocks": blocks,
                "gps": gps,
                "villages": villages,
                "pg_names": pg_names,
            },
            "pgs": [
                {
                    "_id": str(p.get("_id")),
                    "name": p.get("name") or "",
                    "state": p.get("State") or "",
                    "district": p.get("District") or "",
                    "block": p.get("Block") or "",
                    "gp": p.get("Gram Panchayat") or "",
                    "village": p.get("Village") or "",
                }
                for p in pgs
            ],
            "download_path": "/reports/pg-overall/download",
            "tip": "Tip: Select a PG from the list and download its full ZIP. If you download without selecting a PG, it will export all PGs in the current filtered scope (may be large).",
        })

    return render_template(
        "reports_pg_overall.html",
        pgs=pgs,
        filters=filters,
        states=states,
        districts=districts,
        blocks=blocks,
        gps=gps,
        villages=villages,
        pg_names=pg_names,
    )


def _write_csv_rows(writer, fieldnames, docs):
    import json
    writer.writerow(fieldnames)
    for d in docs:
        row = []
        for f in fieldnames:
            v = d.get(f)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str, ensure_ascii=False)
            if f == "_id" and v is not None:
                v = str(v)
            row.append(v if v is not None else "")
        writer.writerow(row)

#changes by atlanta
@reports_bp.route("/pg-overall/download", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def pg_overall_download():
    """Download a ZIP containing multiple CSV files for the selected PG (or a filtered set)."""
    import csv
    import io
    import zipfile
    from datetime import datetime

    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    # Selected PG (preferred)
    selected_pg_id = request.args.get("pg_id")
    selected_pg_id = selected_pg_id if selected_pg_id and str(selected_pg_id).lower() != "none" else None

    # filters (fallback)
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }

    match_pg = _pg_match_with_filters(db, base_match, filters)
    role = session.get("role")
    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        sid = _safe_oid(session.get("pg_id") or session.get("active_pg_id"))
        if sid:
            match_pg["_id"] = ObjectId(sid)

    if selected_pg_id and ObjectId.is_valid(selected_pg_id):
        match_pg["_id"] = ObjectId(selected_pg_id)

    pgs = list(db.pgs.find(match_pg))
    if not pgs:
        flash("No PG found for export in your scope.", "warning")
        return redirect(url_for("reports.pg_overall_report"))
    
    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if not pgs:
        if wants_json:
            return jsonify({
                "success": False,
                "message": "No PG found for export in your scope."
            }), 404

        flash("No PG found for export in your scope.", "warning")
        return redirect(url_for("reports.pg_overall_report"))

    if wants_json:
        return jsonify({
            "success": True,
            "message": "PG Overall export is ready to download.",
            "download_url": url_for(
                "reports.pg_overall_download",
                state=request.args.get("state", ""),
                district=request.args.get("district", ""),
                block=request.args.get("block", ""),
                gp=request.args.get("gp", ""),
                village=request.args.get("village", ""),
                pg_name=request.args.get("pg_name", ""),
                pg_id=request.args.get("pg_id", ""),
            ),
            "selected_pg_id": request.args.get("pg_id", ""),
            "pg_count": len(pgs),
            "pg_names": [p.get("name") or "" for p in pgs[:50]],
        })

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for pg in pgs:
            pg_id = pg.get("_id")
            pg_name = (pg.get("name") or str(pg_id))
            safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", pg_name)[:60]

            # 1) PG profile
            buf = io.StringIO()
            w = csv.writer(buf)
            fieldnames = ["_id", "name", "State", "District", "Block", "Gram Panchayat", "Village", "clf_id", "block_id", "district_id", "state_id", "status", "created_at", "updated_at"]
            doc = {k: pg.get(k) for k in fieldnames}
            doc["_id"] = str(doc.get("_id"))
            for k in ("state_id", "district_id", "block_id", "clf_id"):
                if doc.get(k) is not None:
                    doc[k] = str(doc[k])
            _write_csv_rows(w, fieldnames, [doc])
            z.writestr(f"{safe_prefix}/pg_profile.csv", buf.getvalue())

            # 2) Members
            members = list(db.pg_members.find({"pg_id": pg_id}).sort([("name", 1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            m_fields = ["_id", "pg_id", "name", "spouse", "category", "shg_name", "contact", "photo_id_number", "bank_name", "branch", "account_number", "membership_fee_paid", "lakh_pati_didi", "created_at", "updated_at"]
            for m in members:
                m["_id"] = str(m.get("_id"))
                m["pg_id"] = str(m.get("pg_id"))
            _write_csv_rows(w, m_fields, members)
            z.writestr(f"{safe_prefix}/members.csv", buf.getvalue())

            # 3) Cashbook
            cashbooks = list(db.pg_cashbooks.find({"pg_id": pg_id}).sort([("year", -1), ("month", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            cb_fields = ["_id", "pg_id", "year", "month", "receipts", "payments", "created_at", "updated_at"]
            for c in cashbooks:
                c["_id"] = str(c.get("_id")); c["pg_id"] = str(c.get("pg_id"))
            _write_csv_rows(w, cb_fields, cashbooks)
            z.writestr(f"{safe_prefix}/cashbook.csv", buf.getvalue())

            # 4) Ledger Book
            ledgers = list(db.pg_ledger_books.find({"pg_id": pg_id}).sort([("created_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            l_fields = ["_id", "pg_id", "period", "rows", "created_at", "updated_at"]
            for l in ledgers:
                l["_id"] = str(l.get("_id")); l["pg_id"] = str(l.get("pg_id"))
            _write_csv_rows(w, l_fields, ledgers)
            z.writestr(f"{safe_prefix}/ledger_book.csv", buf.getvalue())

            # 5) Loan Ledger
            loans = list(db.pg_loan_ledgers.find({"pg_id": pg_id}).sort([("created_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            loan_fields = ["_id", "pg_id", "period", "rows", "created_at", "updated_at"]
            for l in loans:
                l["_id"] = str(l.get("_id")); l["pg_id"] = str(l.get("pg_id"))
            _write_csv_rows(w, loan_fields, loans)
            z.writestr(f"{safe_prefix}/loan_ledger.csv", buf.getvalue())

            # 6) Receipt Voucher
            rvs = list(db.pg_receipt_vouchers.find({"pg_id": pg_id}).sort([("created_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            rv_fields = ["_id", "pg_id", "period", "rows", "created_at", "updated_at"]
            for r in rvs:
                r["_id"] = str(r.get("_id")); r["pg_id"] = str(r.get("pg_id"))
            _write_csv_rows(w, rv_fields, rvs)
            z.writestr(f"{safe_prefix}/receipt_voucher.csv", buf.getvalue())

            # 7) Meetings & Minutes
            meetings = list(db.pg_meetings.find({"pg_id": pg_id}).sort([("meeting_date", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            meet_fields = ["_id", "pg_id", "meeting_date", "title", "agenda", "attendance", "created_at"]
            for m in meetings:
                m["_id"] = str(m.get("_id")); m["pg_id"] = str(m.get("pg_id"))
            _write_csv_rows(w, meet_fields, meetings)
            z.writestr(f"{safe_prefix}/meetings.csv", buf.getvalue())

            minutes = list(db.pg_meeting_minutes.find({"pg_id": pg_id}).sort([("created_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            min_fields = ["_id", "pg_id", "meeting_date", "content", "created_at"]
            for m in minutes:
                m["_id"] = str(m.get("_id")); m["pg_id"] = str(m.get("pg_id"))
            _write_csv_rows(w, min_fields, minutes)
            z.writestr(f"{safe_prefix}/meeting_minutes.csv", buf.getvalue())

            # 8) Grants & Utilizations
            grants = list(db.pg_grants.find({"pg_id": pg_id}).sort([("created_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            g_fields = ["_id", "pg_id", "category", "source", "release_date", "amount_received", "uc_status", "created_at"]
            for g in grants:
                g["_id"] = str(g.get("_id")); g["pg_id"] = str(g.get("pg_id"))
            _write_csv_rows(w, g_fields, grants)
            z.writestr(f"{safe_prefix}/grants.csv", buf.getvalue())

            utils = list(db.pg_grant_utilizations.find({"pg_id": pg_id}).sort([("utilized_at", -1)]))
            buf = io.StringIO(); w = csv.writer(buf)
            u_fields = ["_id", "grant_id", "pg_id", "amount", "utilized_at", "head", "remarks", "attachments", "status", "approved_at", "rejected_at"]
            for u in utils:
                u["_id"] = str(u.get("_id"));
                if u.get("grant_id") is not None: u["grant_id"] = str(u.get("grant_id"))
                if u.get("pg_id") is not None: u["pg_id"] = str(u.get("pg_id"))
            _write_csv_rows(w, u_fields, utils)
            z.writestr(f"{safe_prefix}/grant_utilizations.csv", buf.getvalue())

    mem.seek(0)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"pg_overall_export_{stamp}.zip"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype="application/zip")


