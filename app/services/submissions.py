"""Monthly submission workflow (PG → CLF → Block → District) and locking.

This is designed to be additive to your existing system:
- It does NOT remove existing change_requests / validators.
- It provides a simple, explicit chain for month-based forms.
"""

from __future__ import annotations

from datetime import datetime
from flask import session

from app.db import mongo


def _coll():
    return mongo.db.submissions


def get_submission(pg_id: str, year: int, month: int, module: str):
    return _coll().find_one({"pg_id": str(pg_id), "year": int(year), "month": int(month), "module": module})


def upsert_submission(pg_id: str, year: int, month: int, module: str, patch: dict):
    patch = dict(patch or {})
    patch["updated_at"] = datetime.utcnow()
    _coll().update_one(
        {"pg_id": str(pg_id), "year": int(year), "month": int(month), "module": module},
        {"$set": patch, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return get_submission(pg_id, year, month, module)


CHAIN = ["pg", "clf", "block", "district"]


def submit_to_clf(pg_id: str, year: int, month: int, module: str, note: str = ""):
    return upsert_submission(
        pg_id,
        year,
        month,
        module,
        {
            "status": "submitted_to_clf",
            "submitted_by": session.get("user_id"),
            "submitted_role": session.get("role"),
            "note": note,
            "history": [{"at": datetime.utcnow(), "by": session.get("user_id"), "action": "submit_to_clf"}],
        },
    )


def approve(pg_id: str, year: int, month: int, module: str, level: str, remark: str = ""):
    """Approve at a specific level: 'clf'|'block'|'district'."""
    level = level.lower().strip()
    if level not in ("clf", "block", "district"):
        raise ValueError("Invalid level")
    if level == "clf":
        status = "approved_by_clf"
    elif level == "block":
        status = "approved_by_block"
    else:
        status = "approved_by_district"
    doc = get_submission(pg_id, year, month, module) or {}
    hist = list(doc.get("history") or [])
    hist.append({"at": datetime.utcnow(), "by": session.get("user_id"), "action": f"approve_{level}", "remark": remark})
    return upsert_submission(
        pg_id,
        year,
        month,
        module,
        {
            "status": status,
            f"approved_by_{level}": session.get("user_id"),
            f"approved_at_{level}": datetime.utcnow(),
            f"remark_{level}": remark,
            "history": hist,
        },
    )


def reject(pg_id: str, year: int, month: int, module: str, level: str, remark: str = ""):
    level = level.lower().strip()
    if level not in ("clf", "block", "district"):
        raise ValueError("Invalid level")
    doc = get_submission(pg_id, year, month, module) or {}
    hist = list(doc.get("history") or [])
    hist.append({"at": datetime.utcnow(), "by": session.get("user_id"), "action": f"reject_{level}", "remark": remark})
    return upsert_submission(
        pg_id,
        year,
        month,
        module,
        {
            "status": f"rejected_by_{level}",
            f"rejected_by_{level}": session.get("user_id"),
            f"rejected_at_{level}": datetime.utcnow(),
            f"remark_{level}": remark,
            "history": hist,
        },
    )
