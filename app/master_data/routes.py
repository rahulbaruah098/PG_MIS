from services.audit_engine import AuditLogger
from flask import render_template, request, redirect, url_for, flash, current_app, session, jsonify
from bson import ObjectId
from datetime import datetime
import re
import os

from . import master_data_bp
from ..rbac import login_required, roles_required
from ..services.workflow import next_pg_auto_id
from ..utils import hash_password


# ============================================================
# Common helpers
# ============================================================

def _to_object_id(value):
    try:
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _safe_str(value):
    if value in (None, "", [], {}):
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _current_user_oid():
    return _to_object_id(session.get("user_id")) or session.get("user_id")


def _get_block_scope(db):
    """
    Resolve logged-in Block Admin scope.

    Returns:
        {
            state_id,
            district_id,
            block_id,
            block,
            district,
            state
        }
    """
    block_id = session.get("block_id")
    block_oid = _to_object_id(block_id)

    if not block_oid:
        return None

    block = db.blocks.find_one({"_id": block_oid})
    if not block:
        return None

    district_id = block.get("district_id") or _to_object_id(session.get("district_id"))
    district = None
    state = None
    state_id = None

    if district_id:
        district = db.districts.find_one({"_id": district_id})
        if district:
            state_id = district.get("state_id") or _to_object_id(session.get("state_id"))

    if state_id:
        state = db.states.find_one({"_id": state_id})

    return {
        "state_id": state_id,
        "district_id": district_id,
        "block_id": block_oid,
        "block": block,
        "district": district,
        "state": state,
    }


def _resolve_upstream_ids(db, *, district_id=None, block_id=None, clf_id=None, pg_id=None):
    """
    Given a child geo id, resolve upstream ids.

    We store upstream ids on users and PGs for fast scoping and simpler dashboard filters.
    """
    out = {
        "state_id": None,
        "district_id": None,
        "block_id": None,
        "clf_id": None,
        "pg_id": None,
    }

    if pg_id:
        pg_oid = _to_object_id(pg_id)
        if pg_oid:
            pg = db.pgs.find_one(
                {"_id": pg_oid},
                {"state_id": 1, "district_id": 1, "block_id": 1, "clf_id": 1}
            )
            if pg:
                out.update({
                    "state_id": pg.get("state_id"),
                    "district_id": pg.get("district_id"),
                    "block_id": pg.get("block_id"),
                    "clf_id": pg.get("clf_id"),
                    "pg_id": pg_oid,
                })
                return out

    if clf_id:
        clf_oid = _to_object_id(clf_id)
        if clf_oid:
            clf = db.clfs.find_one(
                {"_id": clf_oid},
                {"state_id": 1, "district_id": 1, "block_id": 1}
            )
            if clf:
                out["clf_id"] = clf_oid
                out["block_id"] = clf.get("block_id")
                out["district_id"] = clf.get("district_id")
                out["state_id"] = clf.get("state_id")
                if clf.get("block_id"):
                    block_id = str(clf["block_id"])

    if block_id and not out.get("block_id"):
        block_oid = _to_object_id(block_id)
        if block_oid:
            blk = db.blocks.find_one({"_id": block_oid}, {"district_id": 1})
            if blk and blk.get("district_id"):
                out["block_id"] = block_oid
                district_id = str(blk["district_id"])

    if district_id and not out.get("district_id"):
        district_oid = _to_object_id(district_id)
        if district_oid:
            dist = db.districts.find_one({"_id": district_oid}, {"state_id": 1})
            if dist and dist.get("state_id"):
                out["district_id"] = district_oid
                out["state_id"] = dist.get("state_id")

    return out


def _create_user(db, data, creator_role):
    username = (data.get("username") or "").strip()

    if not username:
        raise ValueError("Username is required.")

    if db.users.find_one({"username": username}):
        raise ValueError("Username already exists.")

    password = data.get("password") or ""
    if not password:
        raise ValueError("Password is required.")

    user_doc = {
        "username": username,
        "password_hash": hash_password(password),
        "role": data.get("role"),
        "state_id": data.get("state_id"),
        "district_id": data.get("district_id"),
        "block_id": data.get("block_id"),
        "clf_id": data.get("clf_id"),
        "pg_id": data.get("pg_id"),
        "validator_level": data.get("validator_level"),
        "full_name": data.get("full_name") or data.get("name"),
        "name": data.get("name") or data.get("full_name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "assigned_pg_ids": data.get("assigned_pg_ids") or [],
        "status": data.get("status") or "active",
        "profile_validation_status": data.get("profile_validation_status") or "approved",
        "profile_validation_reason": data.get("profile_validation_reason") or "",
        "created_at": datetime.utcnow(),
        "created_by": creator_role,
        "created_by_user_id": _current_user_oid(),
        "last_login": None,
    }

    result = db.users.insert_one(user_doc)
    return result.inserted_id


def _pg_name(pg):
    return pg.get("name") or pg.get("pg_name") or "Unnamed PG"


def _clf_name(clf):
    return clf.get("name") or clf.get("clf_name") or "Unnamed CLF"


def _assigned_pg_ids_from_form():
    ids = []
    for pg_id in request.form.getlist("assigned_pg_ids"):
        oid = _to_object_id(pg_id)
        if oid:
            ids.append(oid)
    return ids


def _ensure_block_admin_scope():
    if session.get("role") != "BLOCK_ADMIN":
        raise ValueError("Only Block Admin can perform this action.")

    block_id = session.get("block_id")
    if not block_id or not ObjectId.is_valid(str(block_id)):
        raise ValueError("Your account is not mapped to a valid block.")

    return ObjectId(str(block_id))


# ============================================================
# SHG upload
# ============================================================

@master_data_bp.route("/shg/upload", methods=["GET", "POST"])
@roles_required("SUPER_ADMIN")
def shg_upload_replace():
    """Replace TRESP master data from an uploaded Excel.

    - Drops shg_master and shg_members_master (the large imported datasets)
    - Reimports from the uploaded workbook
    - Syncs States/Districts/Blocks by upserting from Excel (does not delete geo masters)
    """
    db = current_app.mongo_db

    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Please choose an Excel file.", "danger")
            return redirect(url_for("master_data.shg_upload_replace"))

        tmp_dir = os.path.join(current_app.instance_path, "uploads")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"tresp_master_{int(datetime.utcnow().timestamp())}.xlsx")
        f.save(tmp_path)

        # Drop existing imports
        db.shg_master.drop()
        db.shg_members_master.drop()

        try:
            from ..seeds.import_shg_master import main as import_master
            import_master(tmp_path)
            flash("Master data replaced successfully.", "success")
        except Exception as e:
            flash(f"Import failed: {e}", "danger")

        return redirect(url_for("master_data.shg_members_browser"))

    return render_template("shg_upload_replace.html")


def _geo_names_from_session(db, sess):
    """Resolve state/district/block names (strings) for LokOS scoping."""
    state_name = district_name = block_name = None

    state_id = sess.get("state_id")
    district_id = sess.get("district_id")
    block_id = sess.get("block_id")
    clf_id = sess.get("clf_id")

    try:
        # Resolve upstream ids if the session only has a child scope.
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
        return None, None, None

    return state_name, district_name, block_name


@master_data_bp.route("/shg/scope")
@login_required
def shg_scope():
    """Return the user's LokOS geo scope for auto-selecting cascading dropdowns."""
    db = current_app.mongo_db
    role = session.get("role")

    state_name, district_name, block_name = _geo_names_from_session(db, session)
    return jsonify({
        "role": role,
        "state": state_name,
        "district": district_name,
        "block": block_name,
    })


# ============================================================
# SHG MASTER (LokOS) – Cascading Dropdown APIs
# ============================================================

def _distinct_sorted(db, field, query=None):
    query = query or {}
    values = db.shg_master.distinct(field, query)
    values = [v for v in values if v not in (None, "")]
    return sorted(values, key=lambda x: str(x).lower())


@master_data_bp.route("/shg/states")
@login_required
def shg_states():
    db = current_app.mongo_db
    return jsonify(_distinct_sorted(db, "State"))


@master_data_bp.route("/shg/districts")
@login_required
def shg_districts():
    db = current_app.mongo_db
    state = request.args.get("state")
    q = {"State": state} if state else {}
    return jsonify(_distinct_sorted(db, "District", q))


@master_data_bp.route("/shg/blocks")
@login_required
def shg_blocks():
    db = current_app.mongo_db
    state = request.args.get("state")
    district = request.args.get("district")
    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    return jsonify(_distinct_sorted(db, "Block", q))


@master_data_bp.route("/shg/gps")
@login_required
def shg_gps():
    db = current_app.mongo_db
    state = request.args.get("state")
    district = request.args.get("district")
    block = request.args.get("block")
    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    if block:
        q["Block"] = block
    return jsonify(_distinct_sorted(db, "Gram Panchayat", q))


@master_data_bp.route("/shg/villages")
@login_required
def shg_villages():
    db = current_app.mongo_db
    state = request.args.get("state")
    district = request.args.get("district")
    block = request.args.get("block")
    gp = request.args.get("gp")
    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    if block:
        q["Block"] = block
    if gp:
        q["Gram Panchayat"] = gp
    return jsonify(_distinct_sorted(db, "Village", q))


@master_data_bp.route("/shg/codes")
@login_required
def shg_codes():
    db = current_app.mongo_db
    state = request.args.get("state")
    district = request.args.get("district")
    block = request.args.get("block")
    gp = request.args.get("gp")
    village = request.args.get("village")
    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    if block:
        q["Block"] = block
    if gp:
        q["Gram Panchayat"] = gp
    if village:
        q["Village"] = village

    codes = _distinct_sorted(db, "SHG Code", q)
    return jsonify(codes)


@master_data_bp.route("/shg/names")
@login_required
def shg_names():
    db = current_app.mongo_db
    shg_code = request.args.get("shg_code")
    if not shg_code:
        return jsonify([])
    names = _distinct_sorted(db, "SHG Name", {"SHG Code": shg_code})
    return jsonify(names)


@master_data_bp.route("/shg/details")
@login_required
def shg_details():
    """Return the full SHG master record for the selected SHG Code (+ optional name)."""
    db = current_app.mongo_db
    shg_code = request.args.get("shg_code")
    shg_name = request.args.get("shg_name")

    if not shg_code:
        return jsonify({})

    q = {"SHG Code": shg_code}
    if shg_name:
        q["SHG Name"] = shg_name

    doc = db.shg_master.find_one(q)
    if not doc:
        return jsonify({})

    out = {}
    for k, v in doc.items():
        if k == "_id":
            out[k] = str(v)
            continue
        if isinstance(v, datetime):
            out[k] = v.strftime("%Y-%m-%d")
            continue
        out[k] = v

    return jsonify(out)


@master_data_bp.route("/shg/search")
@login_required
def shg_search():
    """Search SHG master with optional geo filters, returns small list for UI tables."""
    db = current_app.mongo_db
    state = request.args.get("state")
    district = request.args.get("district")
    block = request.args.get("block")
    gp = request.args.get("gp")
    village = request.args.get("village")
    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    if block:
        q["Block"] = block
    if gp:
        q["Gram Panchayat"] = gp
    if village:
        q["Village"] = village

    limit = min(int(request.args.get("limit", 100)), 500)
    cursor = db.shg_master.find(q, {
        "State": 1,
        "District": 1,
        "Block": 1,
        "Gram Panchayat": 1,
        "Village": 1,
        "SHG Code": 1,
        "SHG Name": 1,
        "shg_nic_code": 1,
        "Active Members": 1,
        "Status": 1,
    }).limit(limit)

    out = []
    for d in cursor:
        d["_id"] = str(d["_id"])
        out.append(d)

    return jsonify(out)


@master_data_bp.route("/shg/browser")
@login_required
def shg_browser():
    """A lightweight UI for browsing the imported LokOS SHG master data."""
    return render_template("shg_browser.html")


# ============================================================
# SHG MEMBERS MASTER – Browser + Search
# ============================================================

@master_data_bp.route("/shg/members/search")
@login_required
def shg_members_search():
    db = current_app.mongo_db

    state = request.args.get("state")
    district = request.args.get("district")
    block = request.args.get("block")
    gp = request.args.get("gp")
    village = request.args.get("village")
    qtxt = (request.args.get("q") or "").strip()

    q = {}
    if state:
        q["State"] = state
    if district:
        q["District"] = district
    if block:
        q["Block"] = block
    if gp:
        q["Gram Panchayat"] = gp
    if village:
        q["Village"] = village

    if qtxt:
        q["$or"] = [
            {"Member Name": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"Member Code": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"SHG Name": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"SHG Code": {"$regex": re.escape(qtxt), "$options": "i"}},
        ]

    page = max(int(request.args.get("page", 1)), 1)
    page_size = int(request.args.get("page_size", request.args.get("limit", 100)))
    page_size = 25 if page_size not in (25, 50, 100) else page_size

    skip = (page - 1) * page_size

    projection = {
        "State": 1,
        "District": 1,
        "Block": 1,
        "Gram Panchayat": 1,
        "Village": 1,
        "SHG Name": 1,
        "SHG Code": 1,
        "Member Code": 1,
        "Member Name": 1,
        "Designation in SHG": 1,
        "Social Category": 1,
        "Religion": 1,
        "Education": 1,
        "Disability": 1,
        "Is head of Family": 1,
        "Father/Mother/Spouse Name": 1,
    }

    total = db.shg_members_master.count_documents(q)
    pages = max((total + page_size - 1) // page_size, 1)

    if page > pages:
        page = pages
        skip = (page - 1) * page_size

    cursor = (
        db.shg_members_master
        .find(q, projection)
        .sort([
            ("State", 1),
            ("District", 1),
            ("Block", 1),
            ("Gram Panchayat", 1),
            ("Village", 1),
            ("Member Code", 1),
        ])
        .skip(skip)
        .limit(page_size)
    )

    rows = []
    for d in cursor:
        d["_id"] = str(d["_id"])
        rows.append(d)

    return jsonify({
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages
    })


@master_data_bp.route("/shg/members/browser")
@login_required
def shg_members_browser():
    return render_template("shg_members_browser.html")


# ============================================================
# State / District / Block Masters
# ============================================================

@master_data_bp.route("/states", methods=["GET", "POST"])
@roles_required("SUPER_ADMIN")
def manage_states():
    db = current_app.mongo_db

    if request.method == "POST":
        code = request.form["code"].strip()
        name = request.form["name"].strip()

        if db.states.find_one({"code": code}):
            flash("State code already exists.", "danger")
        else:
            db.states.insert_one({
                "code": code,
                "name": name,
                "created_at": datetime.utcnow(),
            })
            flash("State added.", "success")

    states = list(db.states.find())
    return render_template("states.html", states=states)


@master_data_bp.route("/districts", methods=["GET", "POST"])
@roles_required("ADMIN", "SUPER_ADMIN")
def manage_districts():
    db = current_app.mongo_db
    role = session.get("role")
    session_state_id = session.get("state_id")

    states = list(db.states.find())
    if role == "ADMIN" and session_state_id:
        states = list(db.states.find({"_id": ObjectId(session_state_id)}))

    if request.method == "POST":
        name = request.form["name"].strip()
        state_ref = request.form.get("state_id") or session_state_id

        db.districts.insert_one({
            "name": name,
            "state_id": ObjectId(state_ref),
            "created_at": datetime.utcnow(),
        })

        flash("District added.", "success")

    districts_q = {}
    if role == "ADMIN" and session_state_id:
        districts_q["state_id"] = ObjectId(session_state_id)

    districts = list(db.districts.find(districts_q).sort("name", 1))
    return render_template("districts.html", states=states, districts=districts)


@master_data_bp.route("/blocks", methods=["GET", "POST"])
@roles_required("DISTRICT_ADMIN")
def manage_blocks():
    """District-level master data: Blocks."""
    db = current_app.mongo_db
    district_id = session.get("district_id")

    if not district_id:
        flash("No district scope found for your account.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if request.method == "POST":
        name = request.form["name"].strip()

        if not name:
            flash("Block name is required.", "danger")
        else:
            db.blocks.insert_one({
                "name": name,
                "district_id": ObjectId(district_id),
                "created_at": datetime.utcnow(),
                "created_by": _current_user_oid(),
            })
            flash("Block added.", "success")

    blocks = list(db.blocks.find({"district_id": ObjectId(district_id)}).sort("name", 1))
    return render_template("blocks.html", blocks=blocks)


# ============================================================
# CLF Master
# ============================================================

@master_data_bp.route("/clfs", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN")
def manage_clfs():
    """
    Block-level CLF master creation.

    Updated workflow:
    - Block Admin creates CLF master and CLF Admin login from the same form.
    - State, District and Block are auto-mapped from logged-in Block Admin.
    - CLF profile fields are stored in db.clfs.
    - No. of villages covered and PG count are NOT manually entered.
      They are calculated/displayed after PGs are mapped to the CLF.
    - Existing PG assignment and CLF login workflow remains compatible.
    """
    db = current_app.mongo_db
    scope = _get_block_scope(db)

    if not scope:
        flash("No valid block scope found for your account.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    block_id = scope["block_id"]
    district_id = scope["district_id"]
    state_id = scope["state_id"]

    if request.method == "POST":
        try:
            # -----------------------------
            # CLF master/profile fields
            # -----------------------------
            name = (request.form.get("name") or "").strip()
            vc_name_location = (request.form.get("vc_name_location") or "").strip()

            president_name = (request.form.get("president_name") or "").strip()
            president_contact = (request.form.get("president_contact") or "").strip()

            secretary_name = (request.form.get("secretary_name") or "").strip()
            secretary_contact = (request.form.get("secretary_contact") or "").strip()

            is_registered = (request.form.get("is_registered") or "").strip()
            registration_date_raw = (request.form.get("registration_date") or "").strip()

            clf_type = (request.form.get("clf_type") or "").strip()
            ec_members_count_raw = (request.form.get("ec_members_count") or "").strip()

            # -----------------------------
            # CLF login fields
            # -----------------------------
            full_name = (
                request.form.get("full_name")
                or request.form.get("admin_name")
                or request.form.get("clf_admin_name")
                or ""
            ).strip()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            # -----------------------------
            # Validation
            # -----------------------------
            if not name:
                raise ValueError("CLF name is required.")

            if not vc_name_location:
                raise ValueError("VC name/location of the CLF is required.")

            if not president_name:
                raise ValueError("Name of CLF President is required.")

            if not president_contact:
                raise ValueError("Contact no. of President is required.")

            if not secretary_name:
                raise ValueError("Name of CLF Secretary is required.")

            if not secretary_contact:
                raise ValueError("Contact no. of Secretary is required.")

            if is_registered not in ("yes", "no"):
                raise ValueError("Please select whether the CLF is registered.")

            registration_date = None
            if is_registered == "yes":
                if not registration_date_raw:
                    raise ValueError("Date of registration is required for registered CLF.")
                try:
                    registration_date = datetime.strptime(registration_date_raw, "%Y-%m-%d")
                except Exception:
                    raise ValueError("Invalid registration date format.")

            if clf_type not in ("Model CLF", "Non-Model CLF"):
                raise ValueError("Please select valid CLF type.")

            try:
                ec_members_count = int(ec_members_count_raw or 0)
            except Exception:
                raise ValueError("No. of EC members must be a valid number.")

            if ec_members_count < 0:
                raise ValueError("No. of EC members cannot be negative.")

            if not full_name:
                raise ValueError("CLF Admin name is required.")

            if not username:
                raise ValueError("Username is required.")

            if not password:
                raise ValueError("Password is required.")

            existing_clf = db.clfs.find_one({
                "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
                "block_id": block_id,
                "status": {"$ne": "deleted"},
            })

            if existing_clf:
                raise ValueError("This CLF already exists under your block.")

            existing_user = db.users.find_one({
                "username": username,
                "status": {"$ne": "deleted"},
            })

            if existing_user:
                raise ValueError("This username already exists. Please choose another username.")

            now = datetime.utcnow()
            current_user = _current_user_oid()

            # -----------------------------
            # Create CLF master
            # -----------------------------
            clf_doc = {
                "name": name,
                "clf_name": name,

                "state_id": state_id,
                "district_id": district_id,
                "block_id": block_id,

                "vc_name_location": vc_name_location,

                "president_name": president_name,
                "president_contact": president_contact,

                "secretary_name": secretary_name,
                "secretary_contact": secretary_contact,

                "is_registered": is_registered,
                "registration_date": registration_date,
                "registration_date_raw": registration_date_raw if is_registered == "yes" else "",

                "clf_type": clf_type,
                "ec_members_count": ec_members_count,

                # These two will be shown/calculated after PG mapping.
                # Do not take manual input for them.
                "village_covered_count": 0,
                "pg_count": 0,

                "assigned_pg_ids": [],
                "status": "active",

                "created_at": now,
                "created_by": current_user,
                "updated_at": now,
                "updated_by": current_user,
            }

            clf_insert = db.clfs.insert_one(clf_doc)
            clf_id = clf_insert.inserted_id

            # -----------------------------
            # Create CLF Admin login
            # -----------------------------
            user_id = _create_user(db, {
                "username": username,
                "password": password,
                "role": "CLF_ADMIN",

                "state_id": state_id,
                "district_id": district_id,
                "block_id": block_id,
                "clf_id": clf_id,

                "full_name": full_name,
                "name": full_name,

                "assigned_pg_ids": [],

                "status": "active",
                "profile_validation_status": "approved",

                "created_at": now,
                "created_by": current_user,
                "updated_at": now,
                "updated_by": current_user,
            }, "BLOCK_ADMIN")

            # -----------------------------
            # Map created login back to CLF
            # -----------------------------
            db.clfs.update_one(
                {"_id": clf_id},
                {"$set": {
                    "clf_admin_user_id": user_id,
                    "updated_at": datetime.utcnow(),
                    "updated_by": current_user,
                }}
            )

            flash("CLF and CLF login created successfully.", "success")
            return redirect(url_for("master_data.manage_clfs"))

        except Exception as e:
            flash(str(e), "danger")

    clfs = list(db.clfs.find({
        "block_id": block_id,
        "status": {"$ne": "deleted"},
    }).sort("name", 1))

    # ---------------------------------------------------------
    # Runtime calculated values after PG mapping
    # ---------------------------------------------------------
    for clf in clfs:
        clf_id = clf.get("_id")

        assigned_pg_ids = []
        for pg_id in clf.get("assigned_pg_ids") or []:
            oid = _to_object_id(pg_id)
            if oid:
                assigned_pg_ids.append(oid)

        pg_match = {
            "block_id": block_id,
            "status": {"$ne": "deleted"},
            "$or": [
                {"clf_id": clf_id},
                {"clf_id": str(clf_id)},
            ],
        }

        if assigned_pg_ids:
            pg_match["$or"].append({"_id": {"$in": assigned_pg_ids}})

        mapped_pgs = list(db.pgs.find(pg_match, {
            "_id": 1,
            "Village": 1,
            "village": 1,
            "village_name": 1,
            "Village Name": 1,
        }))

        villages = set()
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
                villages.add(village_name.lower())

        clf["pg_count_calculated"] = len(mapped_pgs)
        clf["village_covered_count_calculated"] = len(villages)

    return render_template(
        "clfs.html",
        clfs=clfs,
        block=scope.get("block"),
        district=scope.get("district"),
        state=scope.get("state"),
    )


# ============================================================
# CLF Admin Login Management
# ============================================================

@master_data_bp.route("/clf-admins", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN")
def manage_clf_admins():
    """
    Block Admin creates CLF Admin login.

    Required workflow:
    - Block Admin selects CLF.
    - Enters CLF Admin name, username, password.
    - CLF Admin is mapped with same district and block as Block Admin.
    """
    db = current_app.mongo_db
    scope = _get_block_scope(db)

    if not scope:
        flash("No valid block scope found for your account.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    block_id = scope["block_id"]

    clfs = list(db.clfs.find({"block_id": block_id}).sort("name", 1))
    clf_ids = [clf["_id"] for clf in clfs]

    if request.method == "POST":
        try:
            clf_id = request.form.get("clf_id", "").strip()
            full_name = (
                request.form.get("full_name")
                or request.form.get("name")
                or request.form.get("admin_name")
                or ""
            ).strip()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            clf_oid = _to_object_id(clf_id)

            if not clf_oid:
                raise ValueError("Please select a valid CLF.")

            clf_doc = db.clfs.find_one({"_id": clf_oid, "block_id": block_id})
            if not clf_doc:
                raise ValueError("Selected CLF does not belong to your block.")

            if not full_name:
                raise ValueError("CLF Admin name is required.")

            if not username:
                raise ValueError("Username is required.")

            if not password:
                raise ValueError("Password is required.")

            upstream = _resolve_upstream_ids(db, clf_id=clf_oid)

            user_id = _create_user(db, {
                "username": username,
                "password": password,
                "role": "CLF_ADMIN",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
                "block_id": upstream["block_id"],
                "clf_id": upstream["clf_id"],
                "full_name": full_name,
                "name": full_name,
                "assigned_pg_ids": [],
                "status": "active",
                "profile_validation_status": "approved",
            }, "BLOCK_ADMIN")

            db.clfs.update_one(
                {"_id": clf_oid},
                {"$set": {
                    "clf_admin_user_id": user_id,
                    "updated_at": datetime.utcnow(),
                    "updated_by": _current_user_oid(),
                }}
            )

            flash("CLF Admin login created successfully.", "success")
            return redirect(url_for("master_data.manage_clf_admins"))

        except Exception as e:
            flash(str(e), "danger")

    clf_admins = list(db.users.find({
        "role": "CLF_ADMIN",
        "block_id": block_id,
    }).sort("created_at", -1))

    clf_map = {str(clf["_id"]): clf for clf in clfs}

    for admin in clf_admins:
        admin_clf_id = str(admin.get("clf_id") or "")
        admin["clf_name"] = _clf_name(clf_map.get(admin_clf_id, {}))

        admin["assigned_pg_count"] = len(admin.get("assigned_pg_ids") or [])

    return render_template(
        "clf_admins.html",
        clfs=clfs,
        clf_admins=clf_admins,
        block=scope.get("block"),
        district=scope.get("district"),
        state=scope.get("state"),
    )


@master_data_bp.route("/clf-admins/<user_id>/toggle", methods=["POST"])
@roles_required("BLOCK_ADMIN")
def toggle_clf_admin_status(user_id):
    """
    Enable/disable CLF Admin under the logged-in Block Admin.
    """
    db = current_app.mongo_db
    block_id = _ensure_block_admin_scope()
    user_oid = _to_object_id(user_id)

    if not user_oid:
        flash("Invalid CLF Admin.", "danger")
        return redirect(url_for("master_data.manage_clf_admins"))

    user = db.users.find_one({
        "_id": user_oid,
        "role": "CLF_ADMIN",
        "block_id": block_id,
    })

    if not user:
        flash("CLF Admin not found under your block.", "danger")
        return redirect(url_for("master_data.manage_clf_admins"))

    current_status = (user.get("status") or "active").lower()
    new_status = "disabled" if current_status == "active" else "active"

    db.users.update_one(
        {"_id": user_oid},
        {"$set": {
            "status": new_status,
            "updated_at": datetime.utcnow(),
            "updated_by": _current_user_oid(),
        }}
    )

    flash(f"CLF Admin status changed to {new_status}.", "success")
    return redirect(url_for("master_data.manage_clf_admins"))


@master_data_bp.route("/clf-pg-assignment", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN")
def clf_pg_assignment():
    db = current_app.mongo_db

    block_id = session.get("block_id")
    district_id = session.get("district_id")
    state_id = session.get("state_id")

    if not block_id or not ObjectId.is_valid(str(block_id)):
        flash("No valid block scope found for your account.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    block_oid = ObjectId(str(block_id))
    district_oid = ObjectId(str(district_id)) if district_id and ObjectId.is_valid(str(district_id)) else None
    state_oid = ObjectId(str(state_id)) if state_id and ObjectId.is_valid(str(state_id)) else None

    # ---------------------------------------------------------
    # Resolve current scope display
    # ---------------------------------------------------------
    block = db.blocks.find_one({"_id": block_oid})
    district = db.districts.find_one({"_id": district_oid}) if district_oid else None
    state = db.states.find_one({"_id": state_oid}) if state_oid else None

    # Fallback: derive district/state from block if session is missing them
    if block and not district and block.get("district_id"):
        district = db.districts.find_one({"_id": block.get("district_id")})

    if district and not state and district.get("state_id"):
        state = db.states.find_one({"_id": district.get("state_id")})

    # ---------------------------------------------------------
    # CLF admins under this block
    # Handles both ObjectId and string block_id safely
    # ---------------------------------------------------------
    clf_admins = list(
        db.users.find({
            "role": "CLF_ADMIN",
            "$or": [
                {"block_id": block_oid},
                {"block_id": str(block_oid)},
            ],
            "status": {"$ne": "deleted"},
        }).sort("username", 1)
    )

    clfs = list(
        db.clfs.find({
            "$or": [
                {"block_id": block_oid},
                {"block_id": str(block_oid)},
            ]
        }).sort("name", 1)
    )

    clf_name_by_id = {}
    for c in clfs:
        clf_name_by_id[str(c["_id"])] = c.get("name") or c.get("clf_name") or "Mapped CLF"

        clf_admin_clf_name_by_user_id = {}

    # Add CLF name and assigned count into CLF admin records
    for admin in clf_admins:
        admin["clf_id_str"] = str(admin.get("clf_id") or "")
        admin["clf_name"] = clf_name_by_id.get(admin["clf_id_str"], "Mapped CLF")
        admin["assigned_pg_count"] = len(admin.get("assigned_pg_ids") or [])

        clf_admin_clf_name_by_user_id[str(admin.get("_id") or "")] = admin["clf_name"]

    # ---------------------------------------------------------
    # PGs under this block
    # Handles both ObjectId and string block_id safely
    # ---------------------------------------------------------
    pgs = list(
        db.pgs.find({
            "$or": [
                {"block_id": block_oid},
                {"block_id": str(block_oid)},
            ]
        }).sort("name", 1)
    )

    for pg in pgs:
        pg["display_name"] = pg.get("name") or pg.get("pg_name") or "Unnamed PG"

        pg["assigned_clf_user_id_str"] = str(pg.get("assigned_clf_user_id") or "")
        pg["current_clf_id_str"] = str(pg.get("clf_id") or "")

        current_clf_name = clf_name_by_id.get(pg["current_clf_id_str"], "")

        if not current_clf_name and pg["assigned_clf_user_id_str"]:
            current_clf_name = clf_admin_clf_name_by_user_id.get(
                pg["assigned_clf_user_id_str"],
                ""
            )

        pg["current_clf_name"] = current_clf_name or "another CLF"
        pg["assignment_conflict"] = False

    # ---------------------------------------------------------
    # GET selected CLF admin from URL
    # Example:
    # /master/clf-pg-assignment?clf_admin_id=<id>
    # ---------------------------------------------------------
    selected_user_id = (
        request.args.get("clf_admin_id")
        or request.form.get("clf_admin_id")
        or ""
    ).strip()

    assignment_mode = (
        request.args.get("mode")
        or request.form.get("mode")
        or "overview"
    ).strip().lower()

    if assignment_mode not in ("overview", "manage"):
        assignment_mode = "overview"

    selected_admin = None
    selected_assigned_ids = []
    mapped_pgs = []

    if selected_user_id and ObjectId.is_valid(selected_user_id):
        selected_admin = db.users.find_one({
            "_id": ObjectId(selected_user_id),
            "role": "CLF_ADMIN",
            "$or": [
                {"block_id": block_oid},
                {"block_id": str(block_oid)},
            ],
            "status": {"$ne": "deleted"},
        })

        if selected_admin:
            selected_admin["clf_id_str"] = str(selected_admin.get("clf_id") or "")
            selected_admin["clf_name"] = clf_name_by_id.get(
                selected_admin["clf_id_str"],
                "Mapped CLF"
            )

            selected_assigned_ids = [
                str(x) for x in (selected_admin.get("assigned_pg_ids") or [])
            ]

            mapped_pg_oids = []
            for pg_id in selected_admin.get("assigned_pg_ids") or []:
                if ObjectId.is_valid(str(pg_id)):
                    mapped_pg_oids.append(ObjectId(str(pg_id)))

            if mapped_pg_oids:
                mapped_pgs = list(
                    db.pgs.find({
                        "_id": {"$in": mapped_pg_oids},
                        "$or": [
                            {"block_id": block_oid},
                            {"block_id": str(block_oid)},
                        ],
                    }).sort("name", 1)
                )

                for pg in mapped_pgs:
                    pg["display_name"] = pg.get("name") or pg.get("pg_name") or "Unnamed PG"
                    pg["assigned_clf_user_id_str"] = str(pg.get("assigned_clf_user_id") or "")
            else:
                mapped_pgs = []



                   # ---------------------------------------------------------
    # Mark conflict PGs for frontend warning popup
    # Conflict means: PG is already mapped to another CLF/Admin
    # and current action may transfer it to selected CLF Admin.
    # ---------------------------------------------------------
    if selected_admin:
        selected_clf_id_str = str(selected_admin.get("clf_id") or "")
        selected_admin_id_str = str(selected_admin.get("_id") or "")

        for pg in pgs:
            pg_clf_id_str = str(pg.get("clf_id") or "")
            pg_assigned_user_id_str = str(pg.get("assigned_clf_user_id") or "")

            has_existing_mapping = bool(pg_clf_id_str or pg_assigned_user_id_str)

            belongs_to_selected_admin = (
                pg_assigned_user_id_str
                and selected_admin_id_str
                and pg_assigned_user_id_str == selected_admin_id_str
            )

            belongs_to_selected_clf_without_other_admin = (
                pg_clf_id_str
                and selected_clf_id_str
                and pg_clf_id_str == selected_clf_id_str
                and not pg_assigned_user_id_str
            )

            pg["assignment_conflict"] = (
                has_existing_mapping
                and not belongs_to_selected_admin
                and not belongs_to_selected_clf_without_other_admin
            )

    # ---------------------------------------------------------
    # POST save assignment
    # ---------------------------------------------------------
    if request.method == "POST":
        clf_admin_id = (request.form.get("clf_admin_id") or "").strip()

        assignment_action = (
            request.form.get("assignment_action")
            or "replace"
        ).strip().lower()

        if assignment_action not in ("add", "replace", "delete"):
            assignment_action = "replace"

        # IMPORTANT:
        # Template checkboxes must use name="pg_ids"
        selected_pg_ids = request.form.getlist("pg_ids")

        if not clf_admin_id or not ObjectId.is_valid(clf_admin_id):
            flash("Please select a valid CLF Admin.", "danger")
            return redirect(url_for("master_data.clf_pg_assignment"))

        clf_admin = db.users.find_one({
            "_id": ObjectId(clf_admin_id),
            "role": "CLF_ADMIN",
            "$or": [
                {"block_id": block_oid},
                {"block_id": str(block_oid)},
            ],
            "status": {"$ne": "deleted"},
        })

        if not clf_admin:
            flash("Invalid CLF Admin selected or CLF Admin is outside your block.", "danger")
            return redirect(url_for("master_data.clf_pg_assignment"))

        clf_id = clf_admin.get("clf_id")
        if not clf_id:
            flash("Selected CLF Admin is not mapped with any CLF.", "danger")
            return redirect(url_for("master_data.clf_pg_assignment", clf_admin_id=clf_admin_id))

        clf_oid = ObjectId(str(clf_id)) if ObjectId.is_valid(str(clf_id)) else clf_id

        clean_pg_oids = []
        for pg_id in selected_pg_ids:
            if ObjectId.is_valid(str(pg_id)):
                clean_pg_oids.append(ObjectId(str(pg_id)))

        now = datetime.utcnow()

        # If no PG selected, remove all previous assignment for this selected CLF Admin
        old_assigned_pg_ids = []
        for old_pg_id in clf_admin.get("assigned_pg_ids") or []:
            if ObjectId.is_valid(str(old_pg_id)):
                old_assigned_pg_ids.append(ObjectId(str(old_pg_id)))

        if not clean_pg_oids:
            if assignment_action == "replace":
                db.users.update_one(
                    {"_id": ObjectId(clf_admin_id)},
                    {
                        "$set": {
                            "assigned_pg_ids": [],
                            "updated_at": now,
                        }
                    }
                )

                if old_assigned_pg_ids:
                    db.pgs.update_many(
                        {
                            "_id": {"$in": old_assigned_pg_ids},
                            "$or": [
                                {"block_id": block_oid},
                                {"block_id": str(block_oid)},
                            ],
                            "assigned_clf_user_id": ObjectId(clf_admin_id),
                        },
                        {
                            "$unset": {
                                "clf_id": "",
                                "assigned_clf_user_id": "",
                                "assigned_clf_username": "",
                            },
                            "$set": {
                                "updated_at": now,
                            },
                        }
                    )

                flash("All PG assignments removed from selected CLF Admin.", "success")
                return redirect(url_for("master_data.clf_pg_assignment", clf_admin_id=clf_admin_id))

            flash("Please select at least one PG.", "danger")
            return redirect(url_for(
                "master_data.clf_pg_assignment",
                clf_admin_id=clf_admin_id,
                mode="manage"
            ))

        # Security: only allow PGs from same block
        allowed_pg_ids = [
            p["_id"] for p in db.pgs.find({
                "_id": {"$in": clean_pg_oids},
                "$or": [
                    {"block_id": block_oid},
                    {"block_id": str(block_oid)},
                ],
            }, {"_id": 1})
        ]

        if not allowed_pg_ids:
            flash("No valid PG found under your block for assignment.", "danger")
            return redirect(url_for("master_data.clf_pg_assignment", clf_admin_id=clf_admin_id))
        

                # ---------------------------------------------------------
        # Strict backend safety:
        # If Add/Replace includes PGs already mapped to another CLF/Admin,
        # frontend must send transfer_confirmed=1.
        # ---------------------------------------------------------
        transfer_confirmed = (request.form.get("transfer_confirmed") or "").strip() == "1"

        if assignment_action in ("add", "replace"):
            conflict_pgs = []

            for pg_doc in db.pgs.find({
                "_id": {"$in": allowed_pg_ids},
                "$or": [
                    {"block_id": block_oid},
                    {"block_id": str(block_oid)},
                ],
            }, {
                "name": 1,
                "pg_name": 1,
                "clf_id": 1,
                "assigned_clf_user_id": 1,
            }):
                pg_clf_id_str = str(pg_doc.get("clf_id") or "")
                pg_assigned_user_id_str = str(pg_doc.get("assigned_clf_user_id") or "")

                has_existing_mapping = bool(pg_clf_id_str or pg_assigned_user_id_str)

                belongs_to_selected_clf = (
                    pg_clf_id_str
                    and str(clf_oid)
                    and pg_clf_id_str == str(clf_oid)
                )

                belongs_to_selected_admin = (
                    pg_assigned_user_id_str
                    and pg_assigned_user_id_str == str(clf_admin_id)
                )

                if has_existing_mapping and not belongs_to_selected_clf and not belongs_to_selected_admin:
                    conflict_pgs.append(pg_doc)

            if conflict_pgs and not transfer_confirmed:
                flash("Strict warning required: one or more selected PGs are already mapped to another CLF. Please confirm transfer before saving.", "danger")
                return redirect(url_for(
                    "master_data.clf_pg_assignment",
                    clf_admin_id=clf_admin_id,
                    mode="manage",
                    _anchor="managePgAssignmentSection",
                ))

             # ---------------------------------------------------------
        # DELETE action:
        # Remove selected PGs from this CLF Admin only.
        # ---------------------------------------------------------
        if assignment_action == "delete":
            delete_pg_ids = [
                pg_id for pg_id in allowed_pg_ids
                if pg_id in old_assigned_pg_ids
            ]

            if not delete_pg_ids:
                flash("Selected PGs are not currently assigned to this CLF Admin.", "danger")
                return redirect(url_for(
                    "master_data.clf_pg_assignment",
                    clf_admin_id=clf_admin_id,
                    mode="manage"
                ))

            remaining_pg_ids = [
                pg_id for pg_id in old_assigned_pg_ids
                if pg_id not in delete_pg_ids
            ]

            db.users.update_one(
                {"_id": ObjectId(clf_admin_id)},
                {
                    "$set": {
                        "assigned_pg_ids": remaining_pg_ids,
                        "updated_at": now,
                    }
                }
            )

            db.pgs.update_many(
                {
                    "_id": {"$in": delete_pg_ids},
                    "assigned_clf_user_id": ObjectId(clf_admin_id),
                    "$or": [
                        {"block_id": block_oid},
                        {"block_id": str(block_oid)},
                    ],
                },
                {
                    "$unset": {
                        "clf_id": "",
                        "assigned_clf_user_id": "",
                        "assigned_clf_username": "",
                    },
                    "$set": {
                        "updated_at": now,
                    },
                }
            )

            flash(f"{len(delete_pg_ids)} PG(s) removed from selected CLF Admin.", "success")
            return redirect(url_for("master_data.clf_pg_assignment", clf_admin_id=clf_admin_id))

        # ---------------------------------------------------------
        # ADD action:
        # Keep existing assigned PGs and add newly selected PGs.
        # ---------------------------------------------------------
        if assignment_action == "add":
            final_pg_ids = []

            for pg_id in old_assigned_pg_ids + allowed_pg_ids:
                if pg_id not in final_pg_ids:
                    final_pg_ids.append(pg_id)

        # ---------------------------------------------------------
        # REPLACE action:
        # Replace existing assignment with selected PGs.
        # ---------------------------------------------------------
        else:
            final_pg_ids = allowed_pg_ids

        # Remove newly selected PGs from other CLF Admin users of same block
        db.users.update_many(
            {
                "role": "CLF_ADMIN",
                "_id": {"$ne": ObjectId(clf_admin_id)},
                "$or": [
                    {"block_id": block_oid},
                    {"block_id": str(block_oid)},
                ],
            },
            {
                "$pull": {
                    "assigned_pg_ids": {"$in": final_pg_ids}
                },
                "$set": {
                    "updated_at": now
                }
            }
        )

        # Save final PG ids in selected CLF Admin user
        db.users.update_one(
            {"_id": ObjectId(clf_admin_id)},
            {
                "$set": {
                    "assigned_pg_ids": final_pg_ids,
                    "updated_at": now,
                }
            }
        )

        # For REPLACE only: clear old PG mappings that are no longer selected
        if assignment_action == "replace" and old_assigned_pg_ids:
            db.pgs.update_many(
                {
                    "_id": {
                        "$in": [
                            x for x in old_assigned_pg_ids
                            if x not in final_pg_ids
                        ]
                    },
                    "assigned_clf_user_id": ObjectId(clf_admin_id),
                },
                {
                    "$unset": {
                        "clf_id": "",
                        "assigned_clf_user_id": "",
                        "assigned_clf_username": "",
                    },
                    "$set": {
                        "updated_at": now,
                    },
                }
            )

        # Save CLF mapping directly into PG records
        db.pgs.update_many(
            {
                "_id": {"$in": final_pg_ids},
                "$or": [
                    {"block_id": block_oid},
                    {"block_id": str(block_oid)},
                ],
            },
            {
                "$set": {
                    "clf_id": clf_oid,
                    "assigned_clf_user_id": ObjectId(clf_admin_id),
                    "assigned_clf_username": clf_admin.get("username"),
                    "updated_at": now,
                }
            }
        )

        if assignment_action == "add":
            flash(f"{len(allowed_pg_ids)} PG(s) added to CLF successfully.", "success")
        else:
            flash(f"{len(final_pg_ids)} PG assignment list updated successfully.", "success")

        return redirect(url_for("master_data.clf_pg_assignment", clf_admin_id=clf_admin_id))

    return render_template(
        "clf_pg_assignment.html",
        state=state,
        district=district,
        block=block,
        clfs=clfs,
        clf_admins=clf_admins,
        pgs=pgs,
        mapped_pgs=mapped_pgs,
        assignment_mode=assignment_mode,
        selected_user_id=selected_user_id,
        selected_admin=selected_admin,
        selected_assigned_ids=selected_assigned_ids,
    )


# ============================================================
# PG Master
# ============================================================

@master_data_bp.route("/pgs", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def manage_pgs():
    """
    PG master creation/listing.

    New workflow:
    - PG creation remains with Block/Admin hierarchy.
    - CLF_ADMIN cannot create PG.
    - CLF_MANAGER removed from creation access.
    """
    db = current_app.mongo_db
    role = session.get("role")

    state_id_sess = session.get("state_id")
    district_id_sess = session.get("district_id")
    block_id_sess = session.get("block_id")

    # =========================
    # LIST SCOPE FILTER
    # =========================
    q = {}
    scope_badge = "All states"

    if role == "BLOCK_ADMIN":
        q["block_id"] = ObjectId(block_id_sess)
        scope_badge = "Block scope"

    elif role == "DISTRICT_ADMIN":
        q["district_id"] = ObjectId(district_id_sess)
        scope_badge = "District scope"

    elif role == "ADMIN":
        q["state_id"] = ObjectId(state_id_sess)
        scope_badge = "State scope"

    # =========================
    # POST Basic Creation
    # =========================
    if request.method == "POST":
        create_mode = request.form.get("create_mode")

        if create_mode == "basic":
            try:
                st = (request.form.get("State") or "").strip()
                dist_name = (request.form.get("District") or "").strip()
                blk_name = (request.form.get("Block") or "").strip()
                gp = (request.form.get("Gram Panchayat") or "").strip()
                village = (request.form.get("Village") or "").strip()

                pg_name = (request.form.get("pg_name") or "").strip()
                username = (request.form.get("username") or "").strip()
                password = (request.form.get("password") or "").strip()

                if not (st and dist_name and blk_name and gp and village and pg_name and username and password):
                    raise ValueError("All fields are required.")

                state_doc = db.states.find_one({"name": st})
                if not state_doc:
                    raise ValueError("Invalid State.")
                state_id = state_doc["_id"]

                district_doc = db.districts.find_one({
                    "name": dist_name,
                    "state_id": state_id,
                })
                if not district_doc:
                    raise ValueError("Invalid District.")
                district_id = district_doc["_id"]

                block_doc = db.blocks.find_one({
                    "name": blk_name,
                    "district_id": district_id,
                })
                if not block_doc:
                    raise ValueError("Invalid Block.")
                block_id_obj = block_doc["_id"]

                # Role Enforcement
                if role == "BLOCK_ADMIN" and str(block_id_obj) != str(block_id_sess):
                    raise ValueError("Unauthorized block selection.")

                if role == "DISTRICT_ADMIN" and str(district_id) != str(district_id_sess):
                    raise ValueError("Unauthorized district selection.")

                if role == "ADMIN" and str(state_id) != str(state_id_sess):
                    raise ValueError("Unauthorized state selection.")

                if db.users.find_one({"username": username}):
                    raise ValueError("Username already exists.")

                existing_pg = db.pgs.find_one({
                    "name": {"$regex": f"^{re.escape(pg_name)}$", "$options": "i"},
                    "block_id": block_id_obj,
                })

                if existing_pg:
                    raise ValueError("PG name already exists in this block.")

                pg_insert = {
                    "name": pg_name,
                    "State": st,
                    "District": dist_name,
                    "Block": blk_name,
                    "Gram Panchayat": gp,
                    "Village": village,
                    "block_id": block_id_obj,
                    "district_id": district_id,
                    "state_id": state_id,
                    "clf_id": None,
                    "assigned_clf_user_id": None,
                    "status": "draft",
                    "registration_validation": {
                        "status": "draft",
                        "remarks": "",
                        "submitted_at": None,
                        "reviewed_by": None,
                        "reviewed_at": None,
                        "history": [],
                    },
                    "member_registration_validation": {
                        "status": "draft",
                        "remarks": "",
                        "submitted_at": None,
                        "reviewed_by": None,
                        "reviewed_at": None,
                        "history": [],
                    },
                    "created_at": datetime.utcnow(),
                    "created_by": _current_user_oid(),
                    "updated_at": datetime.utcnow(),
                }

                pg_res = db.pgs.insert_one(pg_insert)
                new_pg_id = pg_res.inserted_id

                db.users.insert_one({
                    "username": username,
                    "password_hash": hash_password(password),
                    "role": "PG_DATA_ENTRY",
                    "pg_id": new_pg_id,
                    "state_id": state_id,
                    "district_id": district_id,
                    "block_id": block_id_obj,
                    "clf_id": None,
                    "status": "active",
                    "assigned_pg_ids": [],
                    "created_at": datetime.utcnow(),
                    "created_by": role,
                    "created_by_user_id": _current_user_oid(),
                    "last_login": None,
                })

                flash("PG and Login created successfully. Assign the PG to a CLF from CLF PG Assignment.", "success")
                return redirect(url_for("master_data.manage_pgs"))

            except Exception as e:
                flash(str(e), "danger")
                return redirect(url_for("master_data.manage_pgs"))

    # =========================
    # GET DATA
    # =========================
    pgs = list(db.pgs.find(q).sort("created_at", -1))

    clf_ids = []
    assigned_user_ids = []

    for pg in pgs:
        if pg.get("clf_id"):
            clf_ids.append(pg.get("clf_id"))
        if pg.get("assigned_clf_user_id"):
            assigned_user_ids.append(pg.get("assigned_clf_user_id"))

    clfs = list(db.clfs.find({"_id": {"$in": clf_ids}})) if clf_ids else []
    clf_admins = list(db.users.find({"_id": {"$in": assigned_user_ids}})) if assigned_user_ids else []

    clf_map = {str(clf["_id"]): clf for clf in clfs}
    clf_admin_map = {str(user["_id"]): user for user in clf_admins}

    for pg in pgs:
        pg["display_name"] = _pg_name(pg)
        pg["clf_name"] = _clf_name(clf_map.get(str(pg.get("clf_id") or ""), {})) if pg.get("clf_id") else ""
        assigned_user = clf_admin_map.get(str(pg.get("assigned_clf_user_id") or ""))
        pg["assigned_clf_admin_name"] = (
            assigned_user.get("full_name")
            or assigned_user.get("name")
            or assigned_user.get("username")
            if assigned_user else ""
        )

    return render_template(
        "pgs.html",
        pgs=pgs,
        scope_badge=scope_badge
    )


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available