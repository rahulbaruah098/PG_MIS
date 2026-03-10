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


@reports_bp.route("/hub", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def reports_hub():
    """Unified download hub for reports (CSV/ZIP).

    Keeps existing report pages intact; this is an additional navigation entry.
    """
    return render_template("reports_hub.html")


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
    if role == "PG_DATA_ENTRY" and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))
    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}))
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



@reports_bp.route("/state_dashboard")
@login_required
@roles_required("SUPER_ADMIN", "ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN")
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

    # ✅ Profit + Loss (cumulative) from Income–Expenditure (separate)
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

    # ✅ Grants (cumulative) + number of PGs that received any grants
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

    # ✅ Lakhpati Didi total (members)
    lakhpati_total = 0
    try:
        if pg_ids:
            lakhpati_total = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True})
    except Exception:
        lakhpati_total = 0

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

    charts = {
        "pg_by_state": {"labels": [x["name"] for x in pg_by_state], "values": [x["count"] for x in pg_by_state]},
        "pg_by_district": {"labels": [x["name"] for x in pg_by_district], "values": [x["count"] for x in pg_by_district]},
        "pg_by_block": {"labels": [x["name"] for x in pg_by_block], "values": [x["count"] for x in pg_by_block]},
        "turnover_ts": {"labels": turnover_ts.get("labels", []), "values": turnover_ts.get("values", [])},
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
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top_districts=top_districts,
        charts=charts,
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
    if clf_id:
        query["clf_id"] = ObjectId(clf_id)
    elif block_id:
        query["block_id"] = ObjectId(block_id)
    elif district_id:
        query["district_id"] = ObjectId(district_id)
    elif state_id:
        query["state_id"] = ObjectId(state_id)

    pgs = list(db.pgs.find(query).sort([("created_at", -1)]).limit(200))
    pg_ids = [pg["_id"] for pg in pgs]
    pg_count = len(pg_ids)
    member_count = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}}) if pg_ids else 0

    # ✅ Turnover / Profit-Loss / Grants / Lakhpati (scope)
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
        pgs=pgs,
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top=shg_top,
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

@reports_bp.route("/pg_mpr/<pg_id>")
@login_required
def pg_mpr(pg_id):
    db = current_app.mongo_db
    snapshots = list(db.mpr_snapshots.find({"level": "pg", "ref_id": pg_id}).sort([("year", -1), ("month", -1)]))
    return render_template("pg_mpr.html", snapshots=snapshots)


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
# NEW: PG GRADATION SYSTEM (Quarterly scoring)
# - Governance + Financial + Business + Meetings
# - Stored snapshot per quarter (year+quarter)
# ============================================================

def _compute_gradation(db, pg_id: str, year: int, quarter: int):
    pg_oid = ObjectId(pg_id)
    # Governance: meetings count in quarter
    q_months = {1:(1,3),2:(4,6),3:(7,9),4:(10,12)}.get(int(quarter),(1,3))
    m_start, m_end = q_months
    meetings = list(db.pg_meetings.find({"pg_id": pg_oid, "meeting_date": {"$regex": f"^{year}-"}}))
    meet_q = 0
    for m in meetings:
        try:
            mo = int(str(m.get("meeting_date") or "").split("-")[1])
            if m_start <= mo <= m_end:
                meet_q += 1
        except Exception:
            pass

    # Financial: overdue loans / repayment regularity
    loans = list(db.pg_loan_accounts.find({"pg_id": pg_oid}))
    overdue = sum(float(l.get("overdue_amount") or 0) for l in loans)
    outstanding = sum(float(l.get("outstanding_amount") or 0) for l in loans)

    # Business: turnover in quarter (from market transactions monthly)
    agg = list(db.pg_market_transactions.aggregate([
        {"$match": {"pg_id": pg_oid, "year": int(year), "month": {"$gte": m_start, "$lte": m_end}}},
        {"$group": {"_id": None, "turnover": {"$sum": "$total_turnover"}}}
    ]))
    turnover = float(agg[0]["turnover"] if agg else 0)

    # Simple scoring rules (can be tuned later)
    score = 0
    breakdown = {}

    # meetings (0-25)
    breakdown["meetings"] = min(meet_q, 5) * 5  # 5 meetings => 25
    # financial (0-35)
    breakdown["financial"] = 35 if overdue <= 0 else max(0, 35 - min(35, int(overdue/1000)))
    # business (0-30)
    breakdown["business"] = 0
    if turnover >= 500000:
        breakdown["business"] = 30
    elif turnover >= 200000:
        breakdown["business"] = 22
    elif turnover >= 50000:
        breakdown["business"] = 14
    elif turnover > 0:
        breakdown["business"] = 8
    # governance placeholder (0-10) — later link to resolutions, attendance avg, etc.
    breakdown["governance"] = 10 if meet_q >= 1 else 4

    score = sum(breakdown.values())
    grade = "D"
    if score >= 80: grade = "A"
    elif score >= 65: grade = "B"
    elif score >= 50: grade = "C"

    return {
        "year": int(year),
        "quarter": int(quarter),
        "score": score,
        "grade": grade,
        "breakdown": breakdown,
        "metrics": {"meetings": meet_q, "overdue": overdue, "outstanding": outstanding, "turnover": turnover}
    }

@reports_bp.route("/gradation/<pg_id>", methods=["GET","POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def gradation(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": ObjectId(pg_id)})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    year = int(request.values.get("year") or datetime.utcnow().year)
    quarter = int(request.values.get("quarter") or ((datetime.utcnow().month-1)//3 + 1))

    # PG can view gradation, but only CLF+ and above can compute/save
    if request.method == "POST" and session.get("role") == "PG_DATA_ENTRY":
        flash("Gradation can only be submitted by CLF/Block authorities.", "warning")
        return redirect(url_for("reports.gradation", pg_id=pg_id, year=year, quarter=quarter))

    if request.method == "POST":
        snap = _compute_gradation(db, pg_id, year, quarter)
        snap.update({"pg_id": ObjectId(pg_id), "updated_at": datetime.utcnow(), "created_at": datetime.utcnow()})
        db.pg_gradation_snapshots.update_one(
            {"pg_id": ObjectId(pg_id), "year": year, "quarter": quarter},
            {"$set": snap},
            upsert=True
        )
        flash("Gradation computed and saved.", "success")
        return redirect(url_for("reports.gradation", pg_id=pg_id, year=year, quarter=quarter))

    snap = db.pg_gradation_snapshots.find_one({"pg_id": ObjectId(pg_id), "year": year, "quarter": quarter})
    computed = _compute_gradation(db, pg_id, year, quarter)
    return render_template("gradation.html", pg=pg, snap=snap, computed=computed, year=year, quarter=quarter)


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



@reports_bp.route("/workflow/submit/<pg_id>/<int:year>/<int:month>/<module>", methods=["POST"])
def submit_monthly(pg_id, year, month, module):
    """PG Data Entry submits a month's module data to CLF."""
    note = request.form.get("note","").strip()
    submit_to_clf(pg_id, year, month, module, note=note)
    flash(f"Submitted {module.upper()} for {month:02d}/{year} to CLF.", "success")
    return redirect(request.referrer or url_for("reports.pg_mpr", pg_id=pg_id))

@reports_bp.route("/workflow/approve/<pg_id>/<int:year>/<int:month>/<module>/<level>", methods=["POST"])
def approve_monthly(pg_id, year, month, module, level):
    """CLF/Block/District approve a submission."""
    remark = request.form.get("remark","").strip()
    approve(pg_id, year, month, module, level=level, remark=remark)
    flash(f"Approved {module.upper()} for {month:02d}/{year} at {level.upper()} level.", "success")
    return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))

@reports_bp.route("/workflow/reject/<pg_id>/<int:year>/<int:month>/<module>/<level>", methods=["POST"])
def reject_monthly(pg_id, year, month, module, level):
    remark = request.form.get("remark","").strip()
    reject(pg_id, year, month, module, level=level, remark=remark)
    flash(f"Rejected {module.upper()} for {month:02d}/{year} at {level.upper()} level.", "warning")
    return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))

@reports_bp.route("/workflow/status/<pg_id>/<int:year>/<int:month>/<module>")
def workflow_status(pg_id, year, month, module):
    doc = get_submission(pg_id, year, month, module) or {}
    return jsonify({"ok": True, "submission": doc})



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

    try:
        now = datetime.utcnow()
        year = int(year) if year else now.year
        month = int(month) if month else now.month
    except Exception:
        now = datetime.utcnow()
        year, month = now.year, now.month

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

    for pg in pgs:

        pg_id = str(pg["_id"])

        for module in modules:

            sub = db.submissions.find_one({
                "pg_id": pg_id,
                "year": year,
                "month": month,
                "module": module
            }) or {}

            rows.append({
                "pg_id": pg_id,
                "pg_name": pg.get("name","(PG)"),
                "module": module,
                "status": sub.get("status","—")
            })

    return render_template(
        "clf_console.html",
        rows=rows,
        year=year,
        month=month
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

def _pg_match_with_filters(db, base_match, filters):
    q = dict(base_match or {})
    # String geo fields (from SHG master import)
    for k in ("State", "District", "Block", "Gram Panchayat", "Village"):
        v = (filters.get(k) or "").strip()
        if v:
            q[k] = v
    pg_name = (filters.get("pg_name") or "").strip()
    if pg_name:
        q["name"] = {"$regex": re.escape(pg_name), "$options": "i"}
    return q

@reports_bp.route("/lakhpati-didi", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def lakhpati_report():
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    # filters
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
    group_by = (request.args.get("group_by") or "").strip()

    match_pg = _pg_match_with_filters(db, base_match, filters)

    # PG user: lock to their PG
    role = session.get("role")
    if role == "PG_DATA_ENTRY" and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
        match_pg["_id"] = ObjectId(session.get("pg_id"))

    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}).sort([("name", 1)]))
    pg_by_id = {p["_id"]: p for p in pgs}
    pg_ids = list(pg_by_id.keys())

    # Date filter
    start, end = _period_range(period)
    member_match = {"pg_id": {"$in": pg_ids}, "lakh_pati_didi": True} if pg_ids else {"pg_id": {"$in": []}, "lakh_pati_didi": True}
    if start and end:
        member_match["created_at"] = {"$gte": start, "$lte": end}
    if search_q:
        member_match["name"] = {"$regex": re.escape(search_q), "$options": "i"}

    members = list(db.pg_members.find(member_match).sort([("created_at", -1)]).limit(2000))

    rows = []
    for m in members:
        pg = pg_by_id.get(m.get("pg_id")) or {}
        rows.append({
            "Member Name": m.get("name") or "",
            "Spouse/Father/Mother": m.get("spouse_name") or "",
            "Contact": m.get("contact") or m.get("phone") or "",
            "PG Name": pg.get("name") or "",
            "State": pg.get("State") or "",
            "District": pg.get("District") or "",
            "Block": pg.get("Block") or "",
            "Gram Panchayat": pg.get("Gram Panchayat") or "",
            "Village": pg.get("Village") or "",
            "Created At": (m.get("created_at").strftime("%Y-%m-%d") if m.get("created_at") else ""),
        })

    # Dropdown sources (within jurisdiction + current filters up to that level)
    dd_match = dict(base_match or {})
    states = _distinct_pg_values(db, dd_match, "State")
    districts = _distinct_pg_values(db, {**dd_match, **({"State": filters["State"]} if filters["State"] else {})}, "District")
    blocks = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"]}.items() if v})}, "Block")
    gps = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"]}.items() if v})}, "Gram Panchayat")
    villages = _distinct_pg_values(db, {**dd_match, **({k:v for k,v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"], "Gram Panchayat": filters["Gram Panchayat"]}.items() if v})}, "Village")

    return render_template(
        "reports_lakhpati.html",
        rows=rows,
        filters=filters,
        q=search_q,
        period=period,
        states=states,
        districts=districts,
        blocks=blocks,
        gps=gps,
        villages=villages,
        total=len(rows),
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
    if role == "PG_DATA_ENTRY" and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
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
    if role == "PG_DATA_ENTRY" and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
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
        total=len(rows),
        total_received=round(total_received, 2),
        total_utilized=round(total_utilized, 2),
        total_balance=round(total_received - total_utilized, 2),
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
    if role == "PG_DATA_ENTRY" and session.get("pg_id") and ObjectId.is_valid(session.get("pg_id")):
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


@reports_bp.route("/pg-overall", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def pg_overall_report():
    """UI page to download a full PG snapshot (multiple CSVs inside a ZIP)."""
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)

    # filters
    filters = {
        "State": request.args.get("state") or "",
        "District": request.args.get("district") or "",
        "Block": request.args.get("block") or "",
        "Gram Panchayat": request.args.get("gp") or "",
        "Village": request.args.get("village") or "",
        "pg_name": request.args.get("pg_name") or "",
    }

    match_pg = _pg_match_with_filters(db, base_match, filters)

    # PG user: lock to their PG
    role = session.get("role")
    if role == "PG_DATA_ENTRY":
        sid = _safe_oid(session.get("pg_id") or session.get("active_pg_id"))
        if sid:
            match_pg["_id"] = ObjectId(sid)

    pgs = list(db.pgs.find(match_pg, {"name": 1, "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1}).sort([("name", 1)]).limit(5000))

    dd_match = dict(base_match or {})
    states = _distinct_pg_values(db, dd_match, "State")
    districts = _distinct_pg_values(db, {**dd_match, **({"State": filters["State"]} if filters["State"] else {})}, "District")
    blocks = _distinct_pg_values(db, {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"]}.items() if v})}, "Block")
    gps = _distinct_pg_values(db, {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"]}.items() if v})}, "Gram Panchayat")
    villages = _distinct_pg_values(db, {**dd_match, **({k: v for k, v in {"State": filters["State"], "District": filters["District"], "Block": filters["Block"], "Gram Panchayat": filters["Gram Panchayat"]}.items() if v})}, "Village")

    return render_template(
        "reports_pg_overall.html",
        pgs=pgs,
        filters=filters,
        states=states,
        districts=districts,
        blocks=blocks,
        gps=gps,
        villages=villages,
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
    if role == "PG_DATA_ENTRY":
        sid = _safe_oid(session.get("pg_id") or session.get("active_pg_id"))
        if sid:
            match_pg["_id"] = ObjectId(sid)

    if selected_pg_id and ObjectId.is_valid(selected_pg_id):
        match_pg["_id"] = ObjectId(selected_pg_id)

    pgs = list(db.pgs.find(match_pg))
    if not pgs:
        flash("No PG found for export in your scope.", "warning")
        return redirect(url_for("reports.pg_overall_report"))

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