import os
from datetime import datetime
from flask import render_template, current_app, session
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

@reports_bp.route("/state_dashboard")
@login_required
@roles_required("SUPER_ADMIN", "ADMIN")
def state_dashboard():
    db = current_app.mongo_db
    state_id = session.get("state_id")

    match_stage = {}
    if state_id:
        match_stage["state_id"] = ObjectId(state_id)

    pg_count = db.pgs.count_documents(match_stage)
    member_count = db.pg_members.count_documents(match_stage if match_stage else {})

    # SHG master counts (LokOS imported)
    shg_q = _shg_filter_from_session(db, session)
    shg_total = db.shg_master.count_documents(shg_q)
    shg_active = db.shg_master.count_documents({**shg_q, "Status": "Active"})

    pipeline = [
        {"$match": {"level": "state", "ref_id": state_id}} if state_id else {"$match": {"level": "state"}},
        {"$group": {"_id": None, "total_turnover": {"$sum": "$monthly_turnover"}}},
    ]
    agg = list(db.mpr_snapshots.aggregate(pipeline))
    total_turnover = agg[0]["total_turnover"] if agg else 0

    # Top districts by SHG count (within state if applicable)
    pipe = []
    if shg_q:
        pipe.append({"$match": shg_q})
    pipe += [
        {"$group": {"_id": "$District", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 12},
    ]
    top_districts = list(db.shg_master.aggregate(pipe))

    return render_template(
        "dashboard_state.html",
        pg_count=pg_count,
        member_count=member_count,
        total_turnover=total_turnover,
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top_districts=top_districts,
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
        pgs=pgs,
        shg_total=shg_total,
        shg_active=shg_active,
        shg_top=shg_top,
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
