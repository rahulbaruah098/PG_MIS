from datetime import datetime
from bson import ObjectId
from flask import current_app, session


SUBMISSION_COLLECTION = "monthly_module_submissions"

# Monthly module approval flow:
# PG_DATA_ENTRY submits monthly module -> CLF_ADMIN/CLF_MANAGER approves -> BLOCK_ADMIN -> DISTRICT_ADMIN -> ADMIN/SUPER_ADMIN.
#
# This service is only for monthly module workflow submission/approval.
# PG Registration / Membership Registration validation is handled separately in app/pg/routes.py
# using validation metadata on the PG/member records, so both workflows remain independent.

MONTHLY_LEVELS = ["CLF", "BLOCK", "DISTRICT", "STATE"]

PENDING_STATUSES = {
    "SUBMITTED",
    "CLF_APPROVED",
    "BLOCK_APPROVED",
    "DISTRICT_APPROVED",
}

FINAL_APPROVED_STATUSES = {
    "STATE_APPROVED",
    "APPROVED",
}

REJECTED_STATUSES = {
    "REJECTED",
}


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
    """
    Normalize all legacy and new role names into the monthly approval levels.

    New CLF role:
    - CLF_ADMIN now owns the CLF-level monthly approval step.
    - CLF_MANAGER remains supported for older users/data.
    """
    level = str(level or "").strip().upper()

    aliases = {
        "CLF": "CLF",
        "CLF_MANAGER": "CLF",
        "CLF_ADMIN": "CLF",

        "BLOCK": "BLOCK",
        "BLOCK_ADMIN": "BLOCK",

        "DISTRICT": "DISTRICT",
        "DISTRICT_ADMIN": "DISTRICT",

        "STATE": "STATE",
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


def _current_level():
    return _clean_level(_current_role())


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

    for key in (
        "submitted_at",
        "created_at",
        "updated_at",
        "approved_at",
        "rejected_at",
        "resubmitted_at",
    ):
        if doc.get(key) and hasattr(doc.get(key), "isoformat"):
            doc[key] = doc[key].isoformat()

    approvals = doc.get("approvals") or {}
    for level, info in approvals.items():
        if isinstance(info, dict):
            if info.get("approved_at") and hasattr(info.get("approved_at"), "isoformat"):
                info["approved_at"] = info["approved_at"].isoformat()
            if info.get("rejected_at") and hasattr(info.get("rejected_at"), "isoformat"):
                info["rejected_at"] = info["rejected_at"].isoformat()

    rejection = doc.get("rejection")
    if isinstance(rejection, dict):
        if rejection.get("rejected_at") and hasattr(rejection.get("rejected_at"), "isoformat"):
            rejection["rejected_at"] = rejection["rejected_at"].isoformat()

    return doc


def _pg_scope_snapshot(db, pg_id):
    """
    Store a lightweight geo/CLF snapshot inside monthly submissions.
    This allows dashboards/queues to filter without expensive joins later.
    """
    pg_oid = _safe_objectid(pg_id)
    if not pg_oid:
        return {}

    pg = db.pgs.find_one(
        {"_id": pg_oid},
        {
            "state_id": 1,
            "district_id": 1,
            "block_id": 1,
            "clf_id": 1,
            "assigned_clf_user_id": 1,
            "name": 1,
            "pg_name": 1,
        },
    ) or {}

    return {
        "state_id": pg.get("state_id"),
        "district_id": pg.get("district_id"),
        "block_id": pg.get("block_id"),
        "clf_id": pg.get("clf_id"),
        "assigned_clf_user_id": pg.get("assigned_clf_user_id"),
        "pg_name": pg.get("name") or pg.get("pg_name") or "",
    }


def _can_role_act_on_level(role, level):
    """
    Permission check for monthly submission approvals.

    This does not replace route-level RBAC; it protects this service if called
    from any route/API later.
    """
    role_level = _clean_level(role)
    level = _clean_level(level)
    return role_level == level


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
    Existing rejected/draft submission can be resubmitted.

    Important:
    This is the monthly workflow only. It is separate from PG Registration
    and Membership Registration validation, which are handled in app/pg/routes.py.
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

    scope_snapshot = _pg_scope_snapshot(db, pg_id)

    payload = {
        **key,
        **scope_snapshot,
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
        payload["resubmitted_at"] = now
        db[SUBMISSION_COLLECTION].update_one(
            key,
            {"$set": payload}
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


def approve(pg_id, year, month, module, level=None, approved_by=None, db=None, remarks=""):
    """
    Approve monthly module submission at a level.

    Flow:
    SUBMITTED -> CLF_APPROVED -> BLOCK_APPROVED -> DISTRICT_APPROVED -> APPROVED

    CLF_ADMIN and legacy CLF_MANAGER both approve at CLF level.
    """

    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)

    level = _clean_level(level or _current_level())
    approved_by = approved_by or _current_user_id()
    now = _now()

    if level not in MONTHLY_LEVELS:
        raise ValueError("Invalid approval level.")

    if not _can_role_act_on_level(_current_role(), level):
        raise ValueError(f"Your role is not allowed to approve at {level} level.")

    doc = db[SUBMISSION_COLLECTION].find_one(key)
    if not doc:
        raise ValueError("No submission found for this module and period.")

    if doc.get("status") in REJECTED_STATUSES:
        raise ValueError("Rejected submission cannot be approved. Please resubmit first.")

    if doc.get("status") in FINAL_APPROVED_STATUSES:
        return {
            "ok": True,
            "already_approved": True,
            "message": "This module is already fully approved.",
            "submission": _normalize_submission(doc),
        }

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
        {"$set": set_data},
    )

    updated = db[SUBMISSION_COLLECTION].find_one(key)

    return {
        "ok": True,
        "already_approved": False,
        "message": f"Submission approved at {level} level.",
        "submission": _normalize_submission(updated),
    }


def reject(pg_id, year, month, module, level=None, reason="", rejected_by=None, db=None):
    """
    Reject monthly module submission at current approval level.

    Rejection sends the same monthly module back for PG correction/resubmission.
    """

    db = _get_db(db)
    key = _submission_key(pg_id, year, month, module)

    level = _clean_level(level or _current_level())
    rejected_by = rejected_by or _current_user_id()
    now = _now()

    if level not in MONTHLY_LEVELS:
        raise ValueError("Invalid approval level.")

    if not _can_role_act_on_level(_current_role(), level):
        raise ValueError(f"Your role is not allowed to reject at {level} level.")

    doc = db[SUBMISSION_COLLECTION].find_one(key)
    if not doc:
        raise ValueError("No submission found for this module and period.")

    if doc.get("status") in FINAL_APPROVED_STATUSES:
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


def list_submissions(
    pg_id=None,
    year=None,
    month=None,
    module=None,
    status=None,
    current_level=None,
    db=None,
    limit=100,
    state_id=None,
    district_id=None,
    block_id=None,
    clf_id=None,
    assigned_clf_user_id=None,
):
    """
    Optional helper for dashboard/listing use.

    Supports the new CLF_ADMIN scope fields stored during submission:
    - state_id
    - district_id
    - block_id
    - clf_id
    - assigned_clf_user_id
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
        if isinstance(status, (list, tuple, set)):
            query["status"] = {"$in": [str(s).strip().upper() for s in status if str(s).strip()]}
        else:
            query["status"] = str(status).strip().upper()

    if current_level:
        query["current_level"] = _clean_level(current_level)

    scoped_ids = {
        "state_id": state_id,
        "district_id": district_id,
        "block_id": block_id,
        "clf_id": clf_id,
        "assigned_clf_user_id": assigned_clf_user_id,
    }

    for key, value in scoped_ids.items():
        oid = _safe_objectid(value)
        if oid:
            query[key] = oid

    rows = list(
        db[SUBMISSION_COLLECTION]
        .find(query)
        .sort("updated_at", -1)
        .limit(int(limit or 100))
    )

    return [_normalize_submission(row) for row in rows]


def list_pending_for_current_user(year=None, month=None, module=None, db=None, limit=100):
    """
    Convenience helper for approval queues.

    Role behavior:
    - CLF_ADMIN/CLF_MANAGER: current_level=CLF, filtered by session clf_id/user assignment when available
    - BLOCK_ADMIN: current_level=BLOCK, filtered by session block_id
    - DISTRICT_ADMIN: current_level=DISTRICT, filtered by session district_id
    - ADMIN/SUPER_ADMIN: current_level=STATE, optionally state filtered if state_id exists
    """

    role = _current_role()
    level = _current_level()

    kwargs = {
        "year": year,
        "month": month,
        "module": module,
        "current_level": level,
        "status": list(PENDING_STATUSES),
        "db": db,
        "limit": limit,
    }

    if role in ("CLF_ADMIN", "CLF_MANAGER"):
        if session.get("clf_id"):
            kwargs["clf_id"] = session.get("clf_id")
        elif session.get("user_id"):
            kwargs["assigned_clf_user_id"] = session.get("user_id")

    elif role == "BLOCK_ADMIN":
        kwargs["block_id"] = session.get("block_id")

    elif role == "DISTRICT_ADMIN":
        kwargs["district_id"] = session.get("district_id")

    elif role == "ADMIN":
        kwargs["state_id"] = session.get("state_id")

    return list_submissions(**kwargs)


def status_summary(pg_id=None, year=None, month=None, db=None):
    """
    Return lightweight monthly workflow counts for dashboards.
    """

    db = _get_db(db)

    query = {}

    pg_oid = _safe_objectid(pg_id)
    if pg_oid:
        query["pg_id"] = pg_oid

    if year:
        query["year"] = int(year)

    if month:
        query["month"] = int(month)

    pipeline = [
        {"$match": query},
        {
            "$group": {
                "_id": {
                    "status": "$status",
                    "current_level": "$current_level",
                },
                "count": {"$sum": 1},
            }
        },
    ]

    result = {
        "total": 0,
        "submitted": 0,
        "clf_pending": 0,
        "block_pending": 0,
        "district_pending": 0,
        "state_pending": 0,
        "approved": 0,
        "rejected": 0,
        "by_status": {},
    }

    for row in db[SUBMISSION_COLLECTION].aggregate(pipeline):
        status = (row.get("_id") or {}).get("status") or "UNKNOWN"
        level = (row.get("_id") or {}).get("current_level") or ""
        count = int(row.get("count") or 0)

        result["total"] += count
        result["by_status"][status] = result["by_status"].get(status, 0) + count

        if status == "SUBMITTED" and level == "CLF":
            result["submitted"] += count
            result["clf_pending"] += count
        elif level == "BLOCK":
            result["block_pending"] += count
        elif level == "DISTRICT":
            result["district_pending"] += count
        elif level == "STATE":
            result["state_pending"] += count
        elif status in FINAL_APPROVED_STATUSES:
            result["approved"] += count
        elif status in REJECTED_STATUSES:
            result["rejected"] += count

    return result
