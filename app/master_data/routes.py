from flask import render_template, request, redirect, url_for, flash, current_app, session, jsonify
from bson import ObjectId
from datetime import datetime
import re
import os

from . import master_data_bp
from ..rbac import login_required, roles_required
from ..services.workflow import next_pg_auto_id


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


# ===============================
# SHG MASTER (LokOS) – Cascading Dropdown APIs
# ===============================

def _distinct_sorted(db, field, query=None):
    query = query or {}
    values = db.shg_master.distinct(field, query)
    # Normalize + sort (case-insensitive)
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
    """Return the full SHG master record for the selected SHG Code (+ optional name).
    Used to auto-fill the remaining fields in forms.
    """
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

    # Convert ObjectId + datetimes to JSON-friendly values
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
        "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1,
        "SHG Code": 1, "SHG Name": 1, "shg_nic_code": 1, "Active Members": 1,
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
    """A lightweight UI for browsing the imported LokOS SHG master data.

    All roles can view this (read-only). The page uses the cascading dropdown APIs
    already implemented above.
    """
    return render_template("shg_browser.html")



# ===============================
# SHG MEMBERS MASTER – Browser + Search (140k+ rows)
# ===============================

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
    if state: q["State"] = state
    if district: q["District"] = district
    if block: q["Block"] = block
    if gp: q["Gram Panchayat"] = gp
    if village: q["Village"] = village

    if qtxt:
        q["$or"] = [
            {"Member Name": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"Member Code": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"SHG Name": {"$regex": re.escape(qtxt), "$options": "i"}},
            {"SHG Code": {"$regex": re.escape(qtxt), "$options": "i"}},
        ]

    # ✅ page + page_size (25/50/100)
    page = max(int(request.args.get("page", 1)), 1)
    page_size = int(request.args.get("page_size", request.args.get("limit", 100)))
    page_size = 25 if page_size not in (25, 50, 100) else page_size

    skip = (page - 1) * page_size

    projection = {
        "State": 1, "District": 1, "Block": 1, "Gram Panchayat": 1, "Village": 1,
        "SHG Name": 1, "SHG Code": 1,
        "Member Code": 1, "Member Name": 1,
        "Designation in SHG": 1, "Social Category": 1, "Religion": 1, "Education": 1,
        "Disability": 1, "Is head of Family": 1, "Father/Mother/Spouse Name": 1,
    }

    # ✅ total count for pagination
    total = db.shg_members_master.count_documents(q)
    pages = max((total + page_size - 1) // page_size, 1)

    # clamp page if user clicks beyond end
    if page > pages:
        page = pages
        skip = (page - 1) * page_size

    # ✅ stable sort so paging is consistent (avoid random ordering)
    cursor = (
        db.shg_members_master
        .find(q, projection)
        .sort([("State", 1), ("District", 1), ("Block", 1), ("Gram Panchayat", 1), ("Village", 1), ("Member Code", 1)])
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
    # State Admin should only manage districts under their own state.
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
    """District-level master data: Blocks (one-time / change-based)."""
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
            })
            flash("Block added.", "success")

    blocks = list(db.blocks.find({"district_id": ObjectId(district_id)}).sort("name", 1))
    return render_template("blocks.html", blocks=blocks)


@master_data_bp.route("/clfs", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN")
def manage_clfs():
    """Block-level master data: CLFs."""
    db = current_app.mongo_db
    block_id = session.get("block_id")
    if not block_id:
        flash("No block scope found for your account.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("CLF name is required.", "danger")
        else:
            db.clfs.insert_one({
                "name": name,
                "block_id": ObjectId(block_id),
                "created_at": datetime.utcnow(),
            })
            flash("CLF added.", "success")

    clfs = list(db.clfs.find({"block_id": ObjectId(block_id)}).sort("name", 1))
    return render_template("clfs.html", clfs=clfs)


@master_data_bp.route("/pgs", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def manage_pgs():

    from bson import ObjectId
    from datetime import datetime
    from ..utils import hash_password

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
    # POST (Basic Creation)
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

                # Resolve IDs
                state_doc = db.states.find_one({"name": st})
                if not state_doc:
                    raise ValueError("Invalid State.")
                state_id = state_doc["_id"]

                district_doc = db.districts.find_one(
                    {"name": dist_name, "state_id": state_id}
                )
                if not district_doc:
                    raise ValueError("Invalid District.")
                district_id = district_doc["_id"]

                block_doc = db.blocks.find_one(
                    {"name": blk_name, "district_id": district_id}
                )
                if not block_doc:
                    raise ValueError("Invalid Block.")
                block_id_obj = block_doc["_id"]

                # 🔒 Role Enforcement
                if role == "BLOCK_ADMIN" and str(block_id_obj) != str(block_id_sess):
                    raise ValueError("Unauthorized block selection.")

                if role == "DISTRICT_ADMIN" and str(district_id) != str(district_id_sess):
                    raise ValueError("Unauthorized district selection.")

                if role == "ADMIN" and str(state_id) != str(state_id_sess):
                    raise ValueError("Unauthorized state selection.")

                # Username uniqueness
                if db.users.find_one({"username": username}):
                    raise ValueError("Username already exists.")

                # Create PG
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
                    "status": "draft",
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }

                pg_res = db.pgs.insert_one(pg_insert)
                new_pg_id = pg_res.inserted_id

                # Create Login
                db.users.insert_one({
                    "username": username,
                    "password_hash": hash_password(password),
                    "role": "PG_DATA_ENTRY",
                    "pg_id": new_pg_id,
                    "state_id": state_id,
                    "district_id": district_id,
                    "block_id": block_id_obj,
                    "status": "active",
                    "created_at": datetime.utcnow(),
                    "created_by": role,
                })

                flash("PG and Login created successfully.", "success")
                return redirect(url_for("master_data.manage_pgs"))

            except Exception as e:
                flash(str(e), "danger")
                return redirect(url_for("master_data.manage_pgs"))

    # =========================
    # GET DATA
    # =========================
    pgs = list(db.pgs.find(q).sort("created_at", -1))

    return render_template(
        "pgs.html",
        pgs=pgs,
        scope_badge=scope_badge
    )


