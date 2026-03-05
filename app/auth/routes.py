from services.audit_engine import AuditLogger
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app,jsonify
from bson import ObjectId
from datetime import datetime,timedelta
import jwt
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
        
        # After: user = db.users.find_one({"username": username})
        # Add this block before building the JWT payload: ---- "ATLANTA GOGOI"

        pg_name = None
        if user.get("pg_id"):
            pg_doc = db.pgs.find_one({"_id": ObjectId(user["pg_id"])}, {"name": 1})
            if pg_doc:
                pg_name = pg_doc.get("name")

        if not user or not verify_password(password, user["password_hash"]):
            return jsonify({"error": "Invalid username or password"}), 401

        # Update last login
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_login": datetime.utcnow()}}
        )

        # 🔐 Generate JWT token for mobile
        payload = {
            "user_id": str(user["_id"]),
            "role": user["role"],
            "pg_id": str(user.get("pg_id")) if user.get("pg_id") else None,
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
                "username": user["username"],
                "role": user["role"],
                "state_id": str(user.get("state_id")) if user.get("state_id") else None,
                "district_id": str(user.get("district_id")) if user.get("district_id") else None,
                "block_id": str(user.get("block_id")) if user.get("block_id") else None,
                "clf_id": str(user.get("clf_id")) if user.get("clf_id") else None,
                "pg_id": str(user.get("pg_id")) if user.get("pg_id") else None,
                "pg_name" : pg_name,
                "validator_level": user.get("validator_level"),
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

        session.clear()
        session["user_id"] = str(user["_id"])
        session["role"] = user["role"]
        session["state_id"] = json_safe(user.get("state_id"))
        session["district_id"] = json_safe(user.get("district_id"))
        session["block_id"] = json_safe(user.get("block_id"))
        session["clf_id"] = json_safe(user.get("clf_id"))
        session["pg_id"] = json_safe(user.get("pg_id"))
        session["validator_level"] = user.get("validator_level")

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
        "status": "active",
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
