from functools import wraps
from flask import (
    session,
    redirect,
    url_for,
    flash,
    abort,
    request,
    jsonify,
    current_app,
    g,
)
import jwt


# ------------------------------------------------------------
# Role constants / aliases.  
# ------------------------------------------------------------

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_DISTRICT_ADMIN = "DISTRICT_ADMIN"
ROLE_BLOCK_ADMIN = "BLOCK_ADMIN"
ROLE_CLF_ADMIN = "CLF_ADMIN"
ROLE_CLF_MANAGER = "CLF_MANAGER"   # legacy role, kept for backward compatibility
ROLE_CADRE_CC = "CADRE_CC"
ROLE_PG_DATA_ENTRY = "PG_DATA_ENTRY"


ADMIN_ROLES = {
    ROLE_SUPER_ADMIN,
    ROLE_ADMIN,
}

DISTRICT_ROLES = {
    ROLE_DISTRICT_ADMIN,
}

BLOCK_ROLES = {
    ROLE_BLOCK_ADMIN,
}

CLF_ROLES = {
    ROLE_CLF_ADMIN,
}

PG_ROLES = {
    ROLE_PG_DATA_ENTRY,
}

CADRE_ROLES = {
    ROLE_CADRE_CC,
}


def _wants_json_response():
    """
    Detect API/mobile/AJAX requests.
    """
    accept = request.headers.get("Accept", "") or ""

    return (
        request.is_json
        or request.headers.get("Authorization")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept
    )


def _normalize_role(role):
    """
    Normalize old/legacy role names.

    CLF_MANAGER is kept for existing DB/users/templates compatibility,
    but new workflow should use CLF_ADMIN.
    """
    if not role:
        return None

    role = str(role).strip().upper()

    # New workflow role
    if role == ROLE_CLF_ADMIN:
        return ROLE_CLF_ADMIN

    # Legacy compatibility:
    # Existing project had CLF_MANAGER. Do not break old users instantly.
    if role == ROLE_CLF_MANAGER:
        return ROLE_CLF_MANAGER

    return role


def _normalize_allowed_roles(allowed_roles):
    """
    Normalize allowed role list while keeping old role behavior safe.
    """
    normalized = set()

    for role in allowed_roles:
        role = _normalize_role(role)
        if not role:
            continue

        normalized.add(role)

        # If a route allows old CLF_MANAGER, allow new CLF_ADMIN too.
        if role == ROLE_CLF_MANAGER:
            normalized.add(ROLE_CLF_ADMIN)

        # If a route allows new CLF_ADMIN, allow old CLF_MANAGER temporarily too.
        # This prevents existing old users from breaking until migration is complete.
        if role == ROLE_CLF_ADMIN:
            normalized.add(ROLE_CLF_MANAGER)

    return normalized


def _serialize_id(value):
    """
    Convert ObjectId/string/None into session-safe string.
    """
    if value in (None, "", [], {}):
        return None

    try:
        return str(value)
    except Exception:
        return None


def _serialize_id_list(values):
    """
    Convert assigned_pg_ids to list[str].
    """
    out = []

    for value in values or []:
        if value in (None, "", [], {}):
            continue
        out.append(str(value))

    return out


def _set_g_from_payload(payload):
    """
    Set flask.g auth context from session/JWT payload.
    """
    g.user_id = _serialize_id(payload.get("user_id"))
    g.role = _normalize_role(payload.get("role"))

    g.pg_id = _serialize_id(payload.get("pg_id"))
    g.clf_id = _serialize_id(payload.get("clf_id"))
    g.block_id = _serialize_id(payload.get("block_id"))
    g.district_id = _serialize_id(payload.get("district_id"))
    g.state_id = _serialize_id(payload.get("state_id"))

    g.validator_level = payload.get("validator_level")
    g.assigned_pg_ids = _serialize_id_list(payload.get("assigned_pg_ids") or [])

    return True


def _authenticate_request():
    """
    Hybrid authentication:
    1) Session authentication for web
    2) JWT token authentication for mobile/API

    Populates:
    - g.user_id
    - g.role
    - g.pg_id
    - g.clf_id
    - g.block_id
    - g.district_id
    - g.state_id
    - g.validator_level
    - g.assigned_pg_ids
    """

    # -----------------------------
    # 1) Web session authentication
    # -----------------------------
    if session.get("user_id"):
        payload = {
            "user_id": session.get("user_id"),
            "role": session.get("role"),
            "pg_id": session.get("pg_id"),
            "clf_id": session.get("clf_id"),
            "block_id": session.get("block_id"),
            "district_id": session.get("district_id"),
            "state_id": session.get("state_id"),
            "validator_level": session.get("validator_level"),
            "assigned_pg_ids": session.get("assigned_pg_ids") or [],
        }
        return _set_g_from_payload(payload)

    # -----------------------------
    # 2) JWT token authentication
    # -----------------------------
    auth_header = request.headers.get("Authorization", "")

    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

        if not token:
            return False

        try:
            payload = jwt.decode(
                token,
                current_app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )

            return _set_g_from_payload(payload)

        except jwt.ExpiredSignatureError:
            return False
        except jwt.InvalidTokenError:
            return False
        except Exception:
            return False

    return False


def _unauthenticated_response():
    """
    Standard unauthenticated response for web/API.
    """
    if _wants_json_response():
        return jsonify({"error": "Authentication required"}), 401

    flash("Please log in to access this page.", "warning")
    return redirect(url_for("auth.login", next=request.path))


def _forbidden_response():
    """
    Standard forbidden response for web/API.
    """
    if _wants_json_response():
        return jsonify({"error": "Forbidden"}), 403

    abort(403)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _authenticate_request():
            return view(*args, **kwargs)

        return _unauthenticated_response()

    return wrapped


def roles_required(*allowed_roles):
    """
    Role-based access decorator.

    Existing behavior preserved:
    - CADRE_CC can access PG_DATA_ENTRY routes where explicitly allowed.

    New behavior:
    - CLF_ADMIN is recognized.
    - CLF_MANAGER remains supported temporarily as legacy compatibility.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _authenticate_request():
                return _unauthenticated_response()

            role = _normalize_role(getattr(g, "role", None))
            effective_allowed = _normalize_allowed_roles(allowed_roles)

            # Existing compatibility:
            # Cadre CC can satisfy PG_DATA_ENTRY routes where the route allows PG_DATA_ENTRY.
            if role == ROLE_CADRE_CC and ROLE_PG_DATA_ENTRY in effective_allowed:
                effective_allowed.add(ROLE_CADRE_CC)

            # New CLF compatibility:
            # CLF_ADMIN replaces most CLF_MANAGER login usage.
            if role == ROLE_CLF_ADMIN and ROLE_CLF_MANAGER in effective_allowed:
                effective_allowed.add(ROLE_CLF_ADMIN)

            if role == ROLE_CLF_MANAGER and ROLE_CLF_ADMIN in effective_allowed:
                effective_allowed.add(ROLE_CLF_MANAGER)

            if role not in effective_allowed:
                return _forbidden_response()

            return view(*args, **kwargs)

        return wrapped
    return decorator


def permissions_required(*required_perms):
    """
    Permission decorator.

    Keeps current permissions.py logic untouched.
    """
    from .permissions import has_permission

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _authenticate_request():
                return _unauthenticated_response()

            role = _normalize_role(getattr(g, "role", None))

            for permission in required_perms:
                if not has_permission(role, permission):
                    return _forbidden_response()

            return view(*args, **kwargs)

        return wrapped
    return decorator


# ------------------------------------------------------------
# Scope helpers
# ------------------------------------------------------------

def current_user_context():
    """
    Return the authenticated user context from flask.g.

    Useful in routes/services where we need role scope.
    """
    if not getattr(g, "user_id", None):
        _authenticate_request()

    return {
        "user_id": getattr(g, "user_id", None),
        "role": getattr(g, "role", None),
        "state_id": getattr(g, "state_id", None),
        "district_id": getattr(g, "district_id", None),
        "block_id": getattr(g, "block_id", None),
        "clf_id": getattr(g, "clf_id", None),
        "pg_id": getattr(g, "pg_id", None),
        "validator_level": getattr(g, "validator_level", None),
        "assigned_pg_ids": getattr(g, "assigned_pg_ids", []) or [],
    }


def is_super_admin():
    return getattr(g, "role", None) == ROLE_SUPER_ADMIN


def is_state_admin():
    return getattr(g, "role", None) == ROLE_ADMIN


def is_district_admin():
    return getattr(g, "role", None) == ROLE_DISTRICT_ADMIN


def is_block_admin():
    return getattr(g, "role", None) == ROLE_BLOCK_ADMIN


def is_clf_admin():
    return getattr(g, "role", None) in {ROLE_CLF_ADMIN, ROLE_CLF_MANAGER}


def is_pg_login():
    return getattr(g, "role", None) == ROLE_PG_DATA_ENTRY


def is_cadre_login():
    return getattr(g, "role", None) == ROLE_CADRE_CC


def has_role(*roles):
    """
    Check current role against roles.

    Example:
        if has_role("BLOCK_ADMIN", "CLF_ADMIN"):
            ...
    """
    role = _normalize_role(getattr(g, "role", None))
    allowed = _normalize_allowed_roles(roles)
    return role in allowed


def get_scope_filter(collection_type="pg"):
    """
    Build a basic jurisdiction filter for common collection types.

    This does not replace route-specific checks, but gives a safe base filter.

    collection_type examples:
    - "pg" for pgs collection
    - "pg_child" for collections storing pg_id only
    - "geo" for collections storing state/district/block/clf directly

    For pg_child collections, if CLF_ADMIN is used and documents do not store clf_id,
    routes should resolve allowed PG IDs and add:
        {"pg_id": {"$in": allowed_pg_ids}}
    """
    ctx = current_user_context()
    role = ctx.get("role")

    if role in {ROLE_SUPER_ADMIN}:
        return {}

    if role == ROLE_ADMIN:
        if ctx.get("state_id"):
            return {"state_id": ctx["state_id"]}
        return {}

    if role == ROLE_DISTRICT_ADMIN:
        if ctx.get("district_id"):
            return {"district_id": ctx["district_id"]}
        return {"_id": {"$exists": False}}

    if role == ROLE_BLOCK_ADMIN:
        if ctx.get("block_id"):
            return {"block_id": ctx["block_id"]}
        return {"_id": {"$exists": False}}

    if role in {ROLE_CLF_ADMIN, ROLE_CLF_MANAGER}:
        if ctx.get("clf_id"):
            return {"clf_id": ctx["clf_id"]}
        return {"_id": {"$exists": False}}

    if role == ROLE_PG_DATA_ENTRY:
        if collection_type == "pg":
            if ctx.get("pg_id"):
                return {"_id": ctx["pg_id"]}
            return {"_id": {"$exists": False}}

        if ctx.get("pg_id"):
            return {"pg_id": ctx["pg_id"]}

        return {"_id": {"$exists": False}}

    if role == ROLE_CADRE_CC:
        assigned_pg_ids = ctx.get("assigned_pg_ids") or []

        if collection_type == "pg":
            return {"_id": {"$in": assigned_pg_ids}}

        return {"pg_id": {"$in": assigned_pg_ids}}

    return {"_id": {"$exists": False}}


def require_same_block(block_id):
    """
    Helper for route-level checks:
    Block Admin can access only own block.
    """
    ctx = current_user_context()
    role = ctx.get("role")

    if role in {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_DISTRICT_ADMIN}:
        return True

    if role == ROLE_BLOCK_ADMIN:
        return str(ctx.get("block_id")) == str(block_id)

    return False


def require_same_clf(clf_id):
    """
    Helper for CLF route-level checks:
    CLF Admin can access only own CLF.
    """
    ctx = current_user_context()
    role = ctx.get("role")

    if role in {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_DISTRICT_ADMIN, ROLE_BLOCK_ADMIN}:
        return True

    if role in {ROLE_CLF_ADMIN, ROLE_CLF_MANAGER}:
        return str(ctx.get("clf_id")) == str(clf_id)

    return False


def require_same_pg(pg_id):
    """
    Helper for PG route-level checks:
    PG login can access only own PG.
    Cadre can access assigned PGs.
    CLF scope should usually be checked by resolving pg.clf_id in route.
    """
    ctx = current_user_context()
    role = ctx.get("role")

    if role in {
        ROLE_SUPER_ADMIN,
        ROLE_ADMIN,
        ROLE_DISTRICT_ADMIN,
        ROLE_BLOCK_ADMIN,
        ROLE_CLF_ADMIN,
        ROLE_CLF_MANAGER,
    }:
        return True

    if role == ROLE_PG_DATA_ENTRY:
        return str(ctx.get("pg_id")) == str(pg_id)

    if role == ROLE_CADRE_CC:
        return str(pg_id) in [str(x) for x in (ctx.get("assigned_pg_ids") or [])]

    return False