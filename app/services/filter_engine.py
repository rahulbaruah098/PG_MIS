from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from flask import g, request, session
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
    assigned_pg_ids: Optional[List[str]] = None


class FilterEngine:
    """
    Universal filter + jurisdiction enforcement.

    Supports:
    - Web session scope
    - JWT/mobile scope from flask.g when rbac.login_required has populated it
    - PG_DATA_ENTRY restricted to own PG
    - CADRE_CC restricted to assigned PGs
    - CLF_ADMIN restricted to own CLF
    - BLOCK_ADMIN restricted to own Block
    - DISTRICT_ADMIN restricted to own District
    - ADMIN/STATE_ADMIN restricted to own State
    - SUPER_ADMIN unrestricted

    Important:
    - pg_match() is for the `pgs` collection.
      So pg_id is converted into `_id`.
    - pg_child_match() is for child collections storing `pg_id`.
      Example: pg_members, pg_grants, pg_cashbooks, pg_business_monthly.
    """

    # ------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------

    @staticmethod
    def _ctx_value(key: str, default=None):
        """
        Prefer flask.g values from JWT/session auth context, then session.
        """
        val = getattr(g, key, None)
        if val not in (None, "", [], {}):
            return val

        val = session.get(key)
        if val not in (None, "", [], {}):
            return val

        return default

    @staticmethod
    def _as_list(value) -> List[str]:
        if value in (None, "", {}, []):
            return []

        if isinstance(value, (list, tuple, set)):
            return [str(x) for x in value if x not in (None, "", [], {})]

        return [str(value)]

    @staticmethod
    def _normalize_role(role: Optional[str]) -> str:
        if not role:
            return ""

        role = str(role).strip().upper()

        # Keep CLF_MANAGER as a legacy role but do not convert it silently.
        # Several old routes still mention it, and rbac.py handles compatibility.
        return role

    @staticmethod
    def _oid(value):
        return safe_objectid(value)

    @staticmethod
    def _oid_list(values) -> List[Any]:
        out = []

        for value in values or []:
            oid = safe_objectid(value)
            if oid:
                out.append(oid)

        return out

    # ------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------

    @staticmethod
    def current_scope() -> Scope:
        assigned_pg_ids = (
            FilterEngine._ctx_value("assigned_pg_ids")
            or session.get("assigned_pg_ids")
            or []
        )

        return Scope(
            role=FilterEngine._normalize_role(FilterEngine._ctx_value("role", "")),
            state_id=FilterEngine._ctx_value("state_id"),
            district_id=FilterEngine._ctx_value("district_id"),
            block_id=FilterEngine._ctx_value("block_id"),
            clf_id=FilterEngine._ctx_value("clf_id"),
            pg_id=FilterEngine._ctx_value("pg_id") or FilterEngine._ctx_value("active_pg_id"),
            validator_level=FilterEngine._ctx_value("validator_level"),
            assigned_pg_ids=FilterEngine._as_list(assigned_pg_ids),
        )

    @staticmethod
    def _effective_role(scope: Scope) -> str:
        """
        Convert VALIDATOR role into effective jurisdiction role.
        """
        role = FilterEngine._normalize_role(scope.role)

        if role == "VALIDATOR":
            lvl = (scope.validator_level or "").strip().lower()

            if lvl == "state":
                return "ADMIN"
            if lvl == "district":
                return "DISTRICT_ADMIN"
            if lvl == "block":
                return "BLOCK_ADMIN"
            if lvl == "clf":
                return "CLF_ADMIN"

            return "ADMIN"

        return role

    @staticmethod
    def _enforce(scope: Scope, requested: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enforce role jurisdiction on request filters.

        Higher-level user choices are overwritten by their login scope.
        This prevents URL query tampering.
        """
        out = dict(requested or {})
        role = FilterEngine._effective_role(scope)

        # SUPER_ADMIN has no jurisdiction restriction.
        if role == "SUPER_ADMIN":
            return out

        # State admin / ADMIN is fixed to own state.
        if role in ("ADMIN", "STATE_ADMIN"):
            if scope.state_id:
                out["state_id"] = scope.state_id

            # State can still filter lower levels under state.
            return out

        # District admin is fixed to own district.
        if role == "DISTRICT_ADMIN":
            if scope.district_id:
                out["district_id"] = scope.district_id

            # Do not trust broader state filter from request.
            out.pop("state_id", None)
            return out

        # Block admin is fixed to own block.
        if role == "BLOCK_ADMIN":
            if scope.block_id:
                out["block_id"] = scope.block_id

            # Do not trust broader or child CLF filter unless explicitly selected.
            # Block can filter by a CLF under its block in route-specific code,
            # but the block scope itself must always remain enforced.
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # New CLF Admin is fixed to own CLF.
        if role in ("CLF_ADMIN", "CLF_MANAGER"):
            if scope.clf_id:
                out["clf_id"] = scope.clf_id

            # CLF must not widen scope to block/district/state.
            out.pop("block_id", None)
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # PG login is fixed to own PG.
        if role == "PG_DATA_ENTRY":
            if scope.pg_id:
                out["pg_id"] = scope.pg_id

            out.pop("clf_id", None)
            out.pop("block_id", None)
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # CADRE sees assigned PGs only.
        if role == "CADRE_CC":
            assigned = FilterEngine._as_list(scope.assigned_pg_ids)

            if scope.pg_id and str(scope.pg_id) in assigned:
                out["pg_id"] = scope.pg_id
                out.pop("assigned_pg_ids", None)
            else:
                out["assigned_pg_ids"] = assigned

            out.pop("clf_id", None)
            out.pop("block_id", None)
            out.pop("district_id", None)
            out.pop("state_id", None)
            return out

        # Unknown role: empty scope.
        out.clear()
        out["_deny_all"] = True
        return out

    # ------------------------------------------------------------
    # Request filters
    # ------------------------------------------------------------

    @staticmethod
    def read_filters() -> Dict[str, Any]:
        """
        Accept both *_id filters and simple name filters for reports.
        """
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
            "status": (args.get("status") or "").strip() or None,
            "form_type": (args.get("form_type") or "").strip() or None,
        }

        return {k: v for k, v in f.items() if v not in (None, "")}

    @staticmethod
    def enforced_filters() -> Dict[str, Any]:
        scope = FilterEngine.current_scope()
        return FilterEngine._enforce(scope, FilterEngine.read_filters())

    # ------------------------------------------------------------
    # Mongo match builders
    # ------------------------------------------------------------

    @staticmethod
    def pg_match(filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a Mongo match for the `pgs` collection.

        Important:
        - `pg_id` maps to `_id`, not `pg_id`.
        - String geo fields support both old imported names and newer form field variants.
        """
        filters = filters or {}

        if filters.get("_deny_all"):
            return {"_id": {"$exists": False}}

        m: Dict[str, Any] = {}

        # Scope ids stored on pgs
        for key in ("state_id", "district_id", "block_id", "clf_id"):
            if key in filters:
                oid = FilterEngine._oid(filters.get(key))
                if oid:
                    m[key] = oid

        # Direct PG id
        if "pg_id" in filters:
            oid = FilterEngine._oid(filters.get("pg_id"))
            if oid:
                m["_id"] = oid

        # Cadre assigned PGs
        if "assigned_pg_ids" in filters:
            pg_oids = FilterEngine._oid_list(filters.get("assigned_pg_ids"))
            m["_id"] = {"$in": pg_oids} if pg_oids else {"$exists": False}

        # Optional string filters
        if "gp" in filters:
            m["$or"] = m.get("$or", [])
            m["$or"].extend([
                {"gp": filters["gp"]},
                {"Gram Panchayat": filters["gp"]},
            ])

        if "village" in filters:
            # If $or already exists for gp, use $and so both conditions can apply.
            village_or = [
                {"village": filters["village"]},
                {"Village": filters["village"]},
            ]

            if "$or" in m:
                existing_or = m.pop("$or")
                m["$and"] = m.get("$and", [])
                m["$and"].append({"$or": existing_or})
                m["$and"].append({"$or": village_or})
            else:
                m["$or"] = village_or

        if "pg_name" in filters:
            m["name"] = {
                "$regex": filters["pg_name"],
                "$options": "i",
            }

        if "q" in filters:
            q = filters["q"]
            search_or = [
                {"name": {"$regex": q, "$options": "i"}},
                {"pg_name": {"$regex": q, "$options": "i"}},
                {"sector": {"$regex": q, "$options": "i"}},
                {"District": {"$regex": q, "$options": "i"}},
                {"Block": {"$regex": q, "$options": "i"}},
                {"Gram Panchayat": {"$regex": q, "$options": "i"}},
                {"Village": {"$regex": q, "$options": "i"}},
            ]

            if "$or" in m:
                existing_or = m.pop("$or")
                m["$and"] = m.get("$and", [])
                m["$and"].append({"$or": existing_or})
                m["$and"].append({"$or": search_or})
            else:
                m["$or"] = search_or

        return m

    @staticmethod
    def pg_child_match(filters: Dict[str, Any], pg_ids: Optional[List[Any]] = None) -> Dict[str, Any]:
        """
        Build a Mongo match for child collections storing `pg_id`.

        Use this for:
        - pg_members
        - pg_grants
        - pg_cashbooks
        - pg_business_monthly
        - pg_income_expenditure
        - pg_member_loan_accounts
        """
        filters = filters or {}

        if filters.get("_deny_all"):
            return {"pg_id": {"$in": []}}

        m: Dict[str, Any] = {}

        if pg_ids is not None:
            m["pg_id"] = {"$in": pg_ids or []}
            return m

        if "pg_id" in filters:
            oid = FilterEngine._oid(filters.get("pg_id"))
            m["pg_id"] = oid if oid else {"$in": []}
            return m

        if "assigned_pg_ids" in filters:
            pg_oids = FilterEngine._oid_list(filters.get("assigned_pg_ids"))
            m["pg_id"] = {"$in": pg_oids or []}
            return m

        return m

    @staticmethod
    def pg_id_oid(filters: Dict[str, Any]) -> Optional[Any]:
        if "pg_id" not in (filters or {}):
            return None
        return safe_objectid(filters["pg_id"])

    @staticmethod
    def apply_period(match: Dict[str, Any], field: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add period range into a Mongo match.
        """
        start, end = FilterEngine.period_range(filters)

        if start and end:
            match[field] = {"$gte": start, "$lt": end}
        elif start:
            match[field] = {"$gte": start}
        elif end:
            match[field] = {"$lt": end}

        return match

    # ------------------------------------------------------------
    # Period handling
    # ------------------------------------------------------------

    @staticmethod
    def period_range(filters: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Return (start, end) UTC datetimes for supported period filters.

        Supports:
        - period=weekly|monthly|six_monthly|yearly
        - from=YYYY-MM-DD, to=YYYY-MM-DD

        End is exclusive for Mongo $lt.
        """
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
