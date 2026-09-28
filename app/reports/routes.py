from services.audit_engine import AuditLogger
import os
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app, send_file, g
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
CADRE_TYPE_OPTIONS = [
    "Pashu Sakhi",
    "Matshya Sakhi",
    "Krishi Sakhi",
    "MBK",
]


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

def _reports_ctx_value(key, default=None):
    """
    Reports Hub context reader.

    Web login      => Flask session
    Mobile app JWT => flask.g populated by rbac/login_required
    """
    val = getattr(g, key, None)
    if val is not None and str(val).strip() != "":
        return val

    val = session.get(key)
    if val is not None and str(val).strip() != "":
        return val

    return default

def _reports_hub_defaults_from_session(db):
    """
    Build default Reports Hub filters from logged-in user's scope.

    Web:
      - Reads from Flask session.

    Mobile app:
      - Reads from flask.g because JWT auth stores user scope there.

    State/Admin:
      - State auto-filled.

    PG Entry:
      - State/District/Block/GP/Village/PG auto-filled from own PG.
    """
    role = _reports_ctx_value("role", "")
    defaults = {
        "state": "",
        "district": "",
        "block": "",
        "clf": "",
        "gp": "",
        "village": "",
        "pg_name": "",
    }

    locked_fields = []

    # PG Entry: lock everything to its own PG
    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        pg = None

        raw_pg_id = (
            _reports_ctx_value("pg_id")
            or _reports_ctx_value("active_pg_id")
        )

        if raw_pg_id and ObjectId.is_valid(str(raw_pg_id)):
            pg = db.pgs.find_one(
                {"_id": ObjectId(str(raw_pg_id))},
        {
                "name": 1,
                "State": 1,
                "District": 1,
                "Block": 1,
                "Gram Panchayat": 1,
                "Village": 1,
                "clf_id": 1,
                "assigned_clf_user_id": 1,
                "assigned_clf_username": 1,
        },
    )

        # fallback: if JWT has assigned_pg_ids but no direct pg_id
        if not pg:
            assigned_pg_ids = (
                getattr(g, "assigned_pg_ids", None)
                or session.get("assigned_pg_ids")
                or []
            )

            possible_pg_id = None
            if isinstance(assigned_pg_ids, list) and assigned_pg_ids:
                possible_pg_id = assigned_pg_ids[0]

            if possible_pg_id and ObjectId.is_valid(str(possible_pg_id)):
                pg = db.pgs.find_one(
                {"_id": ObjectId(str(possible_pg_id))},
                {
                "name": 1,
                "State": 1,
                "District": 1,
                "Block": 1,
                "Gram Panchayat": 1,
                "Village": 1,
                "clf_id": 1,
                "assigned_clf_user_id": 1,
                "assigned_clf_username": 1,
        },
    )

        if pg:
            clf_name = ""

            try:
                clf_id = (
                    pg.get("clf_id")
                    or pg.get("clfId")
                    or pg.get("mapped_clf_id")
                    or pg.get("assigned_clf_id")
                )
                assigned_clf_user_id = pg.get("assigned_clf_user_id")

                clf_doc = None

                # Case 1: PG document directly stores clf_id
                if clf_id:
                    if isinstance(clf_id, ObjectId):
                        clf_doc = db.clfs.find_one(
                            {"_id": clf_id},
                            {"name": 1, "clf_name": 1, "CLF Name": 1},
                        )
                    elif ObjectId.is_valid(str(clf_id)):
                        clf_doc = db.clfs.find_one(
                            {"_id": ObjectId(str(clf_id))},
                            {"name": 1, "clf_name": 1, "CLF Name": 1},
                        )

                # Case 2 fallback: CLF document stores assigned PG ids
                if not clf_doc:
                    pg_oid = pg.get("_id")
                    pg_oid_str = str(pg_oid) if pg_oid else ""

                    clf_doc = db.clfs.find_one(
                        {
                            "$or": [
                                {"pg_ids": pg_oid},
                                {"pg_ids": pg_oid_str},
                                {"assigned_pg_ids": pg_oid},
                                {"assigned_pg_ids": pg_oid_str},
                                {"mapped_pg_ids": pg_oid},
                                {"mapped_pg_ids": pg_oid_str},
                                {"pgs": pg_oid},
                                {"pgs": pg_oid_str},
                            ]
                        },
                        {"name": 1, "clf_name": 1, "CLF Name": 1},
                    )

                if clf_doc:
                    clf_name = (
                        clf_doc.get("name")
                        or clf_doc.get("clf_name")
                        or clf_doc.get("CLF Name")
                        or ""
                    )

            except Exception:
                clf_name = ""

            defaults.update({
                "state": pg.get("State") or "",
                "district": pg.get("District") or "",
                "block": pg.get("Block") or "",
                "clf": clf_name,
                "gp": pg.get("Gram Panchayat") or "",
                "village": pg.get("Village") or "",
                "pg_name": pg.get("name") or "",
            })

        locked_fields = ["state", "district", "block", "clf", "gp", "village", "pg"]
        return defaults, locked_fields

    # Block Admin: lock State/District/Block from login scope.
    # Below Block, user can select CLF / GP / Village / PG under that block.
    if role == "BLOCK_ADMIN":
        state_id = _reports_ctx_value("state_id")
        district_id = _reports_ctx_value("district_id")
        block_id = _reports_ctx_value("block_id")

        try:
            if block_id and ObjectId.is_valid(str(block_id)):
                blk = db.blocks.find_one(
                    {"_id": ObjectId(str(block_id))},
                    {"name": 1, "district_id": 1},
                )
                if blk:
                    defaults["block"] = blk.get("name") or ""

                    if not district_id and blk.get("district_id"):
                        district_id = str(blk.get("district_id"))

            if district_id and ObjectId.is_valid(str(district_id)):
                dist = db.districts.find_one(
                    {"_id": ObjectId(str(district_id))},
                    {"name": 1, "state_id": 1},
                )
                if dist:
                    defaults["district"] = dist.get("name") or ""

                    if not state_id and dist.get("state_id"):
                        state_id = str(dist.get("state_id"))

            if state_id and ObjectId.is_valid(str(state_id)):
                st = db.states.find_one(
                    {"_id": ObjectId(str(state_id))},
                    {"name": 1, "code": 1},
                )
                if st:
                    defaults["state"] = st.get("name") or st.get("code") or ""

        except Exception:
            current_app.logger.exception("Reports Hub Block Admin default scope failed")

        locked_fields = ["state", "district", "block"]
        return defaults, locked_fields

    # State/Admin scoped users
    state_id = _reports_ctx_value("state_id")

    if state_id and ObjectId.is_valid(str(state_id)):
        st = db.states.find_one(
            {"_id": ObjectId(str(state_id))},
            {"name": 1, "code": 1},
        )
        if st:
            defaults["state"] = st.get("name") or st.get("code") or ""

    if defaults["state"] and role in ("ADMIN", "STATE_ADMIN"):
        locked_fields = ["state"]

    return defaults, locked_fields

def _reports_hub_effective_filters(db):
    defaults, locked_fields = _reports_hub_defaults_from_session(db)

    filters = {
        "state": request.args.get("state") or defaults["state"],
        "district": request.args.get("district") or defaults["district"],
        "block": request.args.get("block") or defaults["block"],
        "clf": request.args.get("clf") or defaults.get("clf", ""),
        "gp": request.args.get("gp") or defaults["gp"],
        "village": request.args.get("village") or defaults["village"],
        "pg_name": request.args.get("pg_name") or defaults["pg_name"],
        "period": request.args.get("period", ""),
        "group_by": request.args.get("group_by", ""),
        "q": request.args.get("q", ""),
        "from": request.args.get("from", ""),
        "to": request.args.get("to", ""),
    }

    # Locked fields must always use session-derived values, not user URL values
    for field in locked_fields:
        if field == "pg":
            filters["pg_name"] = defaults["pg_name"]
        else:
            filters[field] = defaults[field]

    return filters, locked_fields

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
    db = current_app.mongo_db
    filters, locked_fields = _reports_hub_effective_filters(db)
    role = _reports_ctx_value("role", "")
    wants_json = (
        request.args.get("format") == "json"
        or request.args.get("mobile") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    payload = {
        "success": True,
        "title": "Reports Hub",
        "subtitle": "Download CSV/ZIP reports with your jurisdiction filters.",
        "role": role,
        "locked_fields": locked_fields,
        "filters": filters,
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
            # "lakhpati_csv": "/reports/export/lakhpati.csv",
            "grants_csv": "/reports/export/grants.csv",
            "members_csv": "/reports/export/members.csv",
            "cashbook_csv": "/reports/export/cashbook.csv",
            "loans_csv": "/reports/export/loans.csv",
            "turnover_csv": "/reports/export/turnover.csv",
        },
        "quick_reports": [
            # {
            #     "title": "Lakhpati Didi",
            #     "subtitle": "Member-level + grouped summary",
            #     "path": "/reports/lakhpati",
            # },
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
    }

    if wants_json:
        return jsonify(payload)

    return render_template(
        "reports_hub.html",
        hub_filters=filters,
        hub_locked_fields=locked_fields,
        hub_role=role,
    )

# -------------------------------------------------------------------
# Reports Hub Dropdown Backend
# -------------------------------------------------------------------

#changes by atlanta
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
    clf = (request.args.get("clf") or "").strip()
    gp = (request.args.get("gp") or "").strip()
    village = (request.args.get("village") or "").strip()

    allowed_levels = {"state", "district", "block", "clf", "gp", "village", "pg"}

    if level not in allowed_levels:
        return jsonify({
            "success": False,
            "message": "Invalid dropdown level.",
            "options": [],
        }), 400

    role = _reports_ctx_value("role", "")
    defaults, locked_fields = _reports_hub_defaults_from_session(db)

    # PG Entry: return only its own fixed values
    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        fixed = {
            "state": defaults.get("state", ""),
            "district": defaults.get("district", ""),
            "block": defaults.get("block", ""),
            "clf": defaults.get("clf", ""),
            "gp": defaults.get("gp", ""),
            "village": defaults.get("village", ""),
            "pg": defaults.get("pg_name", ""),
        }

        value = fixed.get(level, "")
        return jsonify({
            "success": True,
            "level": level,
            "locked": True,
            "options": [value] if value else [],
        })

    field_map = {
        "state": "State",
        "district": "District",
        "block": "Block",
        "gp": "Gram Panchayat",
        "village": "Village",
        "pg": "name",
    }

    mongo_filter = {}

    # State Admin: always keep logged-in state fixed
    if defaults.get("state"):
        mongo_filter["State"] = defaults["state"]

    # Apply dropdown selections
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


    if level == "clf":
        try:
            clf_filter = {}

            block_name_for_clf = block or defaults.get("block") or ""
            block_id_for_clf = _reports_ctx_value("block_id")

            if block_id_for_clf and ObjectId.is_valid(str(block_id_for_clf)):
                clf_filter["block_id"] = ObjectId(str(block_id_for_clf))
            elif block_name_for_clf:
                block_doc = db.blocks.find_one({"name": block_name_for_clf}, {"_id": 1})
                if block_doc:
                    clf_filter["block_id"] = block_doc["_id"]

            rows = list(
                db.clfs.find(
                    clf_filter,
                    {"name": 1, "clf_name": 1, "CLF Name": 1},
                ).sort([("name", 1), ("clf_name", 1)])
            )

            options = []
            seen = set()

            for row in rows:
                label = (
                    row.get("name")
                    or row.get("clf_name")
                    or row.get("CLF Name")
                    or ""
                )
                label = str(label).strip()

                if label and label not in seen:
                    seen.add(label)
                    options.append(label)

            return jsonify({
                "success": True,
                "level": level,
                "locked": level in locked_fields,
                "options": options,
            })

        except Exception as e:
            current_app.logger.exception("Reports Hub CLF dropdown loading failed")
            return jsonify({
                "success": False,
                "message": str(e),
                "level": level,
                "options": [],
            }), 500

    if clf:
        try:
            clf_doc = db.clfs.find_one({
                "$or": [
                    {"name": clf},
                    {"clf_name": clf},
                    {"CLF Name": clf},
                ]
            }, {"_id": 1})

            if clf_doc and clf_doc.get("_id"):
                mongo_filter["clf_id"] = clf_doc["_id"]
        except Exception:
            pass

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
            "locked": level in locked_fields,
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

    role = _reports_ctx_value("role", "")
    pg_id = _reports_ctx_value("pg_id") or _reports_ctx_value("active_pg_id")

    if role in ("PG_DATA_ENTRY", "CADRE_CC") and pg_id and ObjectId.is_valid(str(pg_id)):
        match_pg = {"_id": ObjectId(str(pg_id))}

    pgs = list(db.pgs.find(
        match_pg,
        {
    "name": 1,
    "State": 1,
    "District": 1,
    "Block": 1,
    "Gram Panchayat": 1,
    "Village": 1,
    "clf_id": 1,
},
    ))

    pg_by_id = {p["_id"]: p for p in pgs}

    return list(pg_by_id.keys()), pg_by_id

def _reports_export_filters(db):
    """
    Shared safe filters for Reports Hub CSV downloads.
    """
    effective, _locked_fields = _reports_hub_effective_filters(db)

    return {
        "State": effective.get("state") or "",
        "District": effective.get("district") or "",
        "Block": effective.get("block") or "",
        "clf": effective.get("clf") or "",
        "Gram Panchayat": effective.get("gp") or "",
        "Village": effective.get("village") or "",
        "pg_name": effective.get("pg_name") or "",
    }

@reports_bp.route("/export/members.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_members_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = _reports_export_filters(db)
    group_by = (request.args.get("group_by") or "").strip().lower()
    q = (request.args.get("q") or "").strip()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)
    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    if q:
        match["$or"] = [{field: {"$regex": re.escape(q), "$options": "i"}} for field in ("name", "member_name", "shg_name")]
    _apply_period(match, "created_at", dict(request.args))
    mem = io.StringIO(newline="")
    w = csv.writer(mem)
    if group_by:
        def gkey(pg):
            return {"district": pg.get("District"), "block": pg.get("Block"), "gp": pg.get("Gram Panchayat"), "village": pg.get("Village"), "pg": pg.get("name")}.get(group_by, "") or ""
        agg = {}
        for mdoc in db.pg_members.find(match, {"pg_id": 1}):
            pg = pg_by_id.get(mdoc.get("pg_id")) or {}
            key = gkey(pg)
            agg[key] = agg.get(key, 0) + 1
        w.writerow(["Group", "Members Count"])
        for key in sorted(agg):
            w.writerow([key, agg[key]])
    else:
        fields = ["PG Name", "State", "District", "Block", "Gram Panchayat", "Village", "Member Name", "SHG Name", "Category", "Contact", "Crop (Agri)", "FFS Module", "ARDD Activity", "ARDD Unit", "Fishery Activity"]
        w.writerow(fields)
        for mdoc in db.pg_members.find(match).sort([("name", 1)]):
            pg = pg_by_id.get(mdoc.get("pg_id")) or {}
            master = {}
            member_ref = mdoc.get("shg_member_id") or mdoc.get("member_id")
            if member_ref:
                try:
                    member_oid = member_ref if isinstance(member_ref, ObjectId) else ObjectId(str(member_ref))
                    master = db.shg_members_master.find_one({"_id": member_oid}) or {}
                except Exception:
                    master = db.shg_members_master.find_one({"_id": member_ref}) or {}
            contact = (mdoc.get("contact") or mdoc.get("phone") or mdoc.get("contact_number") or master.get("Contact") or master.get("Contact Number") or master.get("Mobile Number") or master.get("Phone") or master.get("contact") or master.get("phone") or "")
            w.writerow([pg.get("name") or "", pg.get("State") or "", pg.get("District") or "", pg.get("Block") or "", pg.get("Gram Panchayat") or "", pg.get("Village") or "", mdoc.get("name") or mdoc.get("member_name") or master.get("Member Name") or master.get("member_name") or "", mdoc.get("shg_name") or master.get("SHG Name") or master.get("SHG_Name") or master.get("shg_name") or "", mdoc.get("category") or master.get("Category") or master.get("Social Category") or "", contact, mdoc.get("agri_crop") or "", mdoc.get("agri_ffs_module") or "", mdoc.get("ardd_activity") or "", mdoc.get("ardd_unit") or "", mdoc.get("fishery_activity") or ""])
    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=members.csv"})


@reports_bp.route("/export/cashbook.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_cashbook_csv():
    import csv, io
    db = current_app.mongo_db

    base_match = _pg_match_from_session(session)
    filters = _reports_export_filters(db)
    group_by = (request.args.get("group_by") or "").strip().lower()

    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)

    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}

    # Keep existing report period behavior intact.
    _apply_period(match, "updated_at", dict(request.args))

    mem = io.StringIO()
    w = csv.writer(mem)

    def num(value):
        try:
            if value in (None, "", "null", "None", "-", "NaN"):
                return 0.0
            if isinstance(value, (int, float)):
                return float(value)
            return float(str(value).replace("₹", "").replace(",", "").strip() or 0)
        except Exception:
            return 0.0

    def is_contra(row):
        return isinstance(row, dict) and str(row.get("entryType") or "").strip().lower() == "contra"

    def row_parts(row):
        if not isinstance(row, dict):
            return 0.0, 0.0

        cash = num(
            row.get("amount")
            or row.get("cashAmount")
            or row.get("cash_amount")
            or 0
        )

        bank = num(
            row.get("bankAmount")
            or row.get("bank_amount")
            or row.get("bank")
            or 0
        )

        return round(cash, 2), round(bank, 2)

    def sum_parts(rows, *, include_contra=True):
        cash_total = 0.0
        bank_total = 0.0

        if isinstance(rows, list):
            for row in rows:
                if not include_contra and is_contra(row):
                    continue

                cash, bank = row_parts(row)
                cash_total += cash
                bank_total += bank

        return {
            "cash": round(cash_total, 2),
            "bank": round(bank_total, 2),
            "total": round(cash_total + bank_total, 2),
        }

    def contra_totals(receipts, payments):
        cash_to_bank = 0.0
        bank_to_cash = 0.0

        for row in list(receipts or []) + list(payments or []):
            if not is_contra(row):
                continue

            cash, bank = row_parts(row)
            row_total = cash + bank
            contra_type = str(row.get("contraType") or "").strip().lower()

            if contra_type == "cash_to_bank":
                cash_to_bank += row_total
            elif contra_type == "bank_to_cash":
                bank_to_cash += row_total

        cash_to_bank = round(cash_to_bank / 2, 2)
        bank_to_cash = round(bank_to_cash / 2, 2)

        return {
            "cash_to_bank": cash_to_bank,
            "bank_to_cash": bank_to_cash,
            "total": round(cash_to_bank + bank_to_cash, 2),
        }

    def opening_parts(doc):
        opening = doc.get("opening") if isinstance(doc.get("opening"), dict) else {}

        cash = num(
            opening.get("cash")
            if "cash" in opening else
            doc.get("opening_cash")
        )

        bank = num(
            opening.get("bank")
            if "bank" in opening else
            doc.get("opening_bank")
        )

        return {
            "cash": round(cash, 2),
            "bank": round(bank, 2),
            "total": round(cash + bank, 2),
        }

    def cashbook_summary(doc):
        opening = opening_parts(doc)
        receipts = doc.get("receipts") or []
        payments = doc.get("payments") or []

        # Balance includes contra.
        receipt_for_balance = sum_parts(receipts, include_contra=True)
        payment_for_balance = sum_parts(payments, include_contra=True)

        # Real report receipt/payment excludes contra.
        receipt_real = sum_parts(receipts, include_contra=False)
        payment_real = sum_parts(payments, include_contra=False)

        contra = contra_totals(receipts, payments)

        closing_cash = opening["cash"] + receipt_for_balance["cash"] - payment_for_balance["cash"]
        closing_bank = opening["bank"] + receipt_for_balance["bank"] - payment_for_balance["bank"]

        receipt_rows = [r for r in receipts if not is_contra(r)] if isinstance(receipts, list) else []
        payment_rows = [r for r in payments if not is_contra(r)] if isinstance(payments, list) else []
        contra_rows = [r for r in list(receipts or []) + list(payments or []) if is_contra(r)]

        return {
            "opening_cash": round(opening["cash"], 2),
            "opening_bank": round(opening["bank"], 2),
            "opening_total": round(opening["total"], 2),

            "receipt_cash": round(receipt_real["cash"], 2),
            "receipt_bank": round(receipt_real["bank"], 2),
            "receipt_total": round(receipt_real["total"], 2),

            "payment_cash": round(payment_real["cash"], 2),
            "payment_bank": round(payment_real["bank"], 2),
            "payment_total": round(payment_real["total"], 2),

            "contra_cash_to_bank": round(contra["cash_to_bank"], 2),
            "contra_bank_to_cash": round(contra["bank_to_cash"], 2),
            "contra_total": round(contra["total"], 2),

            "closing_cash": round(closing_cash, 2),
            "closing_bank": round(closing_bank, 2),
            "closing_total": round(closing_cash + closing_bank, 2),

            "receipt_count": len(receipt_rows),
            "payment_count": len(payment_rows),
            "contra_count": int(len(contra_rows) / 2),
        }

    if group_by:
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

        cursor = db.pg_cashbooks.find(
            match,
            {
                "pg_id": 1,
                "year": 1,
                "month": 1,
                "opening": 1,
                "opening_cash": 1,
                "opening_bank": 1,
                "receipts": 1,
                "payments": 1,
                "totals": 1,
            },
        )

        for doc in cursor:
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            key = gkey(pg)

            if key not in agg:
                agg[key] = {
                    "entries": 0,
                    "receipt_count": 0,
                    "payment_count": 0,
                    "contra_count": 0,
                    "opening_total": 0.0,
                    "receipt_total": 0.0,
                    "payment_total": 0.0,
                    "contra_cash_to_bank": 0.0,
                    "contra_bank_to_cash": 0.0,
                    "contra_total": 0.0,
                    "closing_total": 0.0,
                }

            s = cashbook_summary(doc)

            agg[key]["entries"] += 1
            agg[key]["receipt_count"] += s["receipt_count"]
            agg[key]["payment_count"] += s["payment_count"]
            agg[key]["contra_count"] += s["contra_count"]

            agg[key]["opening_total"] += s["opening_total"]
            agg[key]["receipt_total"] += s["receipt_total"]
            agg[key]["payment_total"] += s["payment_total"]

            agg[key]["contra_cash_to_bank"] += s["contra_cash_to_bank"]
            agg[key]["contra_bank_to_cash"] += s["contra_bank_to_cash"]
            agg[key]["contra_total"] += s["contra_total"]

            agg[key]["closing_total"] += s["closing_total"]

        w.writerow([
            "Group",
            "Cashbook Entries",
            "Receipt Rows",
            "Payment Rows",
            "Contra Entries",
            "Opening Total",
            "Receipt Total",
            "Payment Total",
            "Cash To Bank Transfer",
            "Bank To Cash Transfer",
            "Contra Transfer Total",
            "Closing Total",
        ])

        for k in sorted(agg.keys()):
            row = agg[k]
            w.writerow([
                k,
                row["entries"],
                row["receipt_count"],
                row["payment_count"],
                row["contra_count"],
                round(row["opening_total"], 2),
                round(row["receipt_total"], 2),
                round(row["payment_total"], 2),
                round(row["contra_cash_to_bank"], 2),
                round(row["contra_bank_to_cash"], 2),
                round(row["contra_total"], 2),
                round(row["closing_total"], 2),
            ])

    else:
        w.writerow([
            "PG Name",
            "State",
            "District",
            "Block",
            "GP",
            "Village",
            "Year",
            "Month",

            "Opening Cash",
            "Opening Bank",
            "Opening Total",

            "Receipt Cash",
            "Receipt Bank",
            "Receipt Total",

            "Payment Cash",
            "Payment Bank",
            "Payment Total",

            "Cash To Bank Transfer",
            "Bank To Cash Transfer",
            "Contra Transfer Total",

            "Closing Cash",
            "Closing Bank",
            "Closing Total",

            "Receipt Rows",
            "Payment Rows",
            "Contra Entries",
        ])

        for doc in db.pg_cashbooks.find(match).sort([("year", -1), ("month", -1)]):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            s = cashbook_summary(doc)

            w.writerow([
                pg.get("name") or "",
                pg.get("State") or "",
                pg.get("District") or "",
                pg.get("Block") or "",
                pg.get("Gram Panchayat") or "",
                pg.get("Village") or "",
                doc.get("year") or "",
                doc.get("month") or "",

                s["opening_cash"],
                s["opening_bank"],
                s["opening_total"],

                s["receipt_cash"],
                s["receipt_bank"],
                s["receipt_total"],

                s["payment_cash"],
                s["payment_bank"],
                s["payment_total"],

                s["contra_cash_to_bank"],
                s["contra_bank_to_cash"],
                s["contra_total"],

                s["closing_cash"],
                s["closing_bank"],
                s["closing_total"],

                s["receipt_count"],
                s["payment_count"],
                s["contra_count"],
            ])

    mem.seek(0)

    return current_app.response_class(
        mem.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cashbook.csv"},
    )


@reports_bp.route("/export/loans.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_loans_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = _reports_export_filters(db)
    group_by = (request.args.get("group_by") or "").strip().lower()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)
    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    _apply_period(match, "created_at", dict(request.args))
    member_docs = list(db.pg_members.find({"pg_id": {"$in": pg_ids}}, {"name": 1, "member_name": 1})) if pg_ids else []
    member_map = {str(m.get("_id")): (m.get("name") or m.get("member_name") or "") for m in member_docs}
    loans = []
    def principal(doc):
        for field in ("principal", "principal_amount", "sanction_amount", "sanctioned_amount", "estimated_amount", "disbursed_amount", "loan_amount", "amount"):
            value = doc.get(field)
            if value not in (None, ""):
                return _safe_float(value)
        return 0.0
    def outstanding(doc):
        return _safe_float(doc.get("outstanding_amount") or doc.get("outstanding") or doc.get("balance_amount"))
    for doc in db.pg_loan_accounts.find(match):
        loans.append(("PG", doc))
    for doc in db.pg_member_loan_accounts.find(match):
        loans.append(("Member", doc))
    mem = io.StringIO(newline="")
    w = csv.writer(mem)
    if group_by:
        def gkey(pg):
            return {"district": pg.get("District"), "block": pg.get("Block"), "gp": pg.get("Gram Panchayat"), "village": pg.get("Village"), "pg": pg.get("name")}.get(group_by, "") or ""
        agg = {}
        for _kind, doc in loans:
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            item = agg.setdefault(gkey(pg), {"loans": 0, "active": 0, "principal": 0.0, "outstanding": 0.0})
            item["loans"] += 1
            item["principal"] += principal(doc)
            item["outstanding"] += outstanding(doc)
            if str(doc.get("status") or "").lower() in ("active", "open"):
                item["active"] += 1
        w.writerow(["Group", "Loans", "Active Loans", "Principal Total", "Outstanding Total"])
        for key in sorted(agg):
            item = agg[key]
            w.writerow([key, item["loans"], item["active"], round(item["principal"], 2), round(item["outstanding"], 2)])
    else:
        w.writerow(["PG Name", "State", "District", "Block", "GP", "Village", "Loan Type", "Loan No", "Member Name", "Member ID", "Lender", "Purpose", "Principal", "Outstanding", "Status", "Created At"])
        def loan_created_sort(pair):
            created_at = pair[1].get("created_at")
            try:
                return created_at.timestamp() if created_at else 0
            except Exception:
                return 0
        for kind, doc in sorted(loans, key=loan_created_sort, reverse=True):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            member_id = str(doc.get("member_id") or "")
            created = doc.get("created_at")
            w.writerow([pg.get("name") or "", pg.get("State") or "", pg.get("District") or "", pg.get("Block") or "", pg.get("Gram Panchayat") or "", pg.get("Village") or "", kind, doc.get("loan_no") or "", (doc.get("member_name") or member_map.get(member_id) or "") if kind == "Member" else "", member_id if kind == "Member" else "", doc.get("lender") or doc.get("source") or "", doc.get("purpose") or "", round(principal(doc), 2), round(outstanding(doc), 2), doc.get("status") or "", created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else (created or "")])
    mem.seek(0)
    return current_app.response_class(mem.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=loans.csv"})


@reports_bp.route("/export/turnover.csv", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY","CLF_MANAGER","CLF_ADMIN","BLOCK_ADMIN","DISTRICT_ADMIN","ADMIN","SUPER_ADMIN")
def export_turnover_csv():
    import csv, io
    db = current_app.mongo_db
    base_match = _pg_match_from_session(session)
    filters = _reports_export_filters(db)
    group_by = (request.args.get("group_by") or "").strip().lower()
    pg_ids, pg_by_id = _pgs_in_scope(db, base_match, filters)
    match = {"pg_id": {"$in": pg_ids}} if pg_ids else {"pg_id": {"$in": []}}
    _apply_period(match, "updated_at", dict(request.args))
    market_rows = list(db.pg_market_transactions.find(match).sort([("year", -1), ("month", -1)]))
    seen_periods = {(str(row.get("pg_id")), row.get("year"), row.get("month")) for row in market_rows}
    legacy_rows = []
    for row in db.pg_business_monthly.find(match).sort([("year", -1), ("month", -1)]):
        if (str(row.get("pg_id")), row.get("year"), row.get("month")) not in seen_periods:
            legacy_rows.append(row)
    def tv(doc):
        for field in ("total_turnover", "turnover", "market_total"):
            value = doc.get(field)
            if value not in (None, ""):
                return _safe_float(value)
        return 0.0
    all_rows = [(doc, "Market") for doc in market_rows] + [(doc, "Legacy") for doc in legacy_rows]
    mem = io.StringIO(newline="")
    w = csv.writer(mem)
    if group_by:
        def gkey(pg):
            return {"district": pg.get("District"), "block": pg.get("Block"), "gp": pg.get("Gram Panchayat"), "village": pg.get("Village"), "pg": pg.get("name")}.get(group_by, "") or ""
        agg = {}
        for doc, _source in all_rows:
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            item = agg.setdefault(gkey(pg), {"turnover": 0.0, "months": 0})
            item["turnover"] += tv(doc)
            item["months"] += 1
        w.writerow(["Group", "Months", "Turnover Total"])
        for key in sorted(agg):
            w.writerow([key, agg[key]["months"], round(agg[key]["turnover"], 2)])
    else:
        w.writerow(["PG Name", "State", "District", "Block", "GP", "Village", "Year", "Month", "Turnover", "Source"])
        for doc, source in sorted(all_rows, key=lambda pair: (pair[0].get("year") or 0, pair[0].get("month") or 0), reverse=True):
            pg = pg_by_id.get(doc.get("pg_id")) or {}
            w.writerow([pg.get("name") or "", pg.get("State") or "", pg.get("District") or "", pg.get("Block") or "", pg.get("Gram Panchayat") or "", pg.get("Village") or "", doc.get("year") or "", doc.get("month") or "", round(tv(doc), 2), source])
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
    """Build a Mongo query for PGs scoped to the user's jurisdiction.

    Supports:
    - Web session
    - Mobile JWT context via flask.g
    """
    role = _reports_ctx_value("role", "")
    state_id = _reports_ctx_value("state_id")
    district_id = _reports_ctx_value("district_id")
    block_id = _reports_ctx_value("block_id")
    clf_id = _reports_ctx_value("clf_id")
    pg_id = _reports_ctx_value("pg_id") or _reports_ctx_value("active_pg_id")

    q = {}

    try:
        if role in ("PG_DATA_ENTRY", "CADRE_CC") and pg_id and ObjectId.is_valid(str(pg_id)):
            q["_id"] = ObjectId(str(pg_id))
        elif clf_id and ObjectId.is_valid(str(clf_id)):
            q["clf_id"] = ObjectId(str(clf_id))
        elif block_id and ObjectId.is_valid(str(block_id)):
            q["block_id"] = ObjectId(str(block_id))
        elif district_id and ObjectId.is_valid(str(district_id)):
            q["district_id"] = ObjectId(str(district_id))
        elif state_id and ObjectId.is_valid(str(state_id)) and role in (
            "ADMIN",
            "STATE_ADMIN",
            "DISTRICT_ADMIN",
            "BLOCK_ADMIN",
            "CLF_ADMIN",
            "CLF_MANAGER",
        ):
            q["state_id"] = ObjectId(str(state_id))
    except Exception:
        return {}

    return q



# ============================================================
# CLF / Block surveillance helpers
# ============================================================

VALIDATION_PENDING_STATUSES = ("submitted", "resubmitted")


def _to_object_id(value):
    try:
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _id_values(value):
    """Return ObjectId + string variants for old/new Mongo documents."""
    values = []
    oid = _to_object_id(value)
    if oid:
        values.append(oid)
        values.append(str(oid))
    elif value not in (None, "", [], {}):
        values.append(str(value))
    return values


def _safe_float(value):
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _pg_child_match(pg_ids):
    """Build child collection match for documents storing pg_id as ObjectId or string."""
    values = []
    for pg_id in pg_ids or []:
        values.extend(_id_values(pg_id))
    return {"pg_id": {"$in": values}} if values else {"pg_id": {"$in": []}}


def _validation_status_from_pg(pg, field_name):
    doc = pg.get(field_name) or {}
    return str(doc.get("status") or "draft").lower()


def _validation_counts_for_pgs(pgs):
    """Return validation status counts for PG Registration and Membership Registration."""
    counts = {
        "pg_registration": {
            "draft": 0,
            "submitted": 0,
            "resubmitted": 0,
            "approved": 0,
            "rejected": 0,
            "pending": 0,
        },
        "member_registration": {
            "draft": 0,
            "submitted": 0,
            "resubmitted": 0,
            "approved": 0,
            "rejected": 0,
            "pending": 0,
        },
        "total_pending": 0,
    }

    valid = {"draft", "submitted", "resubmitted", "approved", "rejected"}

    for pg in pgs or []:
        reg_status = _validation_status_from_pg(pg, "registration_validation")
        mem_status = _validation_status_from_pg(pg, "member_registration_validation")

        if reg_status not in valid:
            reg_status = "draft"
        if mem_status not in valid:
            mem_status = "draft"

        counts["pg_registration"][reg_status] += 1
        counts["member_registration"][mem_status] += 1

        if reg_status in VALIDATION_PENDING_STATUSES:
            counts["pg_registration"]["pending"] += 1
            counts["total_pending"] += 1

        if mem_status in VALIDATION_PENDING_STATUSES:
            counts["member_registration"]["pending"] += 1
            counts["total_pending"] += 1

    return counts


def _cashflow_for_pg_ids(db, pg_ids):
    """Best-effort cashflow used by hierarchy CLF surveillance cards."""
    match = _pg_child_match(pg_ids)

    grant_received = 0.0
    grant_utilized = 0.0
    loan_principal = 0.0
    member_loan_principal = 0.0

    for grant in db.pg_grants.find(match):
        grant_received += _safe_float(
            grant.get("amount_received")
            or grant.get("amount")
            or grant.get("received_amount")
            or grant.get("release_amount")
            or grant.get("grant_amount")
        )

    for util in db.pg_grant_utilizations.find(match):
        grant_utilized += _safe_float(
            util.get("amount")
            or util.get("utilized_amount")
            or util.get("utilization_amount")
        )

    for loan in db.pg_loan_accounts.find(match):
        loan_principal += _safe_float(
            loan.get("principal_amount")
            or loan.get("principal")
            or loan.get("sanctioned_amount")
            or loan.get("amount")
        )

    for loan in db.pg_member_loan_accounts.find(match):
        member_loan_principal += _safe_float(
            loan.get("principal_amount")
            or loan.get("principal")
            or loan.get("loan_amount")
            or loan.get("amount")
        )

    cash_in = grant_received + loan_principal + member_loan_principal
    cash_out = grant_utilized

    return {
        "cash_in": round(cash_in, 2),
        "cash_out": round(cash_out, 2),
        "balance": round(cash_in - cash_out, 2),
        "grant_received": round(grant_received, 2),
        "grant_utilized": round(grant_utilized, 2),
        "loan_principal": round(loan_principal, 2),
        "member_loan_principal": round(member_loan_principal, 2),
    }


def _build_clf_surveillance_summary(db, pgs, role=None):
    """
    Build CLF-wise surveillance rows for Block/District dashboards.

    Each row contains CLF name, PG count, member count, cashflow and pending validation.
    Unassigned PGs are grouped separately so Block Admin can immediately assign them.
    """
    role = role or _reports_ctx_value("role", "")
    grouped = {}

    for pg in pgs or []:
        clf_id = pg.get("clf_id")
        key = str(clf_id) if clf_id else "__unassigned__"

        if key not in grouped:
            grouped[key] = {
                "clf_id": clf_id,
                "clf_id_str": str(clf_id) if clf_id else "",
                "clf_name": "Unassigned CLF" if key == "__unassigned__" else "CLF",
                "pgs": [],
            }
        grouped[key]["pgs"].append(pg)

    clf_ids = [v["clf_id"] for v in grouped.values() if v.get("clf_id")]
    clf_docs = list(db.clfs.find({"_id": {"$in": clf_ids}}, {"name": 1, "clf_name": 1})) if clf_ids else []
    clf_map = {str(c.get("_id")): c for c in clf_docs}

    rows = []
    for key, data in grouped.items():
        row_pgs = data.get("pgs") or []
        row_pg_ids = [pg.get("_id") for pg in row_pgs if pg.get("_id")]
        clf_doc = clf_map.get(str(data.get("clf_id") or ""), {})
        validation_counts = _validation_counts_for_pgs(row_pgs)
        cashflow = _cashflow_for_pg_ids(db, row_pg_ids)

        data["clf_name"] = (
            clf_doc.get("name")
            or clf_doc.get("clf_name")
            or data.get("clf_name")
            or "CLF"
        )
        data["pg_count"] = len(row_pgs)
        data["member_count"] = db.pg_members.count_documents(_pg_child_match(row_pg_ids)) if row_pg_ids else 0
        data["validation_counts"] = validation_counts
        data["pending_validation_count"] = validation_counts.get("total_pending", 0)
        data["cashflow"] = cashflow
        data["assigned_pg_url"] = ""

        if key == "__unassigned__" and role == "BLOCK_ADMIN":
            data["assigned_pg_url"] = url_for("master_data.clf_pg_assignment")
        elif data.get("clf_id"):
            try:
                data["assigned_pg_url"] = url_for("master_data.clf_pg_assignment") if role == "BLOCK_ADMIN" else ""
            except Exception:
                data["assigned_pg_url"] = ""

        rows.append(data)

    rows.sort(key=lambda x: (x.get("clf_name") == "Unassigned CLF", x.get("clf_name") or ""))
    return rows


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
        selected_cadre_type = (request.args.get("cadre_type") or "").strip()
        if selected_cadre_type not in CADRE_TYPE_OPTIONS:
            selected_cadre_type = ""

        cadre_match = {"role": "CADRE_CC"}

        if pg_match.get("state_id"):
            cadre_match["state_id"] = pg_match["state_id"]
        if pg_match.get("district_id"):
            cadre_match["district_id"] = pg_match["district_id"]
        if pg_match.get("block_id"):
            cadre_match["block_id"] = pg_match["block_id"]

        if selected_cadre_type:
            cadre_match["cadre_type"] = selected_cadre_type

        if search_query:
            safe_q = re.escape(search_query)
            cadre_match["$or"] = [
                {"name": {"$regex": safe_q, "$options": "i"}},
                {"full_name": {"$regex": safe_q, "$options": "i"}},
                {"username": {"$regex": safe_q, "$options": "i"}},
                {"phone": {"$regex": safe_q, "$options": "i"}},
                {"contact": {"$regex": safe_q, "$options": "i"}},
                {"contact_number": {"$regex": safe_q, "$options": "i"}},
                {"email": {"$regex": safe_q, "$options": "i"}},
                {"cadre_type": {"$regex": safe_q, "$options": "i"}},
            ]

        cadres = list(
            db.users.find(
                cadre_match,
                {
                    "name": 1,
                    "full_name": 1,
                    "username": 1,
                    "phone": 1,
                    "contact": 1,
                    "contact_number": 1,
                    "email": 1,
                    "cadre_type": 1,
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
                "name": c.get("full_name") or c.get("name") or c.get("username") or "-",
                "username": c.get("username") or "-",
                "cadre_type": c.get("cadre_type") or "-",
                "contact": c.get("phone") or c.get("contact") or c.get("contact_number") or c.get("email") or "-",
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
            "cadre_type_options": CADRE_TYPE_OPTIONS,
            "selected_cadre_type": selected_cadre_type,
            "full_url": url_for(
                "reports.state_dashboard_detail_full",
                detail=detail_key,
                q=search_query,
                cadre_type=selected_cadre_type,
            ),
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


def _state_dashboard_clf_summary(db, pg_match):
    """
    Build CLF KPI/details for State Dashboard.

    Scope follows the same State/District/Block scope already used by State Dashboard.
    Data comes from db.clfs created by Block Admin.
    PG count and village coverage are calculated after PG mapping.
    """
    clf_match = {
        "status": {"$ne": "deleted"},
    }

    if pg_match.get("state_id"):
        clf_match["state_id"] = pg_match["state_id"]

    if pg_match.get("district_id"):
        clf_match["district_id"] = pg_match["district_id"]

    if pg_match.get("block_id"):
        clf_match["block_id"] = pg_match["block_id"]

    clfs = list(db.clfs.find(clf_match).sort("name", 1))

    def _master_name(collection_name, raw_id):
        if not raw_id:
            return "-"

        try:
            oid = raw_id if isinstance(raw_id, ObjectId) else ObjectId(str(raw_id))
            doc = db[collection_name].find_one(
                {"_id": oid},
                {"name": 1, "code": 1}
            ) or {}
            return doc.get("name") or doc.get("code") or "-"
        except Exception:
            try:
                doc = db[collection_name].find_one(
                    {"_id": str(raw_id)},
                    {"name": 1, "code": 1}
                ) or {}
                return doc.get("name") or doc.get("code") or "-"
            except Exception:
                return "-"

    def _to_oid_list(values):
        oid_list = []

        for value in values or []:
            try:
                oid_list.append(value if isinstance(value, ObjectId) else ObjectId(str(value)))
            except Exception:
                pass

        return oid_list

    rows = []

    total_clfs = len(clfs)
    registered_clfs = 0
    model_clfs = 0
    non_model_clfs = 0
    total_ec_members = 0
    total_pg_mapped = 0

    all_villages = set()

    for clf in clfs:
        clf_id = clf.get("_id")

        if clf.get("is_registered") == "yes":
            registered_clfs += 1

        if clf.get("clf_type") == "Model CLF":
            model_clfs += 1

        if clf.get("clf_type") == "Non-Model CLF":
            non_model_clfs += 1

        try:
            total_ec_members += int(clf.get("ec_members_count") or 0)
        except Exception:
            pass

        assigned_pg_ids = _to_oid_list(clf.get("assigned_pg_ids") or [])

        pg_or_conditions = [
            {"clf_id": clf_id},
            {"clf_id": str(clf_id)},
        ]

        if assigned_pg_ids:
            pg_or_conditions.append({"_id": {"$in": assigned_pg_ids}})

        mapped_pg_match = dict(pg_match)
        mapped_pg_match["$or"] = pg_or_conditions
        mapped_pg_match["status"] = {"$ne": "deleted"}

        mapped_pgs = list(db.pgs.find(
            mapped_pg_match,
            {
                "_id": 1,
                "name": 1,
                "pg_name": 1,
                "Village": 1,
                "village": 1,
                "village_name": 1,
                "Village Name": 1,
            }
        ))

        village_set = set()

        for pg in mapped_pgs:
            village_name = (
                pg.get("Village")
                or pg.get("village")
                or pg.get("village_name")
                or pg.get("Village Name")
                or ""
            )

            village_name = str(village_name).strip()

            if village_name:
                village_set.add(village_name.lower())
                all_villages.add(village_name.lower())

        pg_count = len(mapped_pgs)
        village_count = len(village_set)

        total_pg_mapped += pg_count

        rows.append({
            "id": str(clf_id),
            "name": clf.get("name") or clf.get("clf_name") or "-",

            "state": _master_name("states", clf.get("state_id")),
            "district": _master_name("districts", clf.get("district_id")),
            "block": _master_name("blocks", clf.get("block_id")),

            "vc_name_location": clf.get("vc_name_location") or "-",

            "president_name": clf.get("president_name") or "-",
            "president_contact": clf.get("president_contact") or "-",

            "secretary_name": clf.get("secretary_name") or "-",
            "secretary_contact": clf.get("secretary_contact") or "-",

            "is_registered": clf.get("is_registered") or "",
            "registration_date": clf.get("registration_date_raw") or "",

            "clf_type": clf.get("clf_type") or "-",
            "ec_members_count": clf.get("ec_members_count") if clf.get("ec_members_count") is not None else 0,

            "village_covered_count": village_count,
            "pg_count": pg_count,
        })

    return {
        "total_clfs": total_clfs,
        "registered_clfs": registered_clfs,
        "model_clfs": model_clfs,
        "non_model_clfs": non_model_clfs,
        "total_ec_members": total_ec_members,
        "total_pg_mapped": total_pg_mapped,
        "total_villages_covered": len(all_villages),
        "rows": rows,
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
    
    # CLF KPI/details for State Dashboard
    clf_summary = _state_dashboard_clf_summary(db, pg_match)
    
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
        clf_summary=clf_summary,
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
        cadre_pg_oids = [ObjectId(str(x)) for x in assigned_pg_ids if ObjectId.is_valid(str(x))]
        query["_id"] = {"$in": cadre_pg_oids or [ObjectId("000000000000000000000000")]}
    elif clf_id and ObjectId.is_valid(str(clf_id)):
        query["clf_id"] = ObjectId(str(clf_id))
    elif block_id and ObjectId.is_valid(str(block_id)):
        query["block_id"] = ObjectId(str(block_id))
    elif district_id and ObjectId.is_valid(str(district_id)):
        query["district_id"] = ObjectId(str(district_id))
    elif state_id and ObjectId.is_valid(str(state_id)):
        query["state_id"] = ObjectId(str(state_id))

    pgs = list(db.pgs.find(query).sort([("created_at", -1)]).limit(500))

    if role == "CADRE_CC":
        cadre_count = 1
    else:
        cadre_match = {"role": "CADRE_CC"}
        if block_id and ObjectId.is_valid(str(block_id)):
            cadre_match["block_id"] = ObjectId(str(block_id))
        elif district_id and ObjectId.is_valid(str(district_id)):
            cadre_match["district_id"] = ObjectId(str(district_id))
        elif state_id and ObjectId.is_valid(str(state_id)):
            cadre_match["state_id"] = ObjectId(str(state_id))
        cadre_count = db.users.count_documents(cadre_match)

    pg_ids = [pg["_id"] for pg in pgs]
    pg_count = len(pg_ids)
    member_count = db.pg_members.count_documents(_pg_child_match(pg_ids)) if pg_ids else 0

    # CLF surveillance: Block sees all CLFs under block; District sees all CLFs under district.
    clf_match = {}
    if role == "BLOCK_ADMIN" and block_id and ObjectId.is_valid(str(block_id)):
        clf_match["block_id"] = ObjectId(str(block_id))
    elif role == "DISTRICT_ADMIN" and district_id and ObjectId.is_valid(str(district_id)):
        clf_match["district_id"] = ObjectId(str(district_id))
    elif role == "ADMIN" and state_id and ObjectId.is_valid(str(state_id)):
        clf_match["state_id"] = ObjectId(str(state_id))
    elif role in ("CLF_ADMIN", "CLF_MANAGER") and clf_id and ObjectId.is_valid(str(clf_id)):
        clf_match["_id"] = ObjectId(str(clf_id))

    clf_count = db.clfs.count_documents(clf_match) if clf_match or role in ("SUPER_ADMIN", "ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN", "CLF_ADMIN", "CLF_MANAGER") else 0
    clf_admin_count = 0
    clf_admin_match = {"role": "CLF_ADMIN"}
    if block_id and ObjectId.is_valid(str(block_id)):
        clf_admin_match["block_id"] = ObjectId(str(block_id))
    elif district_id and ObjectId.is_valid(str(district_id)):
        clf_admin_match["district_id"] = ObjectId(str(district_id))
    elif state_id and ObjectId.is_valid(str(state_id)):
        clf_admin_match["state_id"] = ObjectId(str(state_id))
    if role in ("SUPER_ADMIN", "ADMIN", "DISTRICT_ADMIN", "BLOCK_ADMIN", "CLF_ADMIN", "CLF_MANAGER"):
        clf_admin_count = db.users.count_documents(clf_admin_match)

    validation_counts = _validation_counts_for_pgs(pgs)
    validation_pending_count = validation_counts.get("total_pending", 0)
    clf_surveillance = _build_clf_surveillance_summary(db, pgs, role=role)

    # Turnover / Profit-Loss / Grants / Lakhpati (scope)
    turnover_total = 0.0
    profit_total = 0.0
    loss_total = 0.0
    grants_total = 0.0
    pgs_with_grants = 0
    lakhpati_total = 0

    if pg_ids:
        child_match = _pg_child_match(pg_ids)

        td = list(db.pg_market_transactions.aggregate([
            {"$match": child_match},
            {"$group": {"_id": None, "turnover": {"$sum": "$total_turnover"}}}
        ]))
        turnover_total = float(td[0].get("turnover") if td else 0)

        pd = list(db.pg_income_expenditure.aggregate([
            {"$match": child_match},
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
            {"$match": child_match},
            {"$group": {"_id": None, "grants_total": {"$sum": "$amount_received"}, "pgs": {"$addToSet": "$pg_id"}}}
        ]))
        if gd:
            grants_total = float(gd[0].get("grants_total") or 0)
            pgs_with_grants = len(gd[0].get("pgs") or [])

        try:
            lakhpati_total = db.pg_members.count_documents({**child_match, "lakh_pati_didi": True})
        except Exception:
            lakhpati_total = 0

    # SHG master counts scoped by user's jurisdiction
    shg_q = _shg_filter_from_session(db, session)
    shg_total = db.shg_master.count_documents(shg_q)
    shg_active = db.shg_master.count_documents({**shg_q, "Status": "Active"})

    # Top blocks or GP for quick insight
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
        clf_count=clf_count,
        clf_admin_count=clf_admin_count,
        validation_counts=validation_counts,
        validation_pending_count=validation_pending_count,
        clf_surveillance=clf_surveillance,
        validation_queue_url=url_for("pg.validation_queue") if role == "BLOCK_ADMIN" else "",
        clf_assignment_url=url_for("master_data.clf_pg_assignment") if role == "BLOCK_ADMIN" else "",
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
    from flask import request, jsonify, redirect, url_for, flash
    from bson import ObjectId
    from datetime import datetime

    db = current_app.mongo_db

    if not pg_id or not ObjectId.is_valid(str(pg_id)):
        flash("Invalid PG selected.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg_oid = ObjectId(str(pg_id))

    selected_year_raw = request.args.get("year")
    selected_month_raw = request.args.get("month")
    view_mode = (request.args.get("view") or "").strip().lower()

    try:
        selected_year = int(selected_year_raw) if selected_year_raw not in (None, "") else None
    except Exception:
        selected_year = None

    try:
        selected_month = int(selected_month_raw) if selected_month_raw not in (None, "") else None
    except Exception:
        selected_month = None

    has_period = (
        selected_year is not None
        and selected_month is not None
        and 1 <= int(selected_month) <= 12
    )

    # Sidebar opens this page without year/month.
    # In that case, show all records instead of returning blank.
    if view_mode == "all" or not has_period:
        effective_view_mode = "all"
    else:
        effective_view_mode = "period"

    query = {
        "level": "pg",
        "$or": [
            {"ref_id": str(pg_oid)},
            {"ref_id": pg_oid},
            {"pg_id": str(pg_oid)},
            {"pg_id": pg_oid},
        ],
    }

    if effective_view_mode == "period":
        query["year"] = int(selected_year)
        query["month"] = int(selected_month)

    snapshots_raw = list(
        db.mpr_snapshots.find(query).sort([
            ("year", -1),
            ("month", -1),
            ("updated_at", -1),
            ("created_at", -1),
        ])
    )

    def _safe_num(value):
        try:
            if value is None or value == "":
                return 0
            if isinstance(value, str):
                value = value.replace("₹", "").replace(",", "").replace("%", "").strip()
            return float(value)
        except Exception:
            return 0

    def _pick(row, *keys, default=0):
        metrics = row.get("metrics") or {}

        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value

        for key in keys:
            value = metrics.get(key)
            if value not in (None, ""):
                return value

        return default

    def _format_amount(value):
        return "₹{:,.2f}".format(_safe_num(value))

    def _format_percent(value):
        return "{:.2f}%".format(_safe_num(value))

    normalized = []

    for i, row in enumerate(snapshots_raw, start=1):
        monthly_turnover = _pick(
            row,
            "monthly_turnover",
            "turnover",
            "total_turnover",
            "business_turnover",
            default=0,
        )

        pct_members_input = _pick(
            row,
            "pct_members_input",
            "members_input_pct",
            "input_business_involvement",
            "input_involvement_pct",
            "input_pct",
            default=0,
        )

        pct_members_output = _pick(
            row,
            "pct_members_output",
            "members_output_pct",
            "output_business_involvement",
            "output_involvement_pct",
            "output_pct",
            default=0,
        )

        workflow_status = (
            row.get("status")
            or row.get("workflow_status")
            or row.get("submission_status")
            or "draft"
        )

        workflow_level = (
            row.get("current_level")
            or row.get("workflow_level")
            or "-"
        )

        normalized.append({
            "_id": str(row.get("_id") or ""),
            "sl_no": i,
            "pg_id": str(pg_oid),
            "year": row.get("year") or "",
            "month": row.get("month") or "",

            "monthly_turnover": _format_amount(monthly_turnover),
            "pct_members_input": _format_percent(pct_members_input),
            "pct_members_output": _format_percent(pct_members_output),

            "workflow_status": str(workflow_status or "draft").lower(),
            "workflow_status_label": str(workflow_status or "draft").replace("_", " ").title(),
            "current_level": str(workflow_level or "-"),

            # backward-compatible aliases for old app keys
            "turnover": _safe_num(monthly_turnover),
            "input": _safe_num(pct_members_input),
            "output": _safe_num(pct_members_output),

            # raw numeric values for mobile/app/json usage
            "monthly_turnover_value": _safe_num(monthly_turnover),
            "pct_members_input_value": _safe_num(pct_members_input),
            "pct_members_output_value": _safe_num(pct_members_output),
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
            "pg_id": str(pg_oid),
            "view": effective_view_mode,
            "year": selected_year,
            "month": selected_month,
            "count": len(normalized),
            "snapshots": normalized,
        })

    return render_template(
        "pg_mpr.html",
        snapshots=normalized,
        selected_year=selected_year,
        selected_month=selected_month,
        view_mode=effective_view_mode,
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
    pg_oid = ObjectId(str(pg_id)) if ObjectId.is_valid(str(pg_id)) else None

    ref_filters = [{"ref_id": str(pg_id)}, {"pg_id": str(pg_id)}]

    if pg_oid:
        ref_filters.extend([
            {"ref_id": pg_oid},
            {"pg_id": pg_oid},
        ])

    snap = db.mpr_snapshots.find_one({
        "level": "pg",
        "year": year,
        "month": month,
        "$or": ref_filters,
    }) or {}  
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
    clf = (filters.get("clf") or filters.get("CLF") or filters.get("clf_name") or "").strip()
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

    if clf:
        try:
            clf_doc = db.clfs.find_one({
                "$or": [
                    {"name": clf},
                    {"clf_name": clf},
                    {"CLF Name": clf},
                ]
            }, {"_id": 1})

            if clf_doc and clf_doc.get("_id"):
                match["clf_id"] = clf_doc["_id"]
        except Exception:
            pass

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

def _resolve_grants_pg_clf_name(db, pg):
    """Resolve the CLF assigned to a PG from its stored reference or CLF assignment list."""
    if not isinstance(pg, dict):
        return ""

    direct_name = (
        pg.get("clf_name") or pg.get("mapped_clf_name") or pg.get("assigned_clf_name")
        or pg.get("CLF Name") or pg.get("CLF")
    )
    clf_ref = (
        pg.get("clf_id") or pg.get("CLF_id") or pg.get("clfId")
        or pg.get("mapped_clf_id") or pg.get("assigned_clf_id")
    )
    clf_doc = None
    if clf_ref not in (None, ""):
        try:
            clf_oid = clf_ref if isinstance(clf_ref, ObjectId) else (ObjectId(str(clf_ref)) if ObjectId.is_valid(str(clf_ref)) else None)
        except Exception:
            clf_oid = None
        if clf_oid:
            clf_doc = db.clfs.find_one({"_id": clf_oid}, {"name": 1, "clf_name": 1, "CLF Name": 1})
            if not clf_doc:
                clf_doc = db.clfs.find_one({"_id": str(clf_ref)}, {"name": 1, "clf_name": 1, "CLF Name": 1})
        else:
            clf_doc = db.clfs.find_one({"$or": [
                {"name": str(clf_ref)}, {"clf_name": str(clf_ref)}, {"CLF Name": str(clf_ref)}
            ]}, {"name": 1, "clf_name": 1, "CLF Name": 1})

    # Some existing PGs only have their assignment recorded on the CLF document.
    if not clf_doc and pg.get("_id"):
        pg_id = pg.get("_id")
        pg_id_str = str(pg_id)
        assignment_terms = []
        for field in ("assigned_pg_ids", "pg_ids", "mapped_pg_ids", "pgs"):
            assignment_terms.extend(({field: pg_id}, {field: pg_id_str}))
        try:
            clf_doc = db.clfs.find_one({"$or": assignment_terms}, {"name": 1, "clf_name": 1, "CLF Name": 1})
        except Exception:
            clf_doc = None

    return (
        (clf_doc.get("name") or clf_doc.get("clf_name") or clf_doc.get("CLF Name") or "")
        if clf_doc else str(direct_name or "").strip()
    )


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

    pgs = list(db.pgs.find(match_pg, {
        "name": 1, "State": 1, "District": 1, "Block": 1,
        "Gram Panchayat": 1, "Village": 1, "clf_id": 1, "CLF_id": 1,
        "clfId": 1, "mapped_clf_id": 1, "assigned_clf_id": 1,
        "clf_name": 1, "mapped_clf_name": 1, "assigned_clf_name": 1,
        "CLF Name": 1, "CLF": 1,
    }))
    for pg in pgs:
        pg["_report_clf_name"] = _resolve_grants_pg_clf_name(db, pg)
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
            "CLF": pg.get("_report_clf_name") or "",
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

    pgs = list(db.pgs.find(match_pg, {
        "name": 1, "State": 1, "District": 1, "Block": 1,
        "Gram Panchayat": 1, "Village": 1, "clf_id": 1, "CLF_id": 1,
        "clfId": 1, "mapped_clf_id": 1, "assigned_clf_id": 1,
        "clf_name": 1, "mapped_clf_name": 1, "assigned_clf_name": 1,
        "CLF Name": 1, "CLF": 1,
    }))
    for pg in pgs:
        pg["_report_clf_name"] = _resolve_grants_pg_clf_name(db, pg)
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
        if group_by.strip().lower() == "clf":
            return pg_doc.get("_report_clf_name") or ""
        if group_by in ("State", "District", "Block", "Gram Panchayat", "Village"):
            return pg_doc.get(group_by) or ""
        return ""

    import csv
    from io import StringIO
    from flask import Response
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["PG Name","State","District","Block","Gram Panchayat","Village","CLF","Category","Source","Release Date","Amount Received","Utilized","Balance","UC Status","Created At"])
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
            pg.get("_report_clf_name") or "",
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
    """Write CSV values as readable text, including nested register rows."""
    import json
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat(sep=" ", timespec="seconds")
        if isinstance(value, dict):
            return "; ".join(f"{key}: {cell(item)}" for key, item in value.items())
        if isinstance(value, list):
            return " | ".join(cell(item) for item in value)
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, (str, int, float, bool)):
            return value
        try:
            return json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            return str(value)
    writer.writerow(fieldnames)
    for doc in docs:
        writer.writerow([cell(doc.get(field)) for field in fieldnames])

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
            m_fields = ["_id", "pg_id", "name", "spouse", "category", "shg_name", "contact", "photo_id_number", "bank_name", "branch", "account_number", "membership_fee_paid", "lakh_pati_didi", "agri_crop", "agri_ffs_module", "ardd_activity", "ardd_unit", "fishery_activity", "created_at", "updated_at"]
            for m in members:
                m["_id"] = str(m.get("_id"))
                m["pg_id"] = str(m.get("pg_id"))
                member_ref = m.get("shg_member_id") or m.get("member_id")
                master = {}
                if member_ref:
                    try:
                        member_oid = member_ref if isinstance(member_ref, ObjectId) else ObjectId(str(member_ref))
                        master = db.shg_members_master.find_one({"_id": member_oid}) or {}
                    except Exception:
                        master = db.shg_members_master.find_one({"_id": member_ref}) or {}
                m["contact"] = (m.get("contact") or m.get("phone") or m.get("contact_number") or master.get("Contact") or master.get("Contact Number") or master.get("Mobile Number") or master.get("Phone") or master.get("contact") or master.get("phone") or "")
                m["name"] = m.get("name") or m.get("member_name") or master.get("Member Name") or master.get("member_name") or ""
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

            # 5a) Loan accounts with member names and normalized principal amounts.
            loan_rows = []
            member_names = {str(m.get("_id")): (m.get("name") or m.get("member_name") or "") for m in members}
            for loan_type, collection in (("PG", db.pg_loan_accounts), ("Member", db.pg_member_loan_accounts)):
                for loan in collection.find({"pg_id": pg_id}).sort([("created_at", -1)]):
                    amount = next((loan.get(key) for key in ("principal", "principal_amount", "sanction_amount", "sanctioned_amount", "estimated_amount", "disbursed_amount", "loan_amount", "amount") if loan.get(key) not in (None, "")), 0)
                    member_id = loan.get("member_id")
                    loan_rows.append({"Loan Type": loan_type, "Loan No": loan.get("loan_no") or "", "Member Name": (loan.get("member_name") or member_names.get(str(member_id), "")) if loan_type == "Member" else "", "Member ID": str(member_id or "") if loan_type == "Member" else "", "Lender": loan.get("lender") or loan.get("source") or "", "Purpose": loan.get("purpose") or "", "Principal": amount, "Outstanding": loan.get("outstanding_amount") or loan.get("outstanding") or 0, "Status": loan.get("status") or "", "Created At": loan.get("created_at") or ""})
            buf = io.StringIO(newline=""); w = csv.writer(buf)
            loan_export_fields = ["Loan Type", "Loan No", "Member Name", "Member ID", "Lender", "Purpose", "Principal", "Outstanding", "Status", "Created At"]
            _write_csv_rows(w, loan_export_fields, loan_rows)
            z.writestr(f"{safe_prefix}/loans.csv", buf.getvalue())

            # 5b) Monthly turnover, sourced from pg_market_transactions.
            turnover_rows = []
            market_periods = set()
            for row in db.pg_market_transactions.find({"pg_id": pg_id}).sort([("year", -1), ("month", -1)]):
                market_periods.add((row.get("year"), row.get("month")))
                amount = row.get("total_turnover")
                if amount in (None, ""):
                    amount = row.get("turnover")
                if amount in (None, ""):
                    amount = row.get("market_total") or 0
                turnover_rows.append({"Year": row.get("year") or "", "Month": row.get("month") or "", "Turnover": amount, "Internal Turnover": "", "Market Turnover": row.get("market_total") or "", "Source": "Market transactions"})
            for row in db.pg_business_monthly.find({"pg_id": pg_id}).sort([("year", -1), ("month", -1)]):
                if (row.get("year"), row.get("month")) in market_periods:
                    continue
                amount = row.get("total_turnover")
                if amount in (None, ""):
                    amount = row.get("turnover") or 0
                turnover_rows.append({"Year": row.get("year") or "", "Month": row.get("month") or "", "Turnover": amount, "Internal Turnover": row.get("internal_total") or "", "Market Turnover": "", "Source": "Legacy business record"})
            buf = io.StringIO(newline=""); w = csv.writer(buf)
            turnover_fields = ["Year", "Month", "Turnover", "Internal Turnover", "Market Turnover", "Source"]
            _write_csv_rows(w, turnover_fields, turnover_rows)
            z.writestr(f"{safe_prefix}/turnover.csv", buf.getvalue())

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


