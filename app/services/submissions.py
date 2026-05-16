from datetime import datetime
from bson import ObjectId
from flask import current_app, session


SUBMISSION_COLLECTION = "monthly_module_submissions"


def _now():
    return datetime.utcnow()


def _get_db(db=None):
    """
    Safely get MongoDB database object.

    Priority:
    1. db passed manually
    2. current_app.mongo_db used by this PG MIS project
    """
    if db is not None:
        return db

    app_db = getattr(current_app, "mongo_db", None)
    if app_db is None:
        raise RuntimeError("MongoDB database is not available on current_app.mongo_db")

    return app_db


def _safe_objectid(value):
    try:
        if isinstance(value, ObjectId):
            return value
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _clean_module(module):
    module = str(module or "").strip().lower()
    module = module.replace(" ", "_").replace("-", "_")
    return module or "unknown"


def _clean_level(level):
    level = str(level or "").strip().upper()
    aliases = {
        "CLF_MANAGER": "CLF",
        "CLF_ADMIN": "CLF",
        "BLOCK_ADMIN": "BLOCK",
        "DISTRICT_ADMIN": "DISTRICT",
        "STATE_ADMIN": "STATE",
        "ADMIN": "STATE",
        "SUPER_ADMIN": "STATE",
    }
    return aliases.get(level, level or "CLF")


def _current_user_id():
    return (
        session.get("user_id")
        or session.get("_id")
        or session.get("uid")
        or session.get("email")
        or session.get("username")
        or ""
    )


def _current_role():
    return session.get("role") or ""


def _submission_key(pg_id, year, month, module):
    pg_oid = _safe_objectid(pg_id)

    if not pg_oid:
        raise ValueError("Invalid PG id")

    return {
        "pg_id": pg_oid,
        "year": int(year),
        "month": int(month),
        "module": _clean_module(module),
    }


def _normalize_submission(doc):
    if not doc:
        return None

    doc["_id"] = str(doc.get("_id"))
    doc["pg_id"] = str(doc.get("pg_id"))

    for key in ("submitted_at", "created_at", "updated_at", "approved_at", "rejected_at"):
        if doc.get(key) and hasattr(doc.get(key), "isoformat"):
            doc[key] = doc[key].isoformat()

    approvals = doc.get("approvals") or {}
    for level, info in approvals.items():
        if isinstance(info, dict):
            if info.get("approved_at") and hasattr(info.get("approved_at"), "isoformat"):
                info["approved_at"] = info["approved_at"].isoformat()
            if info.get("rejected_at") and hasattr(info.get("rejected_at"), "isoformat"):
                info["rejected_at"] = info["rejected_at"].isoformat()

    return doc


def get_submission(pg_id, year, month, module, db=None):
    """
    Fetch one monthly module submission.

    Used by:
    - workflow/status route
    - frontend status checking
    """
    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)

    doc = db[SUBMISSION_COLLECTION].find_one(key)
    return _normalize_submission(doc)


def submit_to_clf(pg_id, year, month, module, submitted_by=None, db=None, remarks=""):
    """
    Submit a monthly module for approval.

    First approval level becomes CLF.
    Existing rejected draft can be resubmitted.
    """

    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)
    now = _now()

    submitted_by = submitted_by or _current_user_id()

    existing = db[SUBMISSION_COLLECTION].find_one(key)

    if existing and existing.get("status") in (
        "SUBMITTED",
        "CLF_APPROVED",
        "BLOCK_APPROVED",
        "DISTRICT_APPROVED",
        "STATE_APPROVED",
        "APPROVED",
    ):
        return {
            "ok": True,
            "already_submitted": True,
            "message": "This module has already been submitted.",
            "submission": _normalize_submission(existing),
        }

    payload = {
        **key,
        "status": "SUBMITTED",
        "current_level": "CLF",
        "submitted_by": submitted_by,
        "submitted_role": _current_role(),
        "submitted_at": now,
        "updated_at": now,
        "remarks": remarks or "",
        "rejection": None,
        "approvals": {},
    }

    if existing:
        db[SUBMISSION_COLLECTION].update_one(
            key,
            {
                "$set": payload
            }
        )
    else:
        payload["created_at"] = now
        db[SUBMISSION_COLLECTION].insert_one(payload)

    doc = db[SUBMISSION_COLLECTION].find_one(key)

    return {
        "ok": True,
        "already_submitted": False,
        "message": "Monthly module submitted to CLF successfully.",
        "submission": _normalize_submission(doc),
    }


def approve(pg_id, year, month, module, level, approved_by=None, db=None, remarks=""):
    """
    Approve monthly module submission at a level.

    Flow:
    SUBMITTED -> CLF_APPROVED -> BLOCK_APPROVED -> DISTRICT_APPROVED -> STATE_APPROVED / APPROVED
    """

    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)

    level = _clean_level(level)
    approved_by = approved_by or _current_user_id()
    now = _now()

    doc = db[SUBMISSION_COLLECTION].find_one(key)
    if not doc:
        raise ValueError("No submission found for this module and period.")

    if doc.get("status") in ("REJECTED",):
        raise ValueError("Rejected submission cannot be approved. Please resubmit first.")

    if doc.get("status") in ("STATE_APPROVED", "APPROVED"):
        return {
            "ok": True,
            "already_approved": True,
            "message": "This module is already fully approved.",
            "submission": _normalize_submission(doc),
        }

    allowed_flow = ["CLF", "BLOCK", "DISTRICT", "STATE"]

    if level not in allowed_flow:
        raise ValueError("Invalid approval level.")

    current_level = _clean_level(doc.get("current_level") or "CLF")

    if level != current_level:
        raise ValueError(f"This submission is currently pending at {current_level}, not {level}.")

    next_level_map = {
        "CLF": "BLOCK",
        "BLOCK": "DISTRICT",
        "DISTRICT": "STATE",
        "STATE": "COMPLETE",
    }

    status_map = {
        "CLF": "CLF_APPROVED",
        "BLOCK": "BLOCK_APPROVED",
        "DISTRICT": "DISTRICT_APPROVED",
        "STATE": "STATE_APPROVED",
    }

    next_level = next_level_map[level]
    new_status = status_map[level]

    set_data = {
        "status": "APPROVED" if next_level == "COMPLETE" else new_status,
        "current_level": next_level,
        "updated_at": now,
        f"approvals.{level}": {
            "approved": True,
            "approved_by": approved_by,
            "approved_role": _current_role(),
            "approved_at": now,
            "remarks": remarks or "",
        },
        "rejection": None,
    }

    if next_level == "COMPLETE":
        set_data["approved_at"] = now
        set_data["approved_by"] = approved_by

    db[SUBMISSION_COLLECTION].update_one(
        key,
        {
            "$set": set_data
        },
    )

    updated = db[SUBMISSION_COLLECTION].find_one(key)

    return {
        "ok": True,
        "already_approved": False,
        "message": f"Submission approved at {level} level.",
        "submission": _normalize_submission(updated),
    }


def reject(pg_id, year, month, module, level, reason="", rejected_by=None, db=None):
    """
    Reject monthly module submission at current approval level.
    """

    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)

    level = _clean_level(level)
    rejected_by = rejected_by or _current_user_id()
    now = _now()

    doc = db[SUBMISSION_COLLECTION].find_one(key)
    if not doc:
        raise ValueError("No submission found for this module and period.")

    if doc.get("status") in ("APPROVED", "STATE_APPROVED"):
        raise ValueError("Approved submission cannot be rejected.")

    current_level = _clean_level(doc.get("current_level") or "CLF")

    if level != current_level:
        raise ValueError(f"This submission is currently pending at {current_level}, not {level}.")

    db[SUBMISSION_COLLECTION].update_one(
        key,
        {
            "$set": {
                "status": "REJECTED",
                "current_level": level,
                "updated_at": now,
                "rejected_at": now,
                "rejected_by": rejected_by,
                "rejection": {
                    "level": level,
                    "reason": reason or "",
                    "rejected_by": rejected_by,
                    "rejected_role": _current_role(),
                    "rejected_at": now,
                },
                f"approvals.{level}": {
                    "approved": False,
                    "rejected": True,
                    "rejected_by": rejected_by,
                    "rejected_role": _current_role(),
                    "rejected_at": now,
                    "reason": reason or "",
                },
            }
        },
    )

    updated = db[SUBMISSION_COLLECTION].find_one(key)

    return {
        "ok": True,
        "message": f"Submission rejected at {level} level.",
        "submission": _normalize_submission(updated),
    }


def list_submissions(pg_id=None, year=None, month=None, module=None, status=None, current_level=None, db=None, limit=100):
    """
    Optional helper for future dashboard/listing use.
    Does not affect existing routes.
    """

    db = _get_db(db)

    query = {}

    if pg_id:
        pg_oid = _safe_objectid(pg_id)
        if pg_oid:
            query["pg_id"] = pg_oid

    if year:
        query["year"] = int(year)

    if month:
        query["month"] = int(month)

    if module:
        query["module"] = _clean_module(module)

    if status:
        query["status"] = str(status).strip().upper()

    if current_level:
        query["current_level"] = _clean_level(current_level)

    rows = list(
        db[SUBMISSION_COLLECTION]
        .find(query)
        .sort("updated_at", -1)
        .limit(int(limit or 100))
    )

    return [_normalize_submission(row) for row in rows]