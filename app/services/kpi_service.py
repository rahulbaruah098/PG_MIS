from typing import Any, Dict, List, Optional
from datetime import datetime
from bson import ObjectId
from app.utils import safe_objectid


class KPIService:
    """Centralized KPI computations (DB-aware).

    Keeps existing workflows intact by providing read-only KPI helpers.
    Updated for CLF_ADMIN scope/surveillance support.
    """

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return float(default)
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _oid(value: Any):
        try:
            if isinstance(value, ObjectId):
                return value
            return safe_objectid(value)
        except Exception:
            return None

    @staticmethod
    def _pg_query_for_scope(
        db,
        *,
        role: str = "",
        state_id: Optional[str] = None,
        district_id: Optional[str] = None,
        block_id: Optional[str] = None,
        clf_id: Optional[str] = None,
        assigned_pg_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return a safe PG query for hierarchy/CLF dashboards.

        Role behavior:
        - SUPER_ADMIN: all PGs
        - ADMIN/STATE_ADMIN: state scoped
        - DISTRICT_ADMIN: district scoped
        - BLOCK_ADMIN: block scoped
        - CLF_ADMIN/CLF_MANAGER: only mapped CLF / assigned PGs
        - PG_DATA_ENTRY: caller should use pg_metrics directly
        """

        role = (role or "").upper()
        q: Dict[str, Any] = {}

        if role in ("SUPER_ADMIN",):
            return q

        if role in ("ADMIN", "STATE_ADMIN"):
            sid = KPIService._oid(state_id)
            if sid:
                q["state_id"] = sid
            return q

        if role == "DISTRICT_ADMIN":
            did = KPIService._oid(district_id)
            if did:
                q["district_id"] = did
            return q

        if role == "BLOCK_ADMIN":
            bid = KPIService._oid(block_id)
            if bid:
                q["block_id"] = bid
            return q

        if role in ("CLF_ADMIN", "CLF_MANAGER"):
            clf_oid = KPIService._oid(clf_id)
            assigned_oids = [
                KPIService._oid(x)
                for x in (assigned_pg_ids or [])
                if KPIService._oid(x)
            ]

            ors = []
            if clf_oid:
                ors.append({"clf_id": clf_oid})
                ors.append({"clf_id": str(clf_oid)})

            if assigned_oids:
                ors.append({"_id": {"$in": assigned_oids}})

            if ors:
                q["$or"] = ors
            else:
                q["_id"] = {"$in": []}

            return q

        return q

    @staticmethod
    def _pg_ids_for_scope(
        db,
        *,
        role: str = "",
        state_id: Optional[str] = None,
        district_id: Optional[str] = None,
        block_id: Optional[str] = None,
        clf_id: Optional[str] = None,
        assigned_pg_ids: Optional[List[str]] = None,
    ) -> List[ObjectId]:
        q = KPIService._pg_query_for_scope(
            db,
            role=role,
            state_id=state_id,
            district_id=district_id,
            block_id=block_id,
            clf_id=clf_id,
            assigned_pg_ids=assigned_pg_ids,
        )
        return [p["_id"] for p in db.pgs.find(q, {"_id": 1})]

    @staticmethod
    def _cashflow_for_pg_ids(db, pg_ids: List[ObjectId]) -> Dict[str, float]:
        if not pg_ids:
            return {
                "grant_received": 0.0,
                "grant_utilized": 0.0,
                "loan_principal": 0.0,
                "loan_repaid": 0.0,
                "member_loan_principal": 0.0,
                "member_loan_repaid": 0.0,
                "cash_in": 0.0,
                "cash_out": 0.0,
                "balance": 0.0,
            }

        grant_received = 0.0
        grant_utilized = 0.0
        loan_principal = 0.0
        loan_repaid = 0.0
        member_loan_principal = 0.0
        member_loan_repaid = 0.0

        for g in db.pg_grants.find(
            {"pg_id": {"$in": pg_ids}},
            {"amount": 1, "received_amount": 1, "total_amount": 1, "utilized_amount": 1, "total_utilized": 1},
        ):
            grant_received += KPIService._num(
                g.get("amount", g.get("received_amount", g.get("total_amount", 0)))
            )
            grant_utilized += KPIService._num(
                g.get("utilized_amount", g.get("total_utilized", 0))
            )

        # Utilization collection can hold additional/approved utilization rows.
        for u in db.pg_grant_utilizations.find(
            {"pg_id": {"$in": pg_ids}},
            {"amount": 1, "utilized_amount": 1, "status": 1},
        ):
            status = str(u.get("status") or "").lower()
            if status in ("rejected", "cancelled"):
                continue
            grant_utilized += KPIService._num(u.get("amount", u.get("utilized_amount", 0)))

        for ln in db.pg_loan_accounts.find(
            {"pg_id": {"$in": pg_ids}},
            {"principal": 1, "principal_amount": 1, "sanctioned_amount": 1, "amount": 1, "total_repaid": 1, "principal_repaid": 1},
        ):
            loan_principal += KPIService._num(
                ln.get("principal", ln.get("principal_amount", ln.get("sanctioned_amount", ln.get("amount", 0))))
            )
            loan_repaid += KPIService._num(ln.get("total_repaid", ln.get("principal_repaid", 0)))

        for ln in db.pg_member_loan_accounts.find(
            {"pg_id": {"$in": pg_ids}},
            {"principal": 1, "principal_amount": 1, "sanctioned_amount": 1, "amount": 1, "total_repaid": 1, "principal_repaid": 1},
        ):
            member_loan_principal += KPIService._num(
                ln.get("principal", ln.get("principal_amount", ln.get("sanctioned_amount", ln.get("amount", 0))))
            )
            member_loan_repaid += KPIService._num(ln.get("total_repaid", ln.get("principal_repaid", 0)))

        cash_in = grant_received + loan_principal + member_loan_principal
        cash_out = grant_utilized + loan_repaid + member_loan_repaid

        return {
            "grant_received": round(grant_received, 2),
            "grant_utilized": round(grant_utilized, 2),
            "loan_principal": round(loan_principal, 2),
            "loan_repaid": round(loan_repaid, 2),
            "member_loan_principal": round(member_loan_principal, 2),
            "member_loan_repaid": round(member_loan_repaid, 2),
            "cash_in": round(cash_in, 2),
            "cash_out": round(cash_out, 2),
            "balance": round(cash_in - cash_out, 2),
        }

    @staticmethod
    def pg_metrics(db, pg_id: str) -> Dict[str, Any]:
        oid = safe_objectid(pg_id)
        if not oid:
            return {
                "members_count": 0,
                "lakhpati_count": 0,
                "grants_count": 0,
                "grants_total": 0,
                "turnover": 0,
                "profit": 0,
                "loss": 0,
                "cashflow": KPIService._cashflow_for_pg_ids(db, []),
            }

        members_count = db.pg_members.count_documents({"pg_id": oid})
        lakhpati_count = db.pg_members.count_documents({"pg_id": oid, "lakh_pati_didi": True})

        # Grants
        grants_count = db.pg_grants.count_documents({"pg_id": oid})
        grants_total = 0.0
        for g in db.pg_grants.find({"pg_id": oid}, {"amount": 1, "received_amount": 1, "total_amount": 1}):
            try:
                grants_total += float(g.get("amount", g.get("received_amount", g.get("total_amount", 0))) or 0)
            except Exception:
                pass

        # Turnover & Profit/Loss (best-effort from monthly business + income_expenditure)
        turnover = 0.0

        for m in db.pg_business_monthly.find(
            {"pg_id": oid},
            {"turnover": 1, "total_turnover": 1, "internal_total": 1, "total_internal": 1},
        ):
            val = m.get("turnover")
            if val is None:
                val = m.get("total_turnover")
            if val is None:
                val = m.get("internal_total")
            if val is None:
                val = m.get("total_internal")
            try:
                turnover += float(val or 0)
            except Exception:
                pass

        # Market transaction collection often stores 6.2/6.4 values.
        for m in db.pg_market_transactions.find(
            {"pg_id": oid},
            {"market_total": 1, "total_turnover": 1, "total_market": 1},
        ):
            val = m.get("market_total")
            if val is None:
                val = m.get("total_market")
            if val is None:
                val = m.get("total_turnover")
            try:
                turnover += float(val or 0)
            except Exception:
                pass

        profit = 0.0
        loss = 0.0

        for ie in db.pg_income_expenditure.find(
            {"pg_id": oid},
            {
                "income": 1,
                "expenditure": 1,
                "profit": 1,
                "loss": 1,
                "profit_or_loss": 1,
                "total_income": 1,
                "total_expenditure": 1,
                "excess_income_over_expenditure": 1,
            },
        ):
            if "profit" in ie or "loss" in ie:
                try:
                    profit += float(ie.get("profit") or 0)
                except Exception:
                    pass
                try:
                    loss += float(ie.get("loss") or 0)
                except Exception:
                    pass
                continue

            try:
                if "excess_income_over_expenditure" in ie:
                    pl = float(ie.get("excess_income_over_expenditure") or 0)
                else:
                    inc_raw = ie.get("total_income", ie.get("income", 0))
                    exp_raw = ie.get("total_expenditure", ie.get("expenditure", 0))

                    if isinstance(inc_raw, dict):
                        inc = sum(KPIService._num(v) for v in inc_raw.values())
                    else:
                        inc = KPIService._num(inc_raw)

                    if isinstance(exp_raw, dict):
                        exp = sum(KPIService._num(v) for v in exp_raw.values())
                    else:
                        exp = KPIService._num(exp_raw)

                    pl = inc - exp

                if pl >= 0:
                    profit += pl
                else:
                    loss += abs(pl)
            except Exception:
                pass

        return {
            "members_count": int(members_count or 0),
            "lakhpati_count": int(lakhpati_count or 0),
            "grants_count": int(grants_count or 0),
            "grants_total": round(grants_total, 2),
            "turnover": round(turnover, 2),
            "profit": round(profit, 2),
            "loss": round(loss, 2),
            "cashflow": KPIService._cashflow_for_pg_ids(db, [oid]),
        }

    @staticmethod
    def clf_metrics(db, clf_id: str, assigned_pg_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """CLF scoped summary for CLF_ADMIN dashboard/reporting."""

        pg_ids = KPIService._pg_ids_for_scope(
            db,
            role="CLF_ADMIN",
            clf_id=clf_id,
            assigned_pg_ids=assigned_pg_ids or [],
        )

        members_count = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}}) if pg_ids else 0
        cashflow = KPIService._cashflow_for_pg_ids(db, pg_ids)

        return {
            "pg_count": len(pg_ids),
            "members_count": int(members_count or 0),
            "cashflow": cashflow,
        }

    @staticmethod
    def hierarchy_metrics(
        db,
        *,
        role: str = "",
        state_id: Optional[str] = None,
        district_id: Optional[str] = None,
        block_id: Optional[str] = None,
        clf_id: Optional[str] = None,
        assigned_pg_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Read-only scoped summary for state/district/block/CLF surveillance pages."""

        role = (role or "").upper()
        pg_ids = KPIService._pg_ids_for_scope(
            db,
            role=role,
            state_id=state_id,
            district_id=district_id,
            block_id=block_id,
            clf_id=clf_id,
            assigned_pg_ids=assigned_pg_ids or [],
        )

        members_count = db.pg_members.count_documents({"pg_id": {"$in": pg_ids}}) if pg_ids else 0
        cashflow = KPIService._cashflow_for_pg_ids(db, pg_ids)

        clf_query: Dict[str, Any] = {}
        if role == "BLOCK_ADMIN":
            bid = KPIService._oid(block_id)
            if bid:
                clf_query["block_id"] = bid
        elif role == "DISTRICT_ADMIN":
            did = KPIService._oid(district_id)
            if did:
                block_ids = [b["_id"] for b in db.blocks.find({"district_id": did}, {"_id": 1})]
                clf_query["block_id"] = {"$in": block_ids}
        elif role in ("ADMIN", "STATE_ADMIN"):
            sid = KPIService._oid(state_id)
            if sid:
                district_ids = [d["_id"] for d in db.districts.find({"state_id": sid}, {"_id": 1})]
                block_ids = [b["_id"] for b in db.blocks.find({"district_id": {"$in": district_ids}}, {"_id": 1})]
                clf_query["block_id"] = {"$in": block_ids}
        elif role in ("CLF_ADMIN", "CLF_MANAGER"):
            cid = KPIService._oid(clf_id)
            if cid:
                clf_query["_id"] = cid

        clf_count = db.clfs.count_documents(clf_query) if clf_query or role in ("SUPER_ADMIN",) else 0

        return {
            "pg_count": len(pg_ids),
            "members_count": int(members_count or 0),
            "clf_count": int(clf_count or 0),
            "cashflow": cashflow,
        }
