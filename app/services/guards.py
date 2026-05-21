"""Guards / invariants enforcement.

This module is intentionally small and dependency-light so it can be used
from any blueprint without creating import cycles.
"""

from __future__ import annotations

from functools import wraps

from flask import (
    request,
    session,
    flash,
    redirect,
    url_for,
    current_app,
    jsonify,
)

from app.services.periods import is_period_locked


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _wants_json() -> bool:
    """Return True when the request should receive JSON instead of redirect/flash."""
    accept = (request.headers.get("Accept") or "").lower()
    content_type = (request.content_type or "").lower()
    requested_with = (request.headers.get("X-Requested-With") or "").lower()

    return (
        request.is_json
        or "application/json" in accept
        or "application/json" in content_type
        or requested_with == "xmlhttprequest"
        or request.args.get("format") == "json"
    )


def _get_request_value(*keys):
    """Read a value from JSON, form, args, or route-compatible request values."""
    json_data = request.get_json(silent=True) or {} if request.is_json else {}

    for key in keys:
        if key in json_data and json_data.get(key) not in (None, ""):
            return json_data.get(key)

        value = request.form.get(key)
        if value not in (None, ""):
            return value

        value = request.args.get(key)
        if value not in (None, ""):
            return value

    return None


def _get_period_from_request():
    """Try to infer (year, month) from common JSON/form/query keys."""
    year = _get_request_value("year", "fy_year", "report_year")
    month = _get_request_value("month", "report_month", "period_month")

    try:
        year = int(year) if year is not None and str(year).strip() != "" else None
    except Exception:
        year = None

    try:
        month = int(month) if month is not None and str(month).strip() != "" else None
    except Exception:
        month = None

    return year, month


def _get_pg_id_from_request(kwargs, pg_id_param: str):
    """Resolve PG id from route kwargs, session, JSON/form/query payload, or active PG context."""
    pg_id = (
        kwargs.get(pg_id_param)
        or kwargs.get("pg_id")
        or session.get("active_pg_id")
        or session.get("pg_id")
        or _get_request_value("pg_id", "active_pg_id")
    )

    return str(pg_id) if pg_id else None


def _redirect_back_or_default(pg_id: str | None, redirect_endpoint: str | None):
    if redirect_endpoint:
        values = {}
        if pg_id:
            values["pg_id"] = pg_id
        try:
            return redirect(url_for(redirect_endpoint, **values))
        except Exception:
            pass

    return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))


def require_unlocked_period(
    scope: str = "pg",
    pg_id_param: str = "pg_id",
    redirect_endpoint: str | None = None,
):
    """Block writes if a PG period is locked.

    This guard is role-neutral. It works for PG_DATA_ENTRY, CLF_ADMIN,
    legacy CLF_MANAGER, BLOCK_ADMIN, and higher roles by checking the same
    PG-period lock before any write method is allowed through.

    Important behavior:
    - GET requests are always allowed.
    - POST/PUT/PATCH/DELETE are checked only when pg_id + year + month are available.
    - JSON/API requests receive a JSON 423 response instead of redirecting.
    - Web requests receive flash + redirect.
    """

    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method not in WRITE_METHODS:
                return view(*args, **kwargs)

            pg_id = _get_pg_id_from_request(kwargs, pg_id_param)
            year, month = _get_period_from_request()
            db = getattr(current_app, "mongo_db", None)

            should_check_lock = (
                db is not None
                and pg_id
                and year is not None
                and month is not None
            )

            if should_check_lock and is_period_locked(
                db,
                scope=scope,
                ref_id=str(pg_id),
                year=year,
                month=month,
            ):
                message = "This period is locked/approved. Editing is disabled."

                if _wants_json():
                    return jsonify({
                        "success": False,
                        "error": "period_locked",
                        "message": message,
                        "scope": scope,
                        "pg_id": str(pg_id),
                        "year": year,
                        "month": month,
                    }), 423

                flash(message, "warning")
                return _redirect_back_or_default(pg_id, redirect_endpoint)

            return view(*args, **kwargs)

        return wrapped

    return deco
