from services.audit_engine import AuditLogger
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify, send_file, abort
from bson import ObjectId
from datetime import datetime,timedelta
import jwt
import os
from werkzeug.utils import secure_filename
from ..utils import verify_password, hash_password, json_safe
from ..rbac import login_required, roles_required
from ..rbac import permissions_required
from ..permissions import P_NOTIFICATIONS_VIEW
from ..constants import ROLES


auth_bp = Blueprint("auth", __name__, template_folder="../templates")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    db = current_app.mongo_db

    # ==========================================================
    # MOBILE LOGIN (JSON request → Returns JWT)
    # ==========================================================
    if request.method == "POST" and request.content_type and "application/json" in request.content_type:
        data = request.get_json(silent=True) or {}

        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"error": "Username and password required"}), 400

        user = db.users.find_one({"username": username})

        if not user or not verify_password(password, user["password_hash"]):
            return jsonify({"error": "Invalid username or password"}), 401

        pg_name = None
        if user.get("pg_id"):
            try:
                pg_oid = user.get("pg_id")
                if not isinstance(pg_oid, ObjectId):
                    pg_oid = ObjectId(pg_oid)
                pg_doc = db.pgs.find_one({"_id": pg_oid}, {"name": 1})
                if pg_doc:
                    pg_name = pg_doc.get("name")
            except Exception:
                pg_name = None

        # Update last login
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_login": datetime.utcnow()}}
        )

        # 🔐 Generate JWT token for mobile (FULL HYBRID SCOPE)
        payload = {
            "user_id": str(user["_id"]),
            "role": user.get("role"),
            "state_id": str(user.get("state_id")) if user.get("state_id") else None,
            "district_id": str(user.get("district_id")) if user.get("district_id") else None,
            "block_id": str(user.get("block_id")) if user.get("block_id") else None,
            "clf_id": str(user.get("clf_id")) if user.get("clf_id") else None,
            "pg_id": str(user.get("pg_id")) if user.get("pg_id") else None,
            "validator_level": user.get("validator_level"),
            "assigned_pg_ids": [str(x) for x in (user.get("assigned_pg_ids") or [])],
            "exp": datetime.utcnow() + timedelta(hours=24),
        }

        token = jwt.encode(
            payload,
            current_app.config["JWT_SECRET_KEY"],
            algorithm="HS256",
        )

        return jsonify({
            "message": "Login successful",
            "token": token,
            "user": {
                "id": str(user["_id"]),
                "username": user.get("username"),
                "role": user.get("role"),
                "state_id": str(user.get("state_id")) if user.get("state_id") else None,
                "district_id": str(user.get("district_id")) if user.get("district_id") else None,
                "block_id": str(user.get("block_id")) if user.get("block_id") else None,
                "clf_id": str(user.get("clf_id")) if user.get("clf_id") else None,
                "pg_id": str(user.get("pg_id")) if user.get("pg_id") else None,
                "pg_name": pg_name,
                "validator_level": user.get("validator_level"),
                "assigned_pg_ids": [str(x) for x in (user.get("assigned_pg_ids") or [])],
            }
        }), 200

    # ==========================================================
    # WEB LOGIN (Form submit → Creates Session)
    # ==========================================================
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db.users.find_one({"username": username})

        if not user or not verify_password(password, user["password_hash"]):
            flash("Invalid username or password.", "danger")
            return render_template("login.html")

        status = (user.get("status") or "active").lower()
        if status != "active":
            if status == "pending":
                flash("Your account is pending block approval.", "warning")
            elif status == "rejected":
                flash("Your account registration was rejected. Please contact your Block Admin.", "danger")
            else:
                flash("Your account is not active.", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = str(user["_id"])
        session["role"] = user["role"]
        session["state_id"] = json_safe(user.get("state_id"))
        session["district_id"] = json_safe(user.get("district_id"))
        session["block_id"] = json_safe(user.get("block_id"))
        session["clf_id"] = json_safe(user.get("clf_id"))
        session["pg_id"] = json_safe(user.get("pg_id"))
        session["validator_level"] = user.get("validator_level")
        session["assigned_pg_ids"] = [str(x) for x in (user.get("assigned_pg_ids") or [])]

        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_login": datetime.utcnow()}}
        )

        flash("Logged in successfully.", "success")

        role = user["role"]
        if role in ["SUPER_ADMIN", "ADMIN"]:
            return redirect(url_for("reports.state_dashboard"))
        elif role in ["DISTRICT_ADMIN", "BLOCK_ADMIN", "CLF_MANAGER"]:
            return redirect(url_for("reports.hierarchy_dashboard"))
        elif role == "CADRE_CC":
            return redirect(url_for("reports.cadre_dashboard"))
        elif role == "PG_DATA_ENTRY":
            return redirect(url_for("pg.pg_home"))
        else:
            return redirect(url_for("reports.hierarchy_dashboard"))

    # ==========================================================
    # GET REQUEST → Render login page (Web only)
    # ==========================================================
    return render_template("login.html")

@auth_bp.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))


def _project_root():
    return os.path.dirname(current_app.root_path)


def _resolved_upload_root():
    upload_root = current_app.config.get("UPLOAD_FOLDER", "uploads") or "uploads"
    if os.path.isabs(upload_root):
        return os.path.normpath(upload_root)
    return os.path.normpath(os.path.join(_project_root(), upload_root))


def _save_cadre_file(file_storage, subdir="cadre_docs"):
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None

    folder = os.path.join(_resolved_upload_root(), subdir)
    os.makedirs(folder, exist_ok=True)

    original = secure_filename(file_storage.filename)
    final_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{original}"
    abs_path = os.path.join(folder, final_name)
    file_storage.save(abs_path)

    # Store a stable relative path in Mongo so the app can move across machines.
    return os.path.join("uploads", subdir, final_name).replace("\\", "/")


def _resolve_cadre_document_path(stored_path, subdir="cadre_docs"):
    if not stored_path:
        return None

    project_root = _project_root()
    app_root = current_app.root_path
    upload_root = _resolved_upload_root()
    normalized = str(stored_path).replace("\\", "/").strip()

    candidates = []

    if os.path.isabs(stored_path):
        candidates.append(os.path.normpath(stored_path))
        parts = normalized.lower().split("/uploads/", 1)
        if len(parts) == 2:
            tail = parts[1]
            candidates.append(os.path.normpath(os.path.join(upload_root, tail)))
            candidates.append(os.path.normpath(os.path.join(app_root, "uploads", tail)))
    else:
        candidates.append(os.path.normpath(os.path.join(project_root, normalized)))
        candidates.append(os.path.normpath(os.path.join(app_root, normalized)))
        if normalized.startswith("uploads/"):
            tail = normalized[len("uploads/"):]
            candidates.append(os.path.normpath(os.path.join(upload_root, tail)))
            candidates.append(os.path.normpath(os.path.join(app_root, "uploads", tail)))

    basename = os.path.basename(normalized)
    if basename:
        candidates.append(os.path.normpath(os.path.join(upload_root, subdir, basename)))
        candidates.append(os.path.normpath(os.path.join(app_root, "uploads", subdir, basename)))
        candidates.append(os.path.normpath(os.path.join(project_root, "uploads", subdir, basename)))

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate):
            return candidate

    return None


def _serialize_profile_from_form(form, files=None, keep_existing_aadhaar=None):
    files = files or {}
    profile = {
        "full_name": (form.get("full_name") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "bank_name": (form.get("bank_name") or "").strip(),
        "account_number": (form.get("account_number") or "").strip(),
        "account_name": (form.get("account_name") or "").strip(),
        "ifsc_code": (form.get("ifsc_code") or "").strip(),
        "branch_name": (form.get("branch_name") or "").strip(),
    }
    aadhaar_path = _save_cadre_file(files.get("aadhaar_card")) if files else None
    if aadhaar_path:
        profile["aadhaar_card"] = aadhaar_path
    elif keep_existing_aadhaar:
        profile["aadhaar_card"] = keep_existing_aadhaar
    return profile


def _profile_summary_doc(profile):
    profile = profile or {}
    return {
        "full_name": profile.get("full_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "bank_name": profile.get("bank_name", ""),
        "account_number": profile.get("account_number", ""),
        "account_name": profile.get("account_name", ""),
        "ifsc_code": profile.get("ifsc_code", ""),
        "branch_name": profile.get("branch_name", ""),
        "aadhaar_card": profile.get("aadhaar_card"),
    }


def _seed_current_profile_fields(user_doc):
    return {
        "full_name": user_doc.get("full_name", ""),
        "email": user_doc.get("email", ""),
        "phone": user_doc.get("phone", ""),
        "bank_name": user_doc.get("bank_name", ""),
        "account_number": user_doc.get("account_number", ""),
        "account_name": user_doc.get("account_name", ""),
        "ifsc_code": user_doc.get("ifsc_code", ""),
        "branch_name": user_doc.get("branch_name", ""),
        "aadhaar_card": user_doc.get("aadhaar_card"),
    }


def _cadre_form_payload(db, form, files=None, block_id=None, self_registration=False):
    files = files or {}
    selected_block_id = block_id or (form.get("block_id") or "").strip()
    if not selected_block_id or not ObjectId.is_valid(selected_block_id):
        raise ValueError("Please select a valid block.")

    upstream = _resolve_upstream_ids(db, block_id=selected_block_id)
    pg_ids = form.getlist("assigned_pg_ids")
    assigned_oids = []
    for pg_id in pg_ids:
        if not ObjectId.is_valid(pg_id):
            continue
        pg_doc = db.pgs.find_one({"_id": ObjectId(pg_id)}, {"block_id": 1})
        if not pg_doc or str(pg_doc.get("block_id")) != str(selected_block_id):
            raise ValueError("Cadre can only be assigned PGs from the selected block.")
        assigned_oids.append(ObjectId(pg_id))

    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    full_name = (form.get("full_name") or "").strip()
    phone = (form.get("phone") or "").strip()
    email = (form.get("email") or "").strip()

    if not username:
        raise ValueError("Username is required.")
    if not password:
        raise ValueError("Password is required.")
    if not full_name:
        raise ValueError("Cadre name is required.")

    aadhaar_path = _save_cadre_file(files.get("aadhaar_card")) if files else None

    data = {
        "username": username,
        "password": password,
        "role": "CADRE_CC",
        "state_id": upstream["state_id"],
        "district_id": upstream["district_id"],
        "block_id": upstream["block_id"],
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "aadhaar_card": aadhaar_path,
        "bank_name": (form.get("bank_name") or "").strip(),
        "account_number": (form.get("account_number") or "").strip(),
        "account_name": (form.get("account_name") or "").strip(),
        "ifsc_code": (form.get("ifsc_code") or "").strip(),
        "branch_name": (form.get("branch_name") or "").strip(),
        "assigned_pg_ids": assigned_oids,
        "pg_id": assigned_oids[0] if assigned_oids else None,
        "status": "pending" if self_registration else "active",
        "is_self_registered": bool(self_registration),
        "approved_at": None,
        "approved_by": None,
    }
    return data


def _create_user(data, creator_role):
    db = current_app.mongo_db
    username = data["username"]
    if db.users.find_one({"username": username}):
        raise ValueError("Username already exists.")

    user_doc = {
        "username": username,
        "password_hash": hash_password(data["password"]),
        "role": data["role"],
        "state_id": data.get("state_id"),
        "district_id": data.get("district_id"),
        "block_id": data.get("block_id"),
        "clf_id": data.get("clf_id"),
        "pg_id": data.get("pg_id"),
        "validator_level": data.get("validator_level"),
        "full_name": data.get("full_name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "aadhaar_card": data.get("aadhaar_card"),
        "bank_name": data.get("bank_name"),
        "account_number": data.get("account_number"),
        "account_name": data.get("account_name"),
        "ifsc_code": data.get("ifsc_code"),
        "branch_name": data.get("branch_name"),
        "assigned_pg_ids": data.get("assigned_pg_ids") or [],
        "status": data.get("status") or "active",
        "is_self_registered": bool(data.get("is_self_registered")),
        "approved_at": data.get("approved_at"),
        "approved_by": data.get("approved_by"),
        "profile_validation_status": data.get("profile_validation_status") or "approved",
        "profile_validation_reason": data.get("profile_validation_reason") or "",
        "profile_submitted_at": data.get("profile_submitted_at"),
        "profile_validated_at": data.get("profile_validated_at"),
        "profile_validated_by": data.get("profile_validated_by"),
        "pending_profile": data.get("pending_profile"),
        "created_at": datetime.utcnow(),
        "created_by": creator_role,
        "last_login": None,
    }
    db.users.insert_one(user_doc)

@auth_bp.route("/users/create/admin", methods=["GET", "POST"])
@roles_required("SUPER_ADMIN")
def create_state_admin():
    db = current_app.mongo_db
    states = list(db.states.find())
    if request.method == "POST":
        try:
            data = {
                "username": request.form["username"],
                "password": request.form["password"],
                "role": "ADMIN",
                "state_id": request.form["state_id"],
            }
            _create_user(data, "SUPER_ADMIN")
            flash("State admin created.", "success")
            return redirect(url_for("auth.create_state_admin"))
        except Exception as e:
            flash(str(e), "danger")
    return render_template("user_create_admin.html", states=states)


def _resolve_upstream_ids(db, *, district_id=None, block_id=None, clf_id=None, pg_id=None):
    """Given a child geo id, resolve upstream ids.

    We store upstream ids on users for fast scoping and simpler dashboard filters.
    """
    out = {"state_id": None, "district_id": None, "block_id": None, "clf_id": None, "pg_id": None}
    if pg_id:
        pg = db.pgs.find_one({"_id": ObjectId(pg_id)}, {"state_id": 1, "district_id": 1, "block_id": 1, "clf_id": 1})
        if pg:
            out.update({
                "state_id": pg.get("state_id"),
                "district_id": pg.get("district_id"),
                "block_id": pg.get("block_id"),
                "clf_id": pg.get("clf_id"),
                "pg_id": ObjectId(pg_id),
            })
            return out

    if clf_id:
        clf = db.clfs.find_one({"_id": ObjectId(clf_id)}, {"block_id": 1})
        if clf and clf.get("block_id"):
            out["clf_id"] = ObjectId(clf_id)
            block_id = str(clf["block_id"])

    if block_id:
        blk = db.blocks.find_one({"_id": ObjectId(block_id)}, {"district_id": 1})
        if blk and blk.get("district_id"):
            out["block_id"] = ObjectId(block_id)
            district_id = str(blk["district_id"])

    if district_id:
        dist = db.districts.find_one({"_id": ObjectId(district_id)}, {"state_id": 1})
        if dist and dist.get("state_id"):
            out["district_id"] = ObjectId(district_id)
            out["state_id"] = dist.get("state_id")

    return out


@auth_bp.route("/users/create/pg-member", methods=["GET", "POST"])
@roles_required("DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def create_pg_member():
    """Create a PG login (PG_DATA_ENTRY) scoped to a PG.

    Requested: District Admin should be able to create PG logins.
    This keeps the existing PG_DATA_ENTRY workflow intact (PG user can update PG + members).
    """
    db = current_app.mongo_db
    role = session.get("role")

    # Scope PG list
    q = {}
    if role == "DISTRICT_ADMIN" and session.get("district_id"):
        q["district_id"] = ObjectId(session.get("district_id"))
    elif role == "ADMIN" and session.get("state_id"):
        q["state_id"] = ObjectId(session.get("state_id"))

    pgs = list(db.pgs.find(q).sort("name", 1))

    if request.method == "POST":
        try:
            pg_id = request.form["pg_id"]
            upstream = _resolve_upstream_ids(db, pg_id=pg_id)

            # Hard scope enforcement
            if role == "DISTRICT_ADMIN" and session.get("district_id") and str(upstream.get("district_id")) != str(session.get("district_id")):
                raise ValueError("You cannot create PG login outside your district.")
            if role == "ADMIN" and session.get("state_id") and str(upstream.get("state_id")) != str(session.get("state_id")):
                raise ValueError("You cannot create PG login outside your state.")

            data = {
                "username": request.form["username"].strip(),
                "password": request.form["password"],
                "role": "PG_DATA_ENTRY",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
                "block_id": upstream["block_id"],
                "clf_id": upstream["clf_id"],
                "pg_id": upstream["pg_id"],
            }
            _create_user(data, role)
            flash("PG login created.", "success")
            return redirect(url_for("auth.create_pg_member"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_pg.html", pgs=pgs, title="Create PG Login")


@auth_bp.route("/users/create/district", methods=["GET", "POST"])
@roles_required("ADMIN")
def create_district_admin():
    """State Admin creates District Admin (flow: SUPER_ADMIN -> ADMIN -> DISTRICT_ADMIN)."""
    db = current_app.mongo_db
    # State admin is scoped to one state.
    state_id = session.get("state_id")
    districts_q = {"state_id": ObjectId(state_id)} if state_id else {}
    districts = list(db.districts.find(districts_q).sort("name", 1))

    if request.method == "POST":
        try:
            district_id = request.form["district_id"]
            upstream = _resolve_upstream_ids(db, district_id=district_id)
            data = {
                "username": request.form["username"].strip(),
                "password": request.form["password"],
                "role": "DISTRICT_ADMIN",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
            }
            _create_user(data, "ADMIN")
            flash("District admin created.", "success")
            return redirect(url_for("auth.create_district_admin"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_district.html", districts=districts)


@auth_bp.route("/users/create/block", methods=["GET", "POST"])
@roles_required("DISTRICT_ADMIN")
def create_clf_admin():
    """District Admin creates CLF Admin (formerly Block Admin)."""
    db = current_app.mongo_db
    district_id = session.get("district_id")
    blocks_q = {"district_id": ObjectId(district_id)} if district_id else {}
    blocks = list(db.blocks.find(blocks_q).sort("name", 1))

    if request.method == "POST":
        try:
            block_id = request.form["block_id"]
            upstream = _resolve_upstream_ids(db, block_id=block_id)
            data = {
                "username": request.form["username"].strip(),
                "password": request.form["password"],
                "role": "BLOCK_ADMIN",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
                "block_id": upstream["block_id"],
            }
            _create_user(data, "DISTRICT_ADMIN")
            flash("CLF admin created.", "success")
            return redirect(url_for("auth.create_clf_admin"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_block.html", blocks=blocks, title="Create CLF Admin")


@auth_bp.route("/users/create/cadre", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN")
def create_cadre_cc():
    """Block Admin creates Cadre (CC) accounts and assigns PGs from the same block only."""
    db = current_app.mongo_db
    block_id = session.get("block_id")
    if not block_id:
        flash("Your account is not mapped to a block.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    pgs = list(db.pgs.find({"block_id": ObjectId(block_id)}).sort("name", 1))

    if request.method == "POST":
        try:
            data = _cadre_form_payload(db, request.form, request.files, block_id=block_id, self_registration=False)
            _create_user(data, "BLOCK_ADMIN")
            flash("Cadre (CC) account created.", "success")
            return redirect(url_for("auth.manage_cadre_cc"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_cadre.html", pgs=pgs, title="Create Cadre (CC)", form_mode="create", pending_approval=False, blocks=[])


@auth_bp.route("/cadre/register", methods=["GET", "POST"])
def register_cadre_cc():
    db = current_app.mongo_db
    blocks = list(db.blocks.find().sort("name", 1))
    selected_block = request.form.get("block_id") if request.method == "POST" else request.args.get("block_id")
    pgs = []
    if selected_block and ObjectId.is_valid(selected_block):
        pgs = list(db.pgs.find({"block_id": ObjectId(selected_block)}).sort("name", 1))

    if request.method == "POST":
        try:
            data = _cadre_form_payload(db, request.form, request.files, self_registration=True)
            _create_user(data, "SELF_REGISTERED_CADRE")
            flash("Cadre registration submitted. Your Block Admin will approve it.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_cadre.html", pgs=pgs, title="Cadre (CC) Registration", form_mode="register", pending_approval=True, blocks=blocks, selected_block=selected_block)


@auth_bp.route("/cadre/profile", methods=["GET", "POST"])
@login_required
@roles_required("CADRE_CC")
def cadre_profile():
    db = current_app.mongo_db
    user_id = session.get("user_id")
    user = db.users.find_one({"_id": ObjectId(user_id)})
    assigned_pg_ids = user.get("assigned_pg_ids") or []
    pgs = list(db.pgs.find({"_id": {"$in": assigned_pg_ids}}).sort("name", 1)) if assigned_pg_ids else []

    if request.method == "POST":
        profile_payload = _serialize_profile_from_form(request.form, request.files, keep_existing_aadhaar=user.get("aadhaar_card"))
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "pending_profile": _profile_summary_doc(profile_payload),
                "profile_validation_status": "pending",
                "profile_validation_reason": "",
                "profile_submitted_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }}
        )
        flash("Your profile has been submitted to the Block Admin for validation.", "success")
        return redirect(url_for("auth.cadre_profile"))

    pending_profile = user.get("pending_profile") or {}
    user_view = dict(user)
    if pending_profile:
        user_view.update({k: v for k, v in pending_profile.items() if v not in (None, "")})
    return render_template(
        "user_create_cadre.html",
        pgs=pgs,
        title="Cadre Profile",
        form_mode="profile",
        pending_approval=False,
        blocks=[],
        user_doc=user_view,
        profile_status=user.get("profile_validation_status") or "approved",
        profile_reason=user.get("profile_validation_reason") or "",
        pending_profile=pending_profile,
    )


@auth_bp.route("/users/manage/cadre", methods=["GET"])
@roles_required("BLOCK_ADMIN")
def manage_cadre_cc():
    db = current_app.mongo_db
    block_id = session.get("block_id")
    cadres = list(db.users.find({"role": "CADRE_CC", "block_id": ObjectId(block_id)}).sort("created_at", -1)) if block_id else []
    pgs = list(db.pgs.find({"block_id": ObjectId(block_id)}).sort("name", 1)) if block_id else []
    pg_name_map = {str(pg["_id"]): (pg.get("name") or pg.get("pg_name") or "Unnamed PG") for pg in pgs}
    validations = [c for c in cadres if (c.get("profile_validation_status") == "pending" and c.get("pending_profile"))]
    return render_template("manage_cadre_cc.html", cadres=cadres, pgs=pgs, pg_name_map=pg_name_map, validations=validations)


@auth_bp.route("/users/manage/cadre/<user_id>/assign", methods=["POST"])
@roles_required("BLOCK_ADMIN")
def assign_cadre_pgs(user_id):
    db = current_app.mongo_db
    block_id = session.get("block_id")
    user = db.users.find_one({"_id": ObjectId(user_id), "role": "CADRE_CC", "block_id": ObjectId(block_id)})
    if not user:
        flash("Cadre account not found in your block.", "danger")
        return redirect(url_for("auth.manage_cadre_cc"))

    assigned_oids = []
    for pg_id in request.form.getlist("assigned_pg_ids"):
        if not ObjectId.is_valid(pg_id):
            continue
        pg_doc = db.pgs.find_one({"_id": ObjectId(pg_id)}, {"block_id": 1})
        if pg_doc and str(pg_doc.get("block_id")) == str(block_id):
            assigned_oids.append(ObjectId(pg_id))

    db.users.update_one({"_id": user["_id"]}, {"$set": {"assigned_pg_ids": assigned_oids, "pg_id": assigned_oids[0] if assigned_oids else None, "updated_at": datetime.utcnow()}})
    flash("Assigned PG list updated for Cadre.", "success")
    return redirect(url_for("auth.manage_cadre_cc"))


@auth_bp.route("/users/manage/cadre/<user_id>/approve", methods=["POST"])
@roles_required("BLOCK_ADMIN")
def approve_cadre_cc(user_id):
    db = current_app.mongo_db
    block_id = session.get("block_id")
    user = db.users.find_one({"_id": ObjectId(user_id), "role": "CADRE_CC", "block_id": ObjectId(block_id)})
    if not user:
        flash("Cadre account not found in your block.", "danger")
        return redirect(url_for("auth.manage_cadre_cc"))

    db.users.update_one({"_id": user["_id"]}, {"$set": {"status": "active", "approved_at": datetime.utcnow(), "approved_by": session.get("user_id")}})
    flash("Cadre account approved.", "success")
    return redirect(url_for("auth.manage_cadre_cc"))


@auth_bp.route("/users/manage/cadre/<user_id>/profile-approve", methods=["POST"])
@roles_required("BLOCK_ADMIN")
def approve_cadre_profile(user_id):
    db = current_app.mongo_db
    block_id = session.get("block_id")
    user = db.users.find_one({"_id": ObjectId(user_id), "role": "CADRE_CC", "block_id": ObjectId(block_id)})
    if not user:
        flash("Cadre account not found in your block.", "danger")
        return redirect(url_for("auth.manage_cadre_cc"))
    pending_profile = user.get("pending_profile") or {}
    if not pending_profile:
        flash("No pending profile validation found for this Cadre.", "warning")
        return redirect(url_for("auth.manage_cadre_cc"))
    update_doc = _profile_summary_doc(pending_profile)
    update_doc.update({
        "pending_profile": None,
        "profile_validation_status": "approved",
        "profile_validation_reason": "",
        "profile_validated_at": datetime.utcnow(),
        "profile_validated_by": session.get("user_id"),
        "updated_at": datetime.utcnow(),
    })
    db.users.update_one({"_id": user["_id"]}, {"$set": update_doc})
    flash("Cadre profile approved.", "success")
    return redirect(url_for("auth.manage_cadre_cc"))


@auth_bp.route("/users/manage/cadre/<user_id>/profile-reject", methods=["POST"])
@roles_required("BLOCK_ADMIN")
def reject_cadre_profile(user_id):
    db = current_app.mongo_db
    block_id = session.get("block_id")
    user = db.users.find_one({"_id": ObjectId(user_id), "role": "CADRE_CC", "block_id": ObjectId(block_id)})
    if not user:
        flash("Cadre account not found in your block.", "danger")
        return redirect(url_for("auth.manage_cadre_cc"))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Please provide a rejection reason.", "danger")
        return redirect(url_for("auth.manage_cadre_cc"))
    db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "pending_profile": None,
            "profile_validation_status": "rejected",
            "profile_validation_reason": reason,
            "profile_validated_at": datetime.utcnow(),
            "profile_validated_by": session.get("user_id"),
            "updated_at": datetime.utcnow(),
        }}
    )
    flash("Cadre profile validation rejected.", "warning")
    return redirect(url_for("auth.manage_cadre_cc"))


@auth_bp.route("/cadre/document/<user_id>")
@login_required
def view_cadre_document(user_id):
    db = current_app.mongo_db

    if not ObjectId.is_valid(user_id):
        return abort(404, description="Invalid user id")

    user = db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return abort(404, description="User not found")

    mode = (request.args.get("mode") or "").strip().lower()

    if mode == "pending":
        filename = (user.get("pending_profile") or {}).get("aadhaar_card")
    else:
        filename = user.get("aadhaar_card")

    if not filename:
        return abort(404, description="Document not uploaded")

    path = _resolve_cadre_document_path(filename)
    if not path:
        return abort(404, description="File not found")

    return send_file(path)


@auth_bp.route("/users/create/clf", methods=["GET", "POST"])
@roles_required("BLOCK_ADMIN", "CLF_MANAGER")
def create_clf_manager():
    """Block Admin creates CLF Official."""
    db = current_app.mongo_db
    block_id = session.get("block_id")
    clfs_q = {"block_id": ObjectId(block_id)} if block_id else {}
    clfs = list(db.clfs.find(clfs_q).sort("name", 1))

    if request.method == "POST":
        try:
            clf_id = request.form["clf_id"]
            upstream = _resolve_upstream_ids(db, clf_id=clf_id)
            data = {
                "username": request.form["username"].strip(),
                "password": request.form["password"],
                "role": "CLF_MANAGER",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
                "block_id": upstream["block_id"],
                "clf_id": upstream["clf_id"],
            }
            _create_user(data, "BLOCK_ADMIN")
            flash("CLF Official created.", "success")
            return redirect(url_for("auth.create_clf_manager"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_clf.html", clfs=clfs)


@auth_bp.route("/users/create/pg", methods=["GET", "POST"])
@roles_required("CLF_MANAGER")
def create_pg_data_entry():
    """CLF Official creates PG Data Entry user."""
    db = current_app.mongo_db
    clf_id = session.get("clf_id")
    pgs_q = {"clf_id": ObjectId(clf_id)} if clf_id else {}
    pgs = list(db.pgs.find(pgs_q).sort("name", 1))

    if request.method == "POST":
        try:
            pg_id = request.form["pg_id"]
            upstream = _resolve_upstream_ids(db, pg_id=pg_id)
            data = {
                "username": request.form["username"].strip(),
                "password": request.form["password"],
                "role": "PG_DATA_ENTRY",
                "state_id": upstream["state_id"],
                "district_id": upstream["district_id"],
                "block_id": upstream["block_id"],
                "clf_id": upstream["clf_id"],
                "pg_id": upstream["pg_id"],
            }
            _create_user(data, "CLF_MANAGER")
            flash("PG data entry user created.", "success")
            return redirect(url_for("auth.create_pg_data_entry"))
        except Exception as e:
            flash(str(e), "danger")

    return render_template("user_create_pg.html", pgs=pgs)


@auth_bp.route("/users/password-reset", methods=["GET", "POST"])
@roles_required("SUPER_ADMIN")
def super_admin_password_reset():
    """SUPER_ADMIN can reset password for any user account (any role)."""
    db = current_app.mongo_db

    if request.method == "POST":
        try:
            user_id = request.form.get("user_id")
            new_password = request.form.get("new_password", "")

            if not user_id:
                raise ValueError("Missing user_id.")
            if not new_password or len(new_password) < 6:
                raise ValueError("Password must be at least 6 characters.")

            res = db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {
                    "password_hash": hash_password(new_password),
                    "password_reset_at": datetime.utcnow(),
                    "password_reset_by": ObjectId(session.get("user_id")) if session.get("user_id") else None,
                }}
            )
            if res.matched_count == 0:
                raise ValueError("User not found.")

            flash("Password reset successfully.", "success")
            return redirect(url_for("auth.super_admin_password_reset"))
        except Exception as e:
            flash(str(e), "danger")

    users = list(db.users.find(
        {},
        {"username": 1, "role": 1, "status": 1, "last_login": 1}
    ).sort("role", 1))
    for u in users:
        u["_id"] = str(u["_id"])

    return render_template("user_reset_password.html", users=users)


@auth_bp.route("/notifications")
@login_required
@permissions_required(P_NOTIFICATIONS_VIEW)
def notifications():
    db = current_app.mongo_db
    user_id = session.get("user_id")
    role = session.get("role")
    q = {"$or": [{"to_user_id": str(user_id)}, {"to_role": role}]}
    notes = list(db.notifications.find(q).sort("ts", -1).limit(200))
    return render_template("notifications.html", notes=notes)


# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available
