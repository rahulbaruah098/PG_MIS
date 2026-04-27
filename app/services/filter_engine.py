from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from datetime import datetime, timedelta
from flask import request, session
from app.utils import safe_objectid

@dataclass
class Scope:
    role: str
    state_id: Optional[str] = None
    district_id: Optional[str] = None
    block_id: Optional[str] = None
    clf_id: Optional[str] = None
    pg_id: Optional[str] = None
    validator_level: Optional[str] = None

class FilterEngine:
    """Universal filter + jurisdiction enforcement.

    - Reads raw filters from request.args
    - Enforces jurisdiction based on session role + assigned ids
    - Produces Mongo-friendly query snippets for PG scoped collections
    """

    @staticmethod
    def current_scope() -> Scope:
        role = session.get("role") or ""
        return Scope(
            role=role,
            state_id=session.get("state_id"),
            district_id=session.get("district_id"),
            block_id=session.get("block_id"),
            clf_id=session.get("clf_id"),
            pg_id=session.get("pg_id"),
            validator_level=session.get("validator_level"),
        )

    @staticmethod
    def _enforce(scope: Scope, requested: Dict[str, Any]) -> Dict[str, Any]:
        # Normalize requested ids
        out = dict(requested)

        # Validator behaves like its level
        role = scope.role
        if role == "VALIDATOR":
            lvl = (scope.validator_level or "").lower()
            if lvl == "district":
                role = "DISTRICT_ADMIN"
            elif lvl == "block":
                role = "BLOCK_ADMIN"
            else:
                role = "STATE_ADMIN"  # treated like state scope

        # PG scope
        if role == "PG_DATA_ENTRY":
            if scope.pg_id:
                out["pg_id"] = scope.pg_id
            out.pop("clf_id", None)
            out.pop("block_id", None)
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # Block/CLF scope
        if role in ("BLOCK_ADMIN", "CLF_MANAGER"):
            if scope.clf_id:
                out["clf_id"] = scope.clf_id
            if scope.block_id:
                out["block_id"] = scope.block_id
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # District scope
        if role == "DISTRICT_ADMIN":
            if scope.district_id:
                out["district_id"] = scope.district_id
            out.pop("state_id", None)
            return out

        # State scope: if we ever add it
        if role == "STATE_ADMIN":
            if scope.state_id:
                out["state_id"] = scope.state_id
            return out

        # Super/Admin: no enforcement
        return out

    @staticmethod
    def read_filters() -> Dict[str, Any]:
        # Accept both *_id and simple names for gp/village/pg name
        args = request.args
        f = {
            "state_id": args.get("state_id") or None,
            "district_id": args.get("district_id") or None,
            "block_id": args.get("block_id") or None,
            "clf_id": args.get("clf_id") or None,
            "pg_id": args.get("pg_id") or None,
            "gp": (args.get("gp") or "").strip() or None,
            "village": (args.get("village") or "").strip() or None,
            "pg_name": (args.get("pg_name") or "").strip() or None,
            "q": (args.get("q") or "").strip() or None,
            "period": (args.get("period") or "").strip() or None,
            "from": (args.get("from") or "").strip() or None,
            "to": (args.get("to") or "").strip() or None,
            "group_by": (args.get("group_by") or "").strip() or None,
            "mode": (args.get("mode") or "").strip() or None,
        }
        # drop None keys
        return {k: v for k, v in f.items() if v not in (None, "")}

    @staticmethod
    def enforced_filters() -> Dict[str, Any]:
        scope = FilterEngine.current_scope()
        return FilterEngine._enforce(scope, FilterEngine.read_filters())

    @staticmethod
    def pg_match(filters: Dict[str, Any]) -> Dict[str, Any]:
        """Build a Mongo match for the `pgs` collection."""
        m: Dict[str, Any] = {}
        for key in ("state_id","district_id","block_id","clf_id","pg_id"):
            if key in filters:
                oid = safe_objectid(filters[key])
                if oid:
                    m[key] = oid
        # Optional string filters (best-effort; only apply if field exists)
        if "gp" in filters:
            m["gp"] = filters["gp"]
        if "village" in filters:
            m["village"] = filters["village"]
        if "pg_name" in filters:
            # case-insensitive contains
            m["name"] = {"$regex": filters["pg_name"], "$options": "i"}
        return m

    @staticmethod
    def pg_id_oid(filters: Dict[str, Any]) -> Optional[Any]:
        if "pg_id" not in filters:
            return None
        return safe_objectid(filters["pg_id"])

    @staticmethod
    def period_range(filters: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Return (start, end) UTC datetimes for supported period filters.

        Supports:
        - period=weekly|monthly|six_monthly|yearly
        - from=YYYY-MM-DD, to=YYYY-MM-DD (overrides period)

        End is exclusive (safe for Mongo $lt).
        """
        # Explicit range overrides
        f = (filters.get("from") or "").strip() if isinstance(filters, dict) else ""
        t = (filters.get("to") or "").strip() if isinstance(filters, dict) else ""
        try:
            if f and t:
                start = datetime.strptime(f, "%Y-%m-%d")
                end = datetime.strptime(t, "%Y-%m-%d") + timedelta(days=1)
                return start, end
            if f and not t:
                start = datetime.strptime(f, "%Y-%m-%d")
                return start, None
            if t and not f:
                end = datetime.strptime(t, "%Y-%m-%d") + timedelta(days=1)
                return None, end
        except Exception:
            pass

        p = (filters.get("period") or "").strip().lower() if isinstance(filters, dict) else ""
        if not p:
            return None, None

        now = datetime.utcnow()
        if p == "weekly":
            return now - timedelta(days=7), now + timedelta(seconds=1)
        if p == "monthly":
            return now - timedelta(days=30), now + timedelta(seconds=1)
        if p in ("six_monthly", "6_monthly", "six-monthly", "6-monthly"):
            return now - timedelta(days=183), now + timedelta(seconds=1)
        if p == "yearly":
            return now - timedelta(days=365), now + timedelta(seconds=1)
        return None, None
