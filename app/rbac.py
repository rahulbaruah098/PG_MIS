from functools import wraps
from flask import session, redirect, url_for, flash, abort, request, jsonify, current_app, g
import jwt


def _authenticate_request():
    """
    Hybrid authentication:
    1) Session (Web)
    2) JWT Token (Mobile)
    """

    # -----------------------------
    # 1) Web session authentication
    # -----------------------------
    if session.get("user_id"):
        g.user_id = session.get("user_id")
        g.role = session.get("role")
        g.pg_id = session.get("pg_id")
        g.clf_id = session.get("clf_id")
        g.block_id = session.get("block_id")
        g.district_id = session.get("district_id")
        g.state_id = session.get("state_id")
        return True

    # -----------------------------
    # 2) JWT token authentication
    # -----------------------------
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]

        try:
            payload = jwt.decode(
                token,
                current_app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )

            g.user_id = payload.get("user_id")
            g.role = payload.get("role")
            g.pg_id = payload.get("pg_id")
            g.clf_id = payload.get("clf_id")
            g.block_id = payload.get("block_id")
            g.district_id = payload.get("district_id")
            g.state_id = payload.get("state_id")
            return True

        except jwt.ExpiredSignatureError:
            return False
        except jwt.InvalidTokenError:
            return False

    return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _authenticate_request():
            return view(*args, **kwargs)

        if request.is_json or request.headers.get("Authorization"):
            return jsonify({"error": "Authentication required"}), 401

        flash("Please log in to access this page.", "warning")
        return redirect(url_for("auth.login", next=request.path))

    return wrapped


def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _authenticate_request():
                if request.is_json or request.headers.get("Authorization"):
                    return jsonify({"error": "Authentication required"}), 401
                flash("Please log in.", "warning")
                return redirect(url_for("auth.login"))

            role = getattr(g, "role", None)

            if role not in allowed_roles:
                if request.is_json or request.headers.get("Authorization"):
                    return jsonify({"error": "Forbidden"}), 403
                abort(403)

            return view(*args, **kwargs)

        return wrapped
    return decorator


def permissions_required(*required_perms):
    from .permissions import has_permission

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _authenticate_request():
                if request.is_json or request.headers.get("Authorization"):
                    return jsonify({"error": "Authentication required"}), 401
                flash("Please log in.", "warning")
                return redirect(url_for("auth.login"))

            role = getattr(g, "role", None)

            for p in required_perms:
                if not has_permission(role, p):
                    if request.is_json or request.headers.get("Authorization"):
                        return jsonify({"error": "Forbidden"}), 403
                    abort(403)

            return view(*args, **kwargs)

        return wrapped
    return decorator