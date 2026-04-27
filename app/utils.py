from werkzeug.security import generate_password_hash, check_password_hash

try:
    # Only available when using MongoDB/BSON (this app does).
    from bson import ObjectId
except Exception:  # pragma: no cover
    ObjectId = None  # type: ignore

def hash_password(password: str) -> str:
    return generate_password_hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def json_safe(value):
    """Convert common Mongo/BSON values to JSON/session-safe primitives.

    Flask's default secure cookie session serializer uses JSON, so values like
    bson.ObjectId cannot be stored directly in the session.
    """
    if ObjectId is not None and isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value

def safe_objectid(value):
    """Safely convert a value into bson.ObjectId.

    Returns ObjectId on success, otherwise None.
    Accepts ObjectId, 24-hex strings, and ignores None/'None'/''.
    """
    if ObjectId is None:
        return None
    if value is None:
        return None
    # Sometimes values come from session as 'None'
    if isinstance(value, str):
        v = value.strip()
        if not v or v.lower() == "none":
            return None
        try:
            return ObjectId(v)
        except Exception:
            return None
    try:
        # already an ObjectId?
        if isinstance(value, ObjectId):
            return value
    except Exception:
        pass
    # last resort: try casting to str
    try:
        return ObjectId(str(value))
    except Exception:
        return None
