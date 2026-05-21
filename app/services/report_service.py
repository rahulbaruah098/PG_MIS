import csv
import io
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Response, session, current_app

from app.utils import safe_objectid
from app.services.filter_engine import FilterEngine


class ReportService:
    """Centralized report generation helpers.

    - Standardizes filters + jurisdiction enforcement via FilterEngine
    - Provides CSV and ZIP report builders without altering routes
    - Adds CLF_ADMIN-safe report scoping helpers
    """

    @staticmethod
    def enforced_filters() -> Dict[str, Any]:
        return FilterEngine.enforced_filters()

    @staticmethod
    def _role() -> str:
        return (session.get("role") or "").upper()

    @staticmethod
    def _oid(value: Any):
        try:
            return safe_objectid(value)
        except Exception:
            return None

    @staticmethod
    def _object_id_list(values: Optional[Iterable[Any]]) -> List[Any]:
        out = []
        for value in values or []:
            oid = ReportService._oid(value)
            if oid:
                out.append(oid)
        return out

    @staticmethod
    def scoped_pg_query(extra_query: Optional[Dict[str, Any]] = None, db=None) -> Dict[str, Any]:
        """Return PG query with current role jurisdiction enforced.

        Scope rules:
        - SUPER_ADMIN: all PGs
        - ADMIN / STATE_ADMIN: own state
        - DISTRICT_ADMIN: own district
        - BLOCK_ADMIN: own block
        - CLF_ADMIN / CLF_MANAGER: mapped CLF or assigned PGs only
        - PG_DATA_ENTRY: own PG only
        - CADRE_CC: assigned PGs or active PG only
        """

        db = db or getattr(current_app, "mongo_db", None)
        role = ReportService._role()

        query: Dict[str, Any] = {}
        extra_query = extra_query or {}

        if role in ("SUPER_ADMIN",):
            query = {}

        elif role in ("ADMIN", "STATE_ADMIN"):
            sid = ReportService._oid(session.get("state_id"))
            query = {"state_id": sid} if sid else {"_id": {"$in": []}}

        elif role == "DISTRICT_ADMIN":
            did = ReportService._oid(session.get("district_id"))
            query = {"district_id": did} if did else {"_id": {"$in": []}}

        elif role == "BLOCK_ADMIN":
            bid = ReportService._oid(session.get("block_id"))
            query = {"block_id": bid} if bid else {"_id": {"$in": []}}

        elif role in ("CLF_ADMIN", "CLF_MANAGER"):
            clf_oid = ReportService._oid(session.get("clf_id"))
            assigned_pg_ids = ReportService._object_id_list(session.get("assigned_pg_ids") or [])

            ors: List[Dict[str, Any]] = []

            if clf_oid:
                ors.append({"clf_id": clf_oid})
                ors.append({"clf_id": str(clf_oid)})

            if assigned_pg_ids:
                ors.append({"_id": {"$in": assigned_pg_ids}})

            query = {"$or": ors} if ors else {"_id": {"$in": []}}

        elif role == "PG_DATA_ENTRY":
            pg_oid = ReportService._oid(session.get("pg_id") or session.get("active_pg_id"))
            query = {"_id": pg_oid} if pg_oid else {"_id": {"$in": []}}

        elif role == "CADRE_CC":
            active_pg_oid = ReportService._oid(session.get("active_pg_id") or session.get("pg_id"))
            assigned_pg_ids = ReportService._object_id_list(session.get("assigned_pg_ids") or [])

            if active_pg_oid:
                query = {"_id": active_pg_oid}
            elif assigned_pg_ids:
                query = {"_id": {"$in": assigned_pg_ids}}
            else:
                query = {"_id": {"$in": []}}

        else:
            query = {"_id": {"$in": []}}

        if extra_query:
            if "$or" in query:
                return {"$and": [query, extra_query]}
            query.update(extra_query)

        return query

    @staticmethod
    def scoped_pg_ids(extra_query: Optional[Dict[str, Any]] = None, db=None) -> List[Any]:
        """Return current user's allowed PG ids."""

        db = db or getattr(current_app, "mongo_db", None)
        if db is None:
            return []

        query = ReportService.scoped_pg_query(extra_query=extra_query, db=db)
        return [row["_id"] for row in db.pgs.find(query, {"_id": 1})]

    @staticmethod
    def enforce_pg_id(pg_id: Any, db=None) -> bool:
        """True only if the requested PG is inside the current user's report scope."""

        oid = ReportService._oid(pg_id)
        if not oid:
            return False

        db = db or getattr(current_app, "mongo_db", None)
        if db is None:
            return False

        query = ReportService.scoped_pg_query({"_id": oid}, db=db)
        return db.pgs.count_documents(query, limit=1) > 0

    @staticmethod
    def scoped_collection_query(
        pg_field: str = "pg_id",
        extra_query: Optional[Dict[str, Any]] = None,
        db=None,
    ) -> Dict[str, Any]:
        """Build a collection query using scoped PG ids.

        Use this for report exports from collections like:
        - pg_members
        - pg_grants
        - pg_income_expenditure
        - pg_business_monthly
        - pg_market_transactions
        - pg_loan_accounts
        - pg_member_loan_accounts
        """

        pg_ids = ReportService.scoped_pg_ids(db=db)
        base: Dict[str, Any] = {pg_field: {"$in": pg_ids}}

        if extra_query:
            base.update(extra_query)

        return base

    @staticmethod
    def scoped_clf_query(extra_query: Optional[Dict[str, Any]] = None, db=None) -> Dict[str, Any]:
        """Return CLF query with hierarchy scope enforced.

        Block sees CLFs under own block.
        District sees CLFs under blocks in own district.
        State/Admin sees CLFs under own state when state scope exists.
        CLF Admin sees own CLF only.
        """

        db = db or getattr(current_app, "mongo_db", None)
        role = ReportService._role()
        query: Dict[str, Any] = {}

        if role == "SUPER_ADMIN":
            query = {}

        elif role in ("ADMIN", "STATE_ADMIN"):
            sid = ReportService._oid(session.get("state_id"))
            if db is not None and sid:
                district_ids = [d["_id"] for d in db.districts.find({"state_id": sid}, {"_id": 1})]
                block_ids = [b["_id"] for b in db.blocks.find({"district_id": {"$in": district_ids}}, {"_id": 1})]
                query = {"block_id": {"$in": block_ids}}
            else:
                query = {"_id": {"$in": []}}

        elif role == "DISTRICT_ADMIN":
            did = ReportService._oid(session.get("district_id"))
            if db is not None and did:
                block_ids = [b["_id"] for b in db.blocks.find({"district_id": did}, {"_id": 1})]
                query = {"block_id": {"$in": block_ids}}
            else:
                query = {"_id": {"$in": []}}

        elif role == "BLOCK_ADMIN":
            bid = ReportService._oid(session.get("block_id"))
            query = {"block_id": bid} if bid else {"_id": {"$in": []}}

        elif role in ("CLF_ADMIN", "CLF_MANAGER"):
            cid = ReportService._oid(session.get("clf_id"))
            query = {"_id": cid} if cid else {"_id": {"$in": []}}

        else:
            query = {"_id": {"$in": []}}

        if extra_query:
            query.update(extra_query)

        return query

    @staticmethod
    def _clean_csv_value(value: Any) -> Any:
        if value is None:
            return ""

        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                pass

        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(v) for v in value)

        if isinstance(value, dict):
            return str(value)

        return value

    @staticmethod
    def csv_response(filename: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> Response:
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()

        for r in rows:
            w.writerow({
                k: ReportService._clean_csv_value(r.get(k))
                for k in fieldnames
            })

        data = out.getvalue().encode("utf-8-sig")
        return Response(
            data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @staticmethod
    def zip_csv_response(filename: str, files: List[Tuple[str, List[Dict[str, Any]], List[str]]]) -> Response:
        mem = io.BytesIO()

        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            for csv_name, rows, fields in files:
                out = io.StringIO()
                w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
                w.writeheader()

                for r in rows:
                    w.writerow({
                        k: ReportService._clean_csv_value(r.get(k))
                        for k in fields
                    })

                z.writestr(csv_name, out.getvalue().encode("utf-8-sig"))

        mem.seek(0)
        return Response(
            mem.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
