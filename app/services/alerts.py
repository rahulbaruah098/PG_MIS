from datetime import datetime, timedelta
from bson import ObjectId

def compute_pg_alerts(db, pg_id: str):
    oid = ObjectId(pg_id) if ObjectId.is_valid(pg_id) else pg_id
    alerts = []
    now = datetime.utcnow()

    # Overdue loans (PG + member) based on next_due_date / overdue_amount stored
    for col, kind in (("pg_loan_accounts","PG"), ("pg_member_loan_accounts","Member")):
        for ln in db[col].find({"pg_id": oid, "status": {"$in": ["active","ongoing","npa"]}}):
            due = ln.get("next_due_date")
            overdue_amt = float(ln.get("overdue_amount") or 0)
            if due and isinstance(due, datetime) and due < now:
                alerts.append({
                    "type":"overdue_loan",
                    "level":"warning" if overdue_amt <= 0 else "danger",
                    "title": f"{kind} loan overdue",
                    "body": f"Loan {ln.get('loan_no','')} has an overdue instalment.",
                    "link": f"/finance/loan_account/{ln.get('_id')}",
                    "ts": now
                })

    # Low activity: no market transactions in last 30 days
    since = now - timedelta(days=30)
    tx = db.pg_market_transactions.find_one({"pg_id": oid, "updated_at": {"$gte": since}})
    if not tx:
        alerts.append({
            "type":"low_activity",
            "level":"info",
            "title":"Low activity",
            "body":"No market transactions updated in the last 30 days.",
            "link": f"/pg/view/{pg_id}",
            "ts": now
        })
    return alerts

def emit_alert_notifications(db, *, pg_id: str, to_role: str = "PG_DATA_ENTRY"):
    alerts = compute_pg_alerts(db, pg_id)
    for a in alerts:
        db.notifications.insert_one({
            "ts": a["ts"],
            "to_role": to_role,
            "title": a["title"],
            "body": a["body"],
            "link": a.get("link"),
            "level": a.get("level","info"),
            "meta": {"type": a.get("type"), "pg_id": pg_id},
            "is_read": False,
        })
    return len(alerts)
