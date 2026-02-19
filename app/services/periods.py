from datetime import datetime
from bson import ObjectId

LOCK_SCOPE = {"pg", "clf", "block", "district", "state"}

def _ref_obj(ref_id):
    if isinstance(ref_id, str) and ObjectId.is_valid(ref_id):
        return ObjectId(ref_id)
    return ref_id

def is_period_locked(db, *, scope: str, ref_id, year: int, month: int) -> bool:
    scope = (scope or "").lower()
    if scope not in LOCK_SCOPE:
        scope = "pg"
    doc = db.period_locks.find_one({
        "scope": scope,
        "ref_id": _ref_obj(ref_id),
        "year": int(year),
        "month": int(month),
        "is_locked": True
    })
    return bool(doc)

def lock_period(db, *, scope: str, ref_id, year: int, month: int, user: dict, note: str = None):
    scope = (scope or "").lower()
    if scope not in LOCK_SCOPE:
        scope = "pg"
    doc = {
        "scope": scope,
        "ref_id": _ref_obj(ref_id),
        "year": int(year),
        "month": int(month),
        "is_locked": True,
        "note": note,
        "locked_at": datetime.utcnow(),
        "locked_by": user,
        "updated_at": datetime.utcnow(),
    }
    db.period_locks.update_one(
        {"scope": doc["scope"], "ref_id": doc["ref_id"], "year": doc["year"], "month": doc["month"]},
        {"$set": doc},
        upsert=True
    )
    return doc

def unlock_period(db, *, scope: str, ref_id, year: int, month: int, user: dict, note: str = None):
    scope = (scope or "").lower()
    if scope not in LOCK_SCOPE:
        scope = "pg"
    db.period_locks.update_one(
        {"scope": scope, "ref_id": _ref_obj(ref_id), "year": int(year), "month": int(month)},
        {"$set": {"is_locked": False, "unlocked_at": datetime.utcnow(), "unlocked_by": user, "note": note, "updated_at": datetime.utcnow()}},
        upsert=True
    )
