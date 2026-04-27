"""Guards / invariants enforcement.

This module is intentionally small and dependency-light so it can be used
from any blueprint without creating import cycles.
"""

from __future__ import annotations

from functools import wraps
from flask import request, session, flash, redirect, url_for, current_app

from app.services.periods import is_period_locked


def _get_period_from_request():
    """Try to infer (year, month) from common form/args keys."""
    # Common keys used across pages
    year = request.form.get("year") or request.args.get("year")
    month = request.form.get("month") or request.args.get("month")
    # Alternative keys
    year = year or request.form.get("fy_year") or request.args.get("fy_year")
    month = month or request.form.get("report_month") or request.args.get("report_month")
    # Normalize
    try:
        year = int(year) if year is not None and str(year).strip() != "" else None
    except Exception:
        year = None
    try:
        month = int(month) if month is not None and str(month).strip() != "" else None
    except Exception:
        month = None
    return year, month


def require_unlocked_period(scope: str = "pg", pg_id_param: str = "pg_id", redirect_endpoint: str | None = None):
    """Decorator: blocks POST/PUT/PATCH/DELETE writes if the period is locked.

    - scope: currently only 'pg' is used by your existing period lock service.
    - pg_id_param: name of kwarg carrying pg_id (route variable).
    - redirect_endpoint: optional endpoint to redirect on block.
    """

    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                pg_id = kwargs.get(pg_id_param) or session.get("pg_id") or request.form.get("pg_id")
                year, month = _get_period_from_request()
                # is_period_locked requires a db handle (see app/services/periods.py).
                db = getattr(current_app, "mongo_db", None)
                if (db is not None) and pg_id and (year is not None) and (month is not None) and is_period_locked(db, scope=scope, ref_id=str(pg_id), year=year, month=month):
                    flash("This period is locked (approved). Editing is disabled.", "warning")
                    if redirect_endpoint:
                        return redirect(url_for(redirect_endpoint, pg_id=pg_id))
                    # fall back
                    return redirect(request.referrer or url_for("reports.hierarchy_dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return deco
