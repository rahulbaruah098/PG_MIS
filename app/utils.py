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
