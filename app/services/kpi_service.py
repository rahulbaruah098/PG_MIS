from typing import Any, Dict, Optional
from datetime import datetime
from app.utils import safe_objectid

class KPIService:
    """Centralized KPI computations (DB-aware).

    Keeps existing workflows intact by providing *read-only* KPI helpers.
    """

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
            }

        members_count = db.pg_members.count_documents({"pg_id": oid})
        lakhpati_count = db.pg_members.count_documents({"pg_id": oid, "lakh_pati_didi": True})

        # Grants
        grants_count = db.pg_grants.count_documents({"pg_id": oid})
        grants_total = 0.0
        for g in db.pg_grants.find({"pg_id": oid}, {"amount": 1}):
            try:
                grants_total += float(g.get("amount") or 0)
            except Exception:
                pass

        # Turnover & Profit/Loss (best-effort from monthly business + income_expenditure)
        turnover = 0.0
        for m in db.pg_business_monthly.find({"pg_id": oid}, {"turnover": 1, "total_turnover": 1}):
            val = m.get("turnover")
            if val is None:
                val = m.get("total_turnover")
            try:
                turnover += float(val or 0)
            except Exception:
                pass

        profit = 0.0
        loss = 0.0
        # We use income_expenditure if present: income, expenditure, profit_or_loss
        for ie in db.pg_income_expenditure.find({"pg_id": oid}, {"income": 1, "expenditure": 1, "profit": 1, "loss": 1, "profit_or_loss": 1}):
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
            # fallback: income - expenditure
            try:
                inc = float(ie.get("income") or 0)
                exp = float(ie.get("expenditure") or 0)
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
        }
