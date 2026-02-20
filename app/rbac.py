from functools import wraps
from flask import session, redirect, url_for, flash, abort, request

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in.", "warning")
                return redirect(url_for("auth.login"))
            role = session.get("role")
            if role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator

def permissions_required(*required_perms):
    """Require that the logged-in user's role grants ALL requested permissions."""
    from .permissions import has_permission
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in.", "warning")
                return redirect(url_for("auth.login"))
            role = session.get("role")
            for p in required_perms:
                if not has_permission(role, p):
                    abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
