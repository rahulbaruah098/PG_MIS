from services.audit_engine import AuditLogger
import os
from flask import render_template, send_from_directory, request, redirect, url_for, flash, current_app, session, jsonify, g
from app.services.guards import require_unlocked_period
from bson import ObjectId
from datetime import datetime
import jwt

from . import finance_bp
from ..rbac import login_required, roles_required

# ============================================================
# CLF / Block / PG finance scope helpers
# ============================================================

def _auth_context_dicts():
    """
    Collect authenticated user context from:
    - Flask session/web
    - flask.g if rbac.py stores user there
    - Authorization Bearer JWT from mobile app
    """
    contexts = []

    jwt_payload = _jwt_payload_from_authorization_header()
    if isinstance(jwt_payload, dict) and jwt_payload:
        contexts.append(jwt_payload)

    # rbac._authenticate_request stores JWT/session identity as scalar values
    # on flask.g. Keep those values available to the finance scope helpers too.
    g_context = {
        key: getattr(g, key, None)
        for key in (
            "user_id", "role", "pg_id", "active_pg_id", "clf_id",
            "block_id", "district_id", "state_id", "validator_level",
            "assigned_pg_ids",
        )
    }
    if any(value not in (None, "", []) for value in g_context.values()):
        contexts.append(g_context)

    for obj_name in (
        "current_user",
        "user",
        "auth_user",
        "jwt_user",
        "token_user",
        "decoded_token",
        "jwt_payload",
    ):
        value = getattr(g, obj_name, None)
        if isinstance(value, dict):
            contexts.append(value)

    for obj_name in (
        "current_user",
        "user",
        "auth_user",
        "jwt_user",
        "token_user",
        "decoded_token",
        "jwt_payload",
    ):
        value = getattr(request, obj_name, None)
        if isinstance(value, dict):
            contexts.append(value)

    return contexts

def _current_user_doc():
    """
    Resolve authenticated user for both:
    - web session
    - mobile JWT/Bearer token
    """
    try:
        db = current_app.mongo_db
    except Exception:
        return None

    contexts = _auth_context_dicts()

    possible_ids = [
        session.get("user_id"),
        session.get("_user_id"),
        session.get("uid"),
        session.get("_id"),
        session.get("id"),
    ]

    for ctx in contexts:
        possible_ids.extend([
            ctx.get("user_id"),
            ctx.get("_id"),
            ctx.get("id"),
            ctx.get("sub"),
            ctx.get("uid"),
        ])

    for raw_id in possible_ids:
        oid = _to_object_id(raw_id)
        if oid:
            user = db.users.find_one({"_id": oid})
            if user:
                return user

    possible_usernames = [session.get("username")]
    possible_emails = [session.get("email")]

    for ctx in contexts:
        possible_usernames.extend([
            ctx.get("username"),
            ctx.get("user_name"),
            ctx.get("name"),
        ])
        possible_emails.extend([
            ctx.get("email"),
            ctx.get("mail"),
        ])

    for username in possible_usernames:
        if username:
            user = db.users.find_one({"username": username})
            if user:
                return user

    for email in possible_emails:
        if email:
            user = db.users.find_one({"email": email})
            if user:
                return user

    for ctx in contexts:
        if ctx:
            return ctx

    return None

def _session_or_user_value(key, user=None):
    """
    Prefer Flask session.
    Then check mobile/JWT context.
    Then check loaded user document.
    """
    value = session.get(key)
    if value not in (None, "", []):
        return value

    for ctx in _auth_context_dicts():
        value = ctx.get(key)
        if value not in (None, "", []):
            return value

    if user is None:
        user = _current_user_doc()

    if isinstance(user, dict):
        value = user.get(key)
        if value not in (None, "", []):
            return value

    return None

def _role():
    user = _current_user_doc()

    for value in (
        session.get("role"),
        session.get("user_role"),
        _session_or_user_value("role", user),
        _session_or_user_value("user_role", user),
    ):
        if value:
            return str(value).strip().upper()

    return ""


def _is_json_request():
    return (
        request.args.get("format") == "json"
        or "application/json" in (request.headers.get("Accept", "").lower())
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.is_json
    )


def _to_object_id(value):
    try:
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None

def _jwt_payload_from_authorization_header():
    """
    Mobile app sends Authorization: Bearer <token>.
    Finance routes must decode it because Flask session is empty for app calls.
    """
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return {}

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return {}

    secret_candidates = [
        current_app.config.get("JWT_SECRET_KEY"),
        current_app.config.get("JWT_SECRET"),
        current_app.config.get("SECRET_KEY"),
        os.getenv("JWT_SECRET_KEY"),
        os.getenv("JWT_SECRET"),
        os.getenv("SECRET_KEY"),
    ]

    for secret in secret_candidates:
        if not secret:
            continue

        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            return payload if isinstance(payload, dict) else {}
        except jwt.ExpiredSignatureError:
            return {}
        except Exception:
            pass

    # Fallback:
    # login_required/roles_required already allowed this request to reach here,
    # so use unverified payload only to recover user_id/role/pg scope.
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def _assigned_pg_identifier_set():
    """
    Returns all assigned PG identifiers as strings.

    Important:
    Mobile/user records may store assigned PG as Mongo ObjectId OR as PG code
    like PG001. So do not drop non-ObjectId values.
    """
    user = _current_user_doc()

    raw_values = []

    for key in ("assigned_pg_ids", "assigned_pgs", "pg_ids"):
        value = session.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value not in (None, ""):
            raw_values.append(value)

    if user:
        for key in ("assigned_pg_ids", "assigned_pgs", "pg_ids"):
            value = user.get(key)
            if isinstance(value, list):
                raw_values.extend(value)
            elif value not in (None, ""):
                raw_values.append(value)

    out = set()

    for raw in raw_values:
        if raw in (None, ""):
            continue

        raw_str = str(raw).strip()
        if not raw_str:
            continue

        out.add(raw_str)

        oid = _to_object_id(raw_str)
        if oid:
            out.add(str(oid))

    return out

def _pg_allowed_for_current_user(pg):
    """
    Finance jurisdiction guard.

    Supports:
    - Web Flask session
    - Mobile JWT/Bearer auth context
    - PG id stored as ObjectId
    - PG code/name stored as PG001
    """
    if not pg:
        return False

    user = _current_user_doc()
    role = _role()

    pg_identifiers = {
        str(pg.get("_id") or "").strip(),
        str(pg.get("pg_id") or "").strip(),
        str(pg.get("pgId") or "").strip(),
        str(pg.get("active_pg_id") or "").strip(),
        str(pg.get("pg_code") or "").strip(),
        str(pg.get("code") or "").strip(),
        str(pg.get("producer_group_code") or "").strip(),
        str(pg.get("pg_name") or "").strip(),
        str(pg.get("name") or "").strip(),
    }
    pg_identifiers = {x for x in pg_identifiers if x}

    def same_id(a, b):
        if a in (None, "") or b in (None, ""):
            return False

        a = str(a).strip()
        b = str(b).strip()

        if not a or not b:
            return False

        if a == b:
            return True

        a_oid = _to_object_id(a)
        b_oid = _to_object_id(b)

        return bool(a_oid and b_oid and str(a_oid) == str(b_oid))

    def matches_pg(value):
        if value in (None, ""):
            return False

        value_str = str(value).strip()
        if not value_str:
            return False

        if value_str in pg_identifiers:
            return True

        return any(same_id(value_str, pg_value) for pg_value in pg_identifiers)

    if role == "SUPER_ADMIN":
        return True

    if role in {"ADMIN", "STATE_ADMIN"}:
        state_id = _session_or_user_value("state_id", user)
        return bool(state_id and same_id(pg.get("state_id"), state_id))

    if role == "DISTRICT_ADMIN":
        district_id = _session_or_user_value("district_id", user)
        return bool(district_id and same_id(pg.get("district_id"), district_id))

    if role == "BLOCK_ADMIN":
        block_id = _session_or_user_value("block_id", user)
        return bool(block_id and any(
            same_id(pg.get(field), block_id)
            for field in ("block_id", "blockId", "mapped_block_id", "assigned_block_id")
        ))

    if role in {"CLF_ADMIN", "CLF_MANAGER"}:
        clf_id = _session_or_user_value("clf_id", user)

        if clf_id and any(
            same_id(pg.get(field), clf_id)
            for field in ("clf_id", "CLF_id", "clfId", "mapped_clf_id", "assigned_clf_id")
        ):
            return True

        # A PG can be mapped directly to a CLF login in addition to (or before)
        # receiving a CLF master id. This is the mapping written by the CLF-PG
        # assignment screen and must grant the same read access.
        current_user_id = (
            _session_or_user_value("user_id", user)
            or session.get("_user_id")
            or session.get("uid")
            or (user.get("_id") if isinstance(user, dict) else None)
        )
        if current_user_id and any(
            same_id(pg.get(field), current_user_id)
            for field in ("assigned_clf_user_id", "clf_user_id", "assigned_user_id")
        ):
            return True

        assigned_pg_ids = _assigned_pg_identifier_set()
        return bool(pg_identifiers.intersection(assigned_pg_ids))

    if role == "PG_DATA_ENTRY":
        possible_pg_values = [
            _session_or_user_value("active_pg_id", user),
            _session_or_user_value("pg_id", user),
            _session_or_user_value("pgId", user),
            _session_or_user_value("pg_code", user),
            _session_or_user_value("active_pg_code", user),
            _session_or_user_value("producer_group_code", user),
        ]

        return any(matches_pg(v) for v in possible_pg_values)

    if role == "CADRE_CC":
        assigned_pg_ids = _assigned_pg_identifier_set()
        return bool(pg_identifiers.intersection(assigned_pg_ids))

    return False

def _deny(message, is_json=None, status_code=403, redirect_endpoint="pg.pg_home"):
    if is_json is None:
        is_json = _is_json_request()

    if is_json:
        return jsonify({"ok": False, "message": message}), status_code

    flash(message, "danger" if status_code == 403 else "warning")
    return redirect(url_for(redirect_endpoint))

def _require_pg_access(pg, is_json=None):
    if not _pg_allowed_for_current_user(pg):
        return _deny("This PG is outside your finance scope.", is_json=is_json, status_code=403)
    return None

def _can_write_finance():
    """
    Finance write permission.

    New workflow:
    - CLF_ADMIN/CLF_MANAGER handles finance operations for assigned/mapped PGs.
    - BLOCK_ADMIN becomes surveillance/view-only for finance data entry.
    - PG_DATA_ENTRY remains view-only for authority-created finance records.
    - Higher admins are kept enabled for administrative correction/support.
    """
    return _role() in {"CLF_ADMIN", "CLF_MANAGER", "DISTRICT_ADMIN", "ADMIN", "STATE_ADMIN", "SUPER_ADMIN"}


def _require_finance_write(message=None, is_json=None):
    if _can_write_finance():
        return None

    message = message or "View only: finance entry and updates are handled by CLF/Admin authorities."
    return _deny(message, is_json=is_json, status_code=403)


def _pg_from_id_or_response(db, pg_id, is_json=None):
    try:
        pg_obj_id = ObjectId(str(pg_id))
    except Exception:
        return None, _deny("Invalid PG ID.", is_json=is_json, status_code=400)

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        return None, _deny("PG not found.", is_json=is_json, status_code=404)

    denied = _require_pg_access(pg, is_json=is_json)
    if denied:
        return None, denied

    return pg, None


def _pg_from_grant_or_response(db, grant, is_json=None):
    pg = db.pgs.find_one({"_id": grant.get("pg_id")}) if grant and grant.get("pg_id") else None
    if not pg:
        return None, _deny("Mapped PG not found for this grant.", is_json=is_json, status_code=404)

    denied = _require_pg_access(pg, is_json=is_json)
    if denied:
        return None, denied

    return pg, None


def _pg_from_loan_or_response(db, loan, is_json=None):
    def unwrap_ref(value):
        if isinstance(value, dict):
            value = (
                value.get("$oid")
                or value.get("_id")
                or value.get("id")
                or value.get("pg_id")
                or value.get("pgId")
                or value.get("code")
            )
        elif hasattr(value, "id"):
            value = getattr(value, "id", value)
        return value

    pg_refs = []
    if isinstance(loan, dict):
        for key in (
            "pg_id", "pgId", "producer_group_id", "producerGroupId",
            "pg_code", "producer_group_code", "pg",
        ):
            value = unwrap_ref(loan.get(key))
            if value not in (None, ""):
                pg_refs.append(value)

    pg = None
    pg_fields = (
        "_id", "pg_id", "pgId", "pg_code", "code",
        "producer_group_id", "producer_group_code",
    )
    seen_queries = set()

    for pg_ref in pg_refs:
        raw_text = str(pg_ref).strip()
        candidates = [pg_ref]
        if raw_text and raw_text != pg_ref:
            candidates.append(raw_text)
        pg_oid = _to_object_id(pg_ref)
        if pg_oid:
            candidates.extend([pg_oid, str(pg_oid)])

        for field in pg_fields:
            for candidate in candidates:
                marker = (field, str(candidate), type(candidate).__name__)
                if marker in seen_queries:
                    continue
                seen_queries.add(marker)
                pg = db.pgs.find_one({field: candidate})
                if pg:
                    break
            if pg:
                break
        if pg:
            break

    if not pg:
        return None, _deny("Mapped PG not found for this loan.", is_json=is_json, status_code=404)

    denied = _require_pg_access(pg, is_json=is_json)
    if denied:
        return None, denied

    return pg, None


def _sanitize_loan_payload(payload: dict) -> dict:
    """Backend enforcement of manual rules:
    - If status == 'discontinued' => only allow 'status' and 'reason_discontinued'
    - If status == 'closed' => block changes to principal/emi/outstanding; allow closure metadata.
    """
    if not isinstance(payload, dict):
        return payload
    status = (payload.get("status") or payload.get("loan_status") or "").strip().lower()
    if status == "discontinued":
        keep = {"status", "loan_status", "reason_discontinued", "discontinued_reason", "remarks"}
        return {k: v for k, v in payload.items() if k in keep}
    if status == "closed":
        # allow only closure-related metadata
        keep = {"status", "loan_status", "closed_on", "closure_date", "remarks", "closing_remarks"}
        return {k: v for k, v in payload.items() if k in keep}
    return payload


@finance_bp.route("/pg_funds/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_funds(pg_id):
    db = current_app.mongo_db
    pg, access_response = _pg_from_id_or_response(db, pg_id, is_json=_is_json_request())
    if access_response:
        return access_response

    if request.method == "POST":
        write_response = _require_finance_write("View only: fund entry/update is handled by CLF/Admin authorities.")
        if write_response:
            return write_response

        fund_doc = {
            "pg_id": ObjectId(pg_id),
            "establishment_cost_received": request.form.get("establishment_cost_received") == "yes",
            "receiving_date": request.form.get("receiving_date"),
            "source": request.form.get("source"),
            "amount_received": float(request.form.get("amount_received") or 0),
            "total_utilized_amount": float(request.form.get("total_utilized_amount") or 0),
            "balance_amount": float(request.form.get("amount_received") or 0) - float(request.form.get("total_utilized_amount") or 0),
            "notes": request.form.get("notes"),
            "updated_at": datetime.utcnow(),
        }
        db.pg_funds.update_one(
            {"pg_id": ObjectId(pg_id)},
            {"$set": fund_doc},
            upsert=True,
        )
        flash("Fund details saved.", "success")
        return redirect(url_for("finance.pg_funds", pg_id=pg_id))

    fund = db.pg_funds.find_one({"pg_id": ObjectId(pg_id)})
    return render_template("pg_funds.html", pg=pg, fund=fund)

@finance_bp.route("/pg_loans/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_loans(pg_id):
    """Compatibility redirect for the removed old Loans page.

    The old single-document Loans module is retired. PG loan records must now
    be created and maintained only through PG Loan Accounts.
    """
    if request.method == "POST":
        flash("The old Loans page has been removed. Use PG Loan Accounts to create or update PG loans.", "warning")
    else:
        flash("Loans has been replaced by PG Loan Accounts.", "info")
    return redirect(url_for("finance.loan_accounts", pg_id=pg_id))



@finance_bp.route("/cashbook", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def cashbook():
    # Cash Book screen (data persists via /api/cashbook/<pg_id> in pg blueprint)
    pg_id = session.get("pg_id") or session.get("active_pg_id")
    return render_template("cash_book.html", pg_id=pg_id)


# ============================================================
# NEW: LOAN LIFECYCLE (PG + MEMBER) + REPAYMENTS + DASHBOARD
# - Uses PG Loan Accounts as the single source of truth for PG loans
# - Adds multiple loan accounts, disbursement, EMI schedule,
#   repayment tracking, overdue logic, and status (active/closed/NPA)
# ============================================================

def _amort_schedule(principal: float, annual_roi: float, tenure_months: int, start_date: str):
    """Simple amortization schedule (monthly).
    start_date: YYYY-MM-DD (1st due date will be next month same day if possible).
    Stores only essential fields for MIS dashboards.
    """
    from datetime import datetime
    import math

    principal = float(principal or 0)
    tenure_months = int(tenure_months or 0)
    annual_roi = float(annual_roi or 0)

    if principal <= 0 or tenure_months <= 0:
        return []

    r = (annual_roi/100.0)/12.0
    if r <= 0:
        emi = principal/tenure_months
    else:
        emi = principal * r * (1+r)**tenure_months / ((1+r)**tenure_months - 1)

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
    except Exception:
        sd = datetime.utcnow()

    schedule = []
    bal = principal
    for i in range(1, tenure_months+1):
        interest = bal * r
        principal_comp = emi - interest
        if principal_comp > bal:
            principal_comp = bal
        bal = bal - principal_comp
        due_date = sd.replace(day=min(sd.day, 28))
        # naive monthly increment
        month = due_date.month + (i-1)
        year = due_date.year + (month-1)//12
        month = (month-1)%12 + 1
        due_date = due_date.replace(year=year, month=month)
        schedule.append({
            "instalment_no": i,
            "due_date": due_date,
            "emi": round(emi,2),
            "principal": round(principal_comp,2),
            "interest": round(interest,2),
            "balance": round(bal,2),
            "is_paid": False,
            "paid_at": None,
            "paid_amount": 0.0,
        })
    return schedule

def _serialize_pg_loan_doc(l, i=1, source="pg_loan_accounts"):
    """
    Normalizes PG loan account documents for web/API responses.
    Source of truth: pg_loan_accounts.
    """
    def _dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    sanction_amount = float(l.get("sanction_amount") or l.get("sanctioned_amount") or 0)
    estimated_amount = float(l.get("estimated_amount") or 0)
    disbursed_amount = float(l.get("disbursed_amount") or l.get("amount_received") or sanction_amount or 0)

    principal_repaid = float(l.get("principal_repaid") or 0)
    interest_paid = float(l.get("interest_paid") or 0)
    total_paid = float(l.get("total_paid") or (principal_repaid + interest_paid) or 0)

    outstanding_amount = float(
        l.get("outstanding_amount")
        if l.get("outstanding_amount") is not None
        else max(disbursed_amount - principal_repaid, 0)
    )

    return {
        "_id": str(l.get("_id")),
        "id": str(l.get("_id")),
        "source": source,
        "sl": i,
        "loan_no": l.get("loan_no") or l.get("loan_account_no") or f"PG-LOAN-{i}",
        "lender": l.get("lender") or l.get("source") or l.get("bank_name") or "Block Released Loan",
        "purpose": l.get("purpose") or l.get("loan_purpose") or "",
        "status": (l.get("status") or l.get("loan_status") or "active").lower(),

        "business_plan_submitted": bool(l.get("business_plan_submitted")),
        "business_plan_date": l.get("business_plan_date"),

        "estimated_amount": estimated_amount,
        "sanction_amount": sanction_amount,
        "disbursed_amount": disbursed_amount,

        "roi": float(l.get("roi") or 0),
        "tenure_months": int(l.get("tenure_months") or 0),
        "moratorium_months": int(l.get("moratorium_months") or l.get("moratorium_period") or 0),

        "principal_repaid": principal_repaid,
        "interest_paid": interest_paid,
        "total_paid": total_paid,

        "outstanding_amount": outstanding_amount,
        "overdue_amount": float(l.get("overdue_amount") or 0),

        "installments_total": int(l.get("installments_total") or 0),
        "installments_paid": int(l.get("installments_paid") or 0),
        "installments_pending": int(l.get("installments_pending") or 0),

        "next_due_date": _dt(l.get("next_due_date")),
        "created_at": _dt(l.get("created_at")),
        "updated_at": _dt(l.get("updated_at")),
    }

def _loan_summary_from_schedule(schedule):
    """Compute loan KPIs required by the manual.

    Outputs:
      - principal_outstanding
      - interest_due
      - installment tracking (paid/total, next_due, overdue)
      - amount outstanding (EMI-based)
    """
    from datetime import datetime
    now = datetime.utcnow()

    schedule = schedule or []

    total_emi = 0.0
    total_paid = 0.0
    principal_outstanding = 0.0
    interest_due = 0.0
    principal_due = 0.0

    next_due = None
    overdue_amt = 0.0
    paid_installments = 0
    total_installments = 0

    for x in schedule:
        total_installments += 1
        emi = float(x.get("emi") or 0)
        pr = float(x.get("principal") or 0)
        it = float(x.get("interest") or 0)
        total_emi += emi

        if x.get("is_paid"):
            paid_installments += 1
            total_paid += float(x.get("paid_amount") or emi or 0)
        else:
            principal_outstanding += pr
            # interest due = unpaid interest portions up to now (and also current due)
            if x.get("due_date") and x["due_date"] <= now:
                interest_due += it
                principal_due += pr
                overdue_amt += emi
            if next_due is None:
                next_due = x.get("due_date")

    outstanding_amount = max(total_emi - total_paid, 0.0)

    return {
        "total_payable": round(total_emi, 2),
        "total_paid": round(total_paid, 2),
        "outstanding_amount": round(outstanding_amount, 2),

        "principal_outstanding": round(principal_outstanding, 2),
        "interest_due": round(interest_due, 2),
        "principal_due": round(principal_due, 2),

        "installments_total": int(total_installments),
        "installments_paid": int(paid_installments),
        "installments_pending": int(max(total_installments - paid_installments, 0)),

        "next_due_date": next_due,
        "overdue_amount": round(overdue_amt, 2),
    }

@finance_bp.route("/loan_dashboard/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def loan_dashboard(pg_id):
    db = current_app.mongo_db
    pg, access_response = _pg_from_id_or_response(db, pg_id, is_json=_is_json_request())
    if access_response:
        return access_response
    session["active_pg_id"] = str(pg.get("_id"))

    loans = list(db.pg_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)]))
    mloans = list(db.pg_member_loan_accounts.find({"pg_id": ObjectId(pg_id)}).sort([("created_at", -1)]).limit(50))

    # Basic KPIs
    total_outstanding = sum(float(l.get("outstanding_amount") or 0) for l in loans) + sum(float(l.get("outstanding_amount") or 0) for l in mloans)
    overdue = sum(float(l.get("overdue_amount") or 0) for l in loans) + sum(float(l.get("overdue_amount") or 0) for l in mloans)
    active_count = sum(1 for l in loans if (l.get("status") in ("active","ongoing"))) + sum(1 for l in mloans if (l.get("status") in ("active","ongoing")))

    return render_template(
        "loan_dashboard.html",
        pg=pg,
        loans=loans,
        mloans=mloans,
        kpi={"outstanding": total_outstanding, "overdue": overdue, "active": active_count},
        can_edit=_can_write_finance(),
    )

# changes made by atlanta 
@finance_bp.route("/loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_accounts(pg_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if is_json_request():
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    access_response = _require_pg_access(pg, is_json=is_json_request())
    if access_response:
        return access_response
    session["active_pg_id"] = str(pg.get("_id"))

    if request.method == "POST":
        write_response = _require_finance_write(
            "Loans can only be created/edited by CLF/Admin authorities.",
            is_json=is_json_request(),
        )
        if write_response:
            return write_response

        payload = request.get_json(silent=True) or request.form

        loan_no = (payload.get("loan_no") or "").strip()
        principal = float(payload.get("principal") or 0)
        sanctioned = float(payload.get("sanction_amount") or 0)
        disbursed = float(payload.get("disbursed_amount") or 0)
        roi = float(payload.get("roi") or 0)
        tenure = int(payload.get("tenure_months") or 0)
        start_date = (
            payload.get("first_due_date")
            or payload.get("disbursement_date")
            or datetime.utcnow().strftime("%Y-%m-%d")
        )

        base_amt = disbursed if disbursed > 0 else (sanctioned if sanctioned > 0 else principal)
        schedule = _amort_schedule(base_amt, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": pg_obj_id,
            "loan_no": loan_no or f"PG-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "lender": payload.get("lender") or payload.get("source") or "",
            "purpose": payload.get("purpose") or "",
            "business_plan_submitted": payload.get("business_plan_submitted") == "yes",
            "business_plan_date": payload.get("business_plan_date") or None,
            "estimated_amount": principal,
            "sanction_amount": sanctioned,
            "disbursed_amount": disbursed,
            "disbursement_date": payload.get("disbursement_date") or None,
            "roi": roi,
            "tenure_months": tenure,
            "moratorium_months": int(payload.get("moratorium_months") or 0),
            "status": (payload.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        result = db.pg_loan_accounts.insert_one(doc)

        if is_json_request():
            return jsonify({
                "ok": True,
                "message": "Loan account created.",
                "loan_id": str(result.inserted_id),
            }), 201

        flash("Loan account created.", "success")
        return redirect(url_for("finance.loan_accounts", pg_id=pg_id))

    loan_accounts_raw = list(
        db.pg_loan_accounts.find({"pg_id": pg_obj_id}).sort([("created_at", -1)])
    )

    loans = [
        _serialize_pg_loan_doc(l, i=i, source="pg_loan_accounts")
        for i, l in enumerate(loan_accounts_raw, start=1)
    ]

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "loans": loans,
            "can_create": _can_write_finance(),
        })

    return render_template(
        "loan_accounts.html",
        pg=pg,
        loans=loans,
        can_edit=_can_write_finance(),
    )

# changes made by atlanta
@finance_bp.route("/loan_account/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def loan_account_view(loan_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        loan_obj_id = ObjectId(loan_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid loan ID"}), 400
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    loan = db.pg_loan_accounts.find_one({"_id": loan_obj_id})
    loan_source = "pg_loan_accounts"

    if not loan:
        if is_json_request():
            return jsonify({"ok": False, "message": "Loan not found"}), 404
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg, access_response = _pg_from_loan_or_response(db, loan, is_json=is_json_request())
    if access_response:
        return access_response

    # Keep the hierarchy sidebar and back-navigation on the PG whose loan was
    # opened. This is especially important for CLF/Block users switching PGs.
    session["active_pg_id"] = str(pg.get("_id"))

    if request.method == "POST":
        write_response = _require_finance_write(
            "Loan repayments/updates can only be entered by CLF/Admin authorities.",
            is_json=is_json_request(),
        )
        if write_response:
            return write_response

        payload = request.get_json(silent=True) or request.form

        amt = float(payload.get("paid_amount") or 0)
        inst_no = int(payload.get("instalment_no") or 0)
        paid_at = payload.get("paid_at") or datetime.utcnow().strftime("%Y-%m-%d")

        from datetime import datetime as _dt
        try:
            paid_dt = _dt.strptime(paid_at, "%Y-%m-%d")
        except Exception:
            paid_dt = _dt.utcnow()

        schedule = loan.get("schedule") or []

        for item in schedule:
            if int(item.get("instalment_no") or 0) == inst_no and not item.get("is_paid"):
                item["is_paid"] = True
                item["paid_at"] = paid_dt
                item["paid_amount"] = amt
                break

        summary = _loan_summary_from_schedule(schedule)

        status = (loan.get("status") or "active").strip().lower()
        if summary.get("overdue_amount", 0) > 0 and summary.get("next_due_date"):
            try:
                if (datetime.utcnow() - summary["next_due_date"]).days >= 90:
                    status = "npa"
            except Exception:
                pass
        if summary.get("outstanding_amount", 0) <= 0.01:
            status = "closed"

        db.pg_loan_repayments.insert_one({
            "loan_id": loan_obj_id,
            "pg_id": loan.get("pg_id"),
            "instalment_no": inst_no,
            "paid_amount": amt,
            "paid_at": paid_dt,
            "created_at": datetime.utcnow(),
        })

        db.pg_loan_accounts.update_one(
            {"_id": loan_obj_id},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )

        if is_json_request():
            return jsonify({"ok": True, "message": "Repayment saved."}), 200

        flash("Repayment saved.", "success")
        return redirect(url_for("finance.loan_account_view", loan_id=loan_id))

    repayments_raw = list(
        db.pg_loan_repayments.find({"loan_id": loan_obj_id}).sort([("paid_at", -1)])
    )

    repayments = [
        {
            "_id": str(r.get("_id")),
            "instalment_no": int(r.get("instalment_no") or 0),
            "paid_amount": float(r.get("paid_amount") or 0),
            "paid_at": serialize_dt(r.get("paid_at")),
            "created_at": serialize_dt(r.get("created_at")),
        }
        for r in repayments_raw
    ]

    schedule_json = []
    for s in loan.get("schedule") or []:
        schedule_json.append({
            "instalment_no": int(s.get("instalment_no") or 0),
            "due_date": serialize_dt(s.get("due_date")),
            "emi": float(s.get("emi") or 0),
            "is_paid": bool(s.get("is_paid")),
            "paid_amount": float(s.get("paid_amount") or 0),
            "paid_at": serialize_dt(s.get("paid_at")),
        })

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "loan": {
                **_serialize_pg_loan_doc(loan, i=1, source=loan_source),
                "schedule": schedule_json,
            },
            "repayments": repayments,
            "can_edit": _can_write_finance(),
        })

    return render_template(
        "loan_account_view.html",
        pg=pg,
        loan=loan,
        repayments=repayments_raw,
        can_edit=_can_write_finance(),
    )

#changes made atlanta
@finance_bp.route("/member_loan_accounts/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_accounts(pg_id):
    db = current_app.mongo_db

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if request.args.get("format") == "json" or request.headers.get("Accept", "").lower().find("application/json") >= 0:
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if request.args.get("format") == "json" or request.headers.get("Accept", "").lower().find("application/json") >= 0:
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    access_response = _require_pg_access(pg, is_json=_is_json_request())
    if access_response:
        return access_response

    members_raw = list(
        db.pg_members.find({"pg_id": pg_obj_id}, {"name": 1, "member_name": 1}).limit(500)
    )

    def _member_name(m):
        return (m.get("name") or m.get("member_name") or "Member").strip()

    members = [
        {
            "_id": str(m["_id"]),
            "name": _member_name(m),
        }
        for m in members_raw
    ]

    is_json_request = (
        request.args.get("format") == "json"
        or "application/json" in (request.headers.get("Accept", "").lower())
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )

    # PG/Block can view loans, but only CLF/Admin authorities can create/update.
    if request.method == "POST":
        write_response = _require_finance_write(
            "Member loans can only be created/edited by CLF/Admin authorities.",
            is_json=is_json_request,
        )
        if write_response:
            return write_response

        payload = request.get_json(silent=True) or request.form

        member_id = payload.get("member_id")
        principal = float(payload.get("principal") or 0)
        roi = float(payload.get("roi") or 0)
        tenure = int(payload.get("tenure_months") or 0)
        start_date = payload.get("first_due_date") or datetime.utcnow().strftime("%Y-%m-%d")

        schedule = _amort_schedule(principal, roi, tenure, start_date)
        summary = _loan_summary_from_schedule(schedule)

        doc = {
            "pg_id": pg_obj_id,
            "member_id": ObjectId(member_id) if (member_id and ObjectId.is_valid(member_id)) else member_id,
            "loan_no": (payload.get("loan_no") or "").strip() or f"MB-LOAN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "purpose": payload.get("purpose") or "",
            "principal": principal,
            "roi": roi,
            "tenure_months": tenure,
            "status": (payload.get("status") or "active").strip().lower(),
            "schedule": schedule,
            **summary,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        result = db.pg_member_loan_accounts.insert_one(doc)

        if is_json_request:
            return jsonify({
                "ok": True,
                "message": "Member loan created.",
                "loan_id": str(result.inserted_id)
            }), 201

        flash("Member loan created.", "success")
        return redirect(url_for("finance.member_loan_accounts", pg_id=pg_id))

    loans_raw = list(
        db.pg_member_loan_accounts.find({"pg_id": pg_obj_id}).sort([("created_at", -1)])
    )

    member_map = {m["_id"]: m["name"] for m in members}

    def _serialize_dt(dt):
        if not dt:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)

    loans = []
    for i, l in enumerate(loans_raw, start=1):
        member_id_str = str(l.get("member_id")) if l.get("member_id") is not None else ""
        loans.append({
            "_id": str(l["_id"]),
            "sl": i,
            "loan_no": l.get("loan_no") or "",
            "member_id": member_id_str,
            "member_name": member_map.get(member_id_str, "Member"),
            "purpose": l.get("purpose") or "",
            "principal": float(l.get("principal") or 0),
            "roi": float(l.get("roi") or 0),
            "tenure_months": int(l.get("tenure_months") or 0),
            "status": (l.get("status") or "active").lower(),
            "outstanding_amount": float(l.get("outstanding_amount") or 0),
            "overdue_amount": float(l.get("overdue_amount") or 0),
            "total_paid": float(l.get("total_paid") or 0),
            "installments_total": int(l.get("installments_total") or 0),
            "installments_paid": int(l.get("installments_paid") or 0),
            "installments_pending": int(l.get("installments_pending") or 0),
            "next_due_date": _serialize_dt(l.get("next_due_date")),
            "created_at": _serialize_dt(l.get("created_at")),
            "updated_at": _serialize_dt(l.get("updated_at")),
        })

    if is_json_request:
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "members": members,
            "loans": loans,
            "can_create": _can_write_finance(),
        })

    return render_template(
        "member_loan_accounts.html",
        pg=pg,
        loans=loans_raw,
        members=members_raw,
        can_edit=_can_write_finance(),
    )

#changes made by atlanta
@finance_bp.route("/member_loan_view/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def member_loan_view(loan_id):
    db = current_app.mongo_db

    def is_json_request():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            return v.isoformat()
        except Exception:
            return str(v)

    try:
        loan_obj_id = ObjectId(loan_id)
    except Exception:
        if is_json_request():
            return jsonify({"ok": False, "message": "Invalid loan ID"}), 400
        flash("Invalid loan ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    loan = db.pg_member_loan_accounts.find_one({"_id": loan_obj_id})
    if not loan:
        if is_json_request():
            return jsonify({"ok": False, "message": "Loan not found"}), 404
        flash("Loan not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg, access_response = _pg_from_loan_or_response(db, loan, is_json=is_json_request())
    if access_response:
        return access_response

    session["active_pg_id"] = str(pg.get("_id"))

    member = None
    if loan.get("member_id") and ObjectId.is_valid(str(loan.get("member_id"))):
        member = db.pg_members.find_one({"_id": ObjectId(str(loan.get("member_id")))})

    if request.method == "POST":
        write_response = _require_finance_write(
            "View only: Loan entry/updates are handled by CLF/Admin.",
            is_json=is_json_request(),
        )
        if write_response:
            return write_response

        payload = request.get_json(silent=True) or request.form

        inst_no = int(payload.get("instalment_no") or 0)
        amt = float(payload.get("paid_amount") or 0)
        paid_at_raw = payload.get("paid_at")

        try:
            paid_dt = datetime.strptime(paid_at_raw, "%Y-%m-%d") if paid_at_raw else datetime.utcnow()
        except Exception:
            paid_dt = datetime.utcnow()

        schedule = loan.get("schedule") or []
        for row in schedule:
            if int(row.get("instalment_no") or 0) == inst_no:
                row["is_paid"] = True
                row["paid_amount"] = float(amt)
                row["paid_at"] = paid_dt
                break

        summary = _loan_summary_from_schedule(schedule)
        status = "closed" if float(summary.get("outstanding_amount") or 0) <= 0 else (loan.get("status") or "active")

        db.pg_member_loan_repayments.insert_one(
            {
                "loan_id": loan_obj_id,
                "pg_id": loan.get("pg_id"),
                "member_id": loan.get("member_id"),
                "instalment_no": inst_no,
                "paid_amount": amt,
                "paid_at": paid_dt,
                "created_at": datetime.utcnow(),
            }
        )

        db.pg_member_loan_accounts.update_one(
            {"_id": loan_obj_id},
            {"$set": {"schedule": schedule, **summary, "status": status, "updated_at": datetime.utcnow()}},
        )

        if is_json_request():
            return jsonify({
                "ok": True,
                "message": "Repayment saved.",
            }), 200

        flash("Repayment saved.", "success")
        return redirect(url_for("finance.member_loan_view", loan_id=loan_id))

    repayments_raw = list(
        db.pg_member_loan_repayments.find({"loan_id": loan_obj_id}).sort([("paid_at", -1)])
    )

    schedule = loan.get("schedule") or []
    repayments = [
        {
            "_id": str(r.get("_id")),
            "instalment_no": int(r.get("instalment_no") or 0),
            "paid_amount": float(r.get("paid_amount") or 0),
            "paid_at": serialize_dt(r.get("paid_at")),
            "created_at": serialize_dt(r.get("created_at")),
        }
        for r in repayments_raw
    ]

    schedule_json = []
    for s in schedule:
        schedule_json.append({
            "instalment_no": int(s.get("instalment_no") or 0),
            "due_date": serialize_dt(s.get("due_date")),
            "emi": float(s.get("emi") or 0),
            "is_paid": bool(s.get("is_paid")),
            "paid_amount": float(s.get("paid_amount") or 0),
            "paid_at": serialize_dt(s.get("paid_at")),
        })

    if is_json_request():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "loan": {
                "_id": str(loan.get("_id")),
                "loan_no": loan.get("loan_no") or "",
                "purpose": loan.get("purpose") or "",
                "principal": float(loan.get("principal") or 0),
                "roi": float(loan.get("roi") or 0),
                "tenure_months": int(loan.get("tenure_months") or 0),
                "status": (loan.get("status") or "active").lower(),
                "outstanding_amount": float(loan.get("outstanding_amount") or 0),
                "overdue_amount": float(loan.get("overdue_amount") or 0),
                "total_paid": float(loan.get("total_paid") or 0),
                "installments_total": int(loan.get("installments_total") or 0),
                "installments_paid": int(loan.get("installments_paid") or 0),
                "installments_pending": int(loan.get("installments_pending") or 0),
                "next_due_date": serialize_dt(loan.get("next_due_date")),
                "created_at": serialize_dt(loan.get("created_at")),
                "updated_at": serialize_dt(loan.get("updated_at")),
                "schedule": schedule_json,
            },
            "member": {
                "_id": str(member.get("_id")) if member else "",
                "name": (member.get("name") or member.get("member_name") or "Member") if member else "Member",
            },
            "repayments": repayments,
            "can_edit": _can_write_finance(),
        })

    return render_template(
        "member_loan_view.html",
        pg=pg,
        loan=loan,
        member=member,
        repayments=repayments_raw,
        can_edit=_can_write_finance(),
    )


def _safe_float(value, default=0.0):
    try:
        return float(value or default)
    except Exception:
        return float(default)


def _grant_utilization_total(db, grant_id, include_rejected=False):
    grant_obj_id = grant_id if isinstance(grant_id, ObjectId) else ObjectId(str(grant_id))

    match = {"grant_id": grant_obj_id}
    if not include_rejected:
        match["status"] = {"$ne": "REJECTED"}

    rows = list(db.pg_grant_utilizations.aggregate([
        {"$match": match},
        {"$group": {"_id": None, "utilized": {"$sum": "$amount"}}}
    ]))

    return round(float(rows[0]["utilized"] if rows else 0), 2)


def _sync_grant_utilization_summary(db, grant_id):
    """Sync approved utilization total and balance into pg_grants.

    Only approved-stage utilization entries are counted in parent grant totals.
    Pending entries remain visible in utilization history but do not become
    official utilized amount until approved.
    """
    grant_obj_id = grant_id if isinstance(grant_id, ObjectId) else ObjectId(str(grant_id))

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return {
            "utilized_amount": 0.0,
            "balance_amount": 0.0,
        }

    approved_statuses = [
        "CLF_APPROVED",
        "BLOCK_APPROVED",
        "DISTRICT_APPROVED",
        "STATE_APPROVED",
        "APPROVED",
    ]

    approved_utils = list(
        db.pg_grant_utilizations.find(
            {
                "grant_id": grant_obj_id,
                "status": {"$in": approved_statuses},
            },
            {
                "amount": 1,
            },
        )
    )

    received = _safe_float(grant.get("amount_received"))
    utilized = round(
        sum(_safe_float(u.get("amount")) for u in approved_utils),
        2,
    )
    balance = round(received - utilized, 2)

    db.pg_grants.update_one(
        {"_id": grant_obj_id},
        {
            "$set": {
                "utilized_amount": utilized,
                "balance_amount": balance,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    return {
        "utilized_amount": utilized,
        "balance_amount": balance,
    }



# changes made by atlanta
@finance_bp.route("/grants/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grants(pg_id):
    db = current_app.mongo_db

    def wants_json():
        return (
            request.args.get("format") == "json"
            or "application/json" in (request.headers.get("Accept", "").lower())
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def serialize_dt(v):
        if not v:
            return None
        try:
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            return str(v)[:10]
        except Exception:
            return str(v)

    try:
        pg_obj_id = ObjectId(pg_id)
    except Exception:
        if wants_json():
            return jsonify({"ok": False, "message": "Invalid PG ID"}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if wants_json():
            return jsonify({"ok": False, "message": "PG not found"}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    access_response = _require_pg_access(pg, is_json=wants_json())
    if access_response:
        return access_response

    if request.method == "POST":
        write_response = _require_finance_write(
            "Grant creation/update is handled by CLF/Admin authorities.",
            is_json=wants_json(),
        )
        if write_response:
            return write_response

        payload = request.get_json(silent=True) or request.form

        amount_received = _safe_float(payload.get("amount_received"))

        doc = {
            "pg_id": pg_obj_id,
            "category": payload.get("category") or "Infrastructure",
            "source": payload.get("source") or "",
            "release_date": payload.get("release_date") or None,
            "amount_received": amount_received,
            "utilized_amount": 0.0,
            "balance_amount": amount_received,
            "uc_status": payload.get("uc_status") or "pending",
            "uc_submitted_date": payload.get("uc_submitted_date") or None,
            "notes": payload.get("notes") or "",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        res = db.pg_grants.insert_one(doc)

        if wants_json():
            return jsonify({
                "ok": True,
                "message": "Grant added.",
                "grant_id": str(res.inserted_id),
            }), 201

        flash("Grant added.", "success")
        return redirect(url_for("finance.grants", pg_id=pg_id))

    grants_raw = list(db.pg_grants.find({"pg_id": pg_obj_id}).sort([("created_at", -1)]))

    grants_json = []
    for i, g in enumerate(grants_raw, start=1):
        summary = _sync_grant_utilization_summary(db, g.get("_id"))

        g["utilized_amount"] = summary["utilized_amount"]
        g["balance_amount"] = summary["balance_amount"]

        grants_json.append({
            "_id": str(g.get("_id")),
            "id": str(g.get("_id")),
            "sl": i,
            "category": g.get("category") or "",
            "source": g.get("source") or "",
            "release_date": serialize_dt(g.get("release_date")),
            "amount_received": float(g.get("amount_received") or 0),
            "utilized_amount": float(g.get("utilized_amount") or 0),
            "balance_amount": float(g.get("balance_amount") or 0),
            "uc_status": g.get("uc_status") or "pending",
            "uc_submitted_date": serialize_dt(g.get("uc_submitted_date")),
            "notes": g.get("notes") or "",
            "created_at": serialize_dt(g.get("created_at")),
            "updated_at": serialize_dt(g.get("updated_at")),
        })

    if wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "pg_name": pg.get("pg_name") or pg.get("name") or "",
            },
            "grants": grants_json,
        })

    return render_template("grants.html", pg=pg, grants=grants_raw)



@finance_bp.route('/uploads/<filename>')
@login_required
def serve_uploaded_file(filename):
    import os
    from flask import send_from_directory, current_app

    BASE_DIR = os.path.abspath(os.path.join(current_app.root_path, ".."))
    upload_folder = os.path.join(BASE_DIR, "uploads")

    return send_from_directory(upload_folder, filename)




# changes made by atlanta
@finance_bp.route("/grant/<grant_id>/update", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grant_update(grant_id):
    db = current_app.mongo_db

    try:
        grant_obj_id = ObjectId(grant_id)
    except Exception:
        return jsonify({"ok": False, "message": "Invalid grant ID"}), 400

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return jsonify({"ok": False, "message": "Grant not found"}), 404

    pg, access_response = _pg_from_grant_or_response(db, grant, is_json=True)
    if access_response:
        return access_response

    write_response = _require_finance_write(
        "Grant update is handled by CLF/Admin authorities.",
        is_json=True,
    )
    if write_response:
        return write_response

    payload = request.get_json(silent=True) or request.form

    update_doc = {
        "category": payload.get("category") or grant.get("category") or "Infrastructure",
        "source": payload.get("source") or "",
        "release_date": payload.get("release_date") or None,
        "amount_received": float(payload.get("amount_received") or 0),
        "uc_status": payload.get("uc_status") or "pending",
        "uc_submitted_date": payload.get("uc_submitted_date") or None,
        "notes": payload.get("notes") or "",
        "updated_at": datetime.utcnow(),
    }

    db.pg_grants.update_one({"_id": grant_obj_id}, {"$set": update_doc})
    _sync_grant_utilization_summary(db, grant_obj_id)

    return jsonify({"ok": True, "message": "Grant updated."}), 200


# changes made by atlanta
@finance_bp.route("/grant/<grant_id>/delete", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def grant_delete(grant_id):
    db = current_app.mongo_db

    try:
        grant_obj_id = ObjectId(grant_id)
    except Exception:
        return jsonify({"ok": False, "message": "Invalid grant ID"}), 400

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        return jsonify({"ok": False, "message": "Grant not found"}), 404

    pg, access_response = _pg_from_grant_or_response(db, grant, is_json=True)
    if access_response:
        return access_response

    write_response = _require_finance_write(
        "Grant deletion is handled by CLF/Admin authorities.",
        is_json=True,
    )
    if write_response:
        return write_response

    db.pg_grant_utilizations.delete_many({"grant_id": grant_obj_id})
    db.pg_grants.delete_one({"_id": grant_obj_id})

    return jsonify({"ok": True, "message": "Grant deleted."}), 200


@finance_bp.route("/grant/<grant_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope="pg")
def grant_view(grant_id):
    db = current_app.mongo_db

    def wants_json():
        return _is_json_request()

    def serialize_dt(v):
        if not v:
            return None
        try:
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            return str(v)[:10]
        except Exception:
            return str(v)

    def current_role():
        return _role()

    def current_user_id():
        user = _current_user_doc()
        if isinstance(user, dict) and user.get("_id"):
            return str(user.get("_id"))

        for key in ("user_id", "_id", "id", "sub", "uid"):
            value = _session_or_user_value(key, user)
            if value not in (None, "", []):
                return str(value)

        return str(session.get("user_id") or "")

    def is_utilization_submit_allowed():
        return current_role() in {
            "PG_DATA_ENTRY",
            "CLF_MANAGER",
            "CLF_ADMIN",
            "BLOCK_ADMIN",
            "DISTRICT_ADMIN",
            "ADMIN",
            "STATE_ADMIN",
            "SUPER_ADMIN",
        }

    def is_rejected_status(status):
        return str(status or "").strip().upper() == "REJECTED"

    def is_approved_status(status):
        return str(status or "").strip().upper() in {
            "CLF_APPROVED",
            "BLOCK_APPROVED",
            "DISTRICT_APPROVED",
            "STATE_APPROVED",
            "APPROVED",
        }

    try:
        grant_obj_id = ObjectId(str(grant_id))
    except Exception:
        if wants_json():
            return jsonify({"ok": False, "message": "Invalid grant ID"}), 400
        flash("Invalid grant ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    grant = db.pg_grants.find_one({"_id": grant_obj_id})
    if not grant:
        if wants_json():
            return jsonify({"ok": False, "message": "Grant not found"}), 404
        flash("Grant not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg, access_response = _pg_from_grant_or_response(db, grant, is_json=wants_json())
    if access_response:
        return access_response

    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form
        action = (payload.get("action") or "utilize").strip().lower()

        # ============================================================
        # GRANT DELETE / UPDATE
        # Only CLF/Admin finance authority can do this.
        # PG_DATA_ENTRY must not be blocked from utilization submission,
        # but must remain blocked from grant edit/delete.
        # ============================================================
        if action in {"delete", "update"}:
            write_response = _require_finance_write(
                "Grant creation/update is handled by CLF/Admin authorities.",
                is_json=wants_json(),
            )
            if write_response:
                return write_response

            if action == "delete":
                db.pg_grant_utilizations.delete_many({"grant_id": grant_obj_id})
                db.pg_grants.delete_one({"_id": grant_obj_id})

                if wants_json():
                    return jsonify({"ok": True, "message": "Grant deleted."}), 200

                flash("Grant deleted.", "success")
                return redirect(url_for("finance.grants", pg_id=str(pg.get("_id"))))

            amount_received = _safe_float(payload.get("amount_received"))

            update_doc = {
                "category": payload.get("category") or grant.get("category") or "Infrastructure",
                "source": payload.get("source") or "",
                "release_date": payload.get("release_date") or None,
                "amount_received": amount_received,
                "uc_status": payload.get("uc_status") or "pending",
                "uc_submitted_date": payload.get("uc_submitted_date") or None,
                "notes": payload.get("notes") or "",
                "updated_at": datetime.utcnow(),
            }

            db.pg_grants.update_one({"_id": grant_obj_id}, {"$set": update_doc})
            _sync_grant_utilization_summary(db, grant_obj_id)

            if wants_json():
                return jsonify({"ok": True, "message": "Grant updated."}), 200

            flash("Grant updated.", "success")
            return redirect(url_for("finance.grants", pg_id=str(pg.get("_id"))))

        # ============================================================
        # GRANT UTILIZATION SUBMISSION
        # PG_DATA_ENTRY is allowed here.
        # Entry is saved as PENDING and goes to approval workflow.
        # ============================================================
        if action == "utilize":
            if not is_utilization_submit_allowed():
                if wants_json():
                    return jsonify({
                        "ok": False,
                        "message": "You are not allowed to submit grant utilization."
                    }), 403

                flash("You are not allowed to submit grant utilization.", "danger")
                return redirect(url_for("finance.grant_view", grant_id=grant_id))

            head = (payload.get("head") or "").strip()
            amount = _safe_float(payload.get("amount"))

            if not head:
                if wants_json():
                    return jsonify({"ok": False, "message": "Head is required."}), 400
                flash("Head is required.", "danger")
                return redirect(url_for("finance.grant_view", grant_id=grant_id))

            if amount <= 0:
                if wants_json():
                    return jsonify({"ok": False, "message": "Amount must be greater than 0."}), 400
                flash("Amount must be greater than 0.", "danger")
                return redirect(url_for("finance.grant_view", grant_id=grant_id))

            utilized_at = payload.get("utilized_at") or datetime.utcnow().strftime("%Y-%m-%d")
            try:
                u_dt = datetime.strptime(utilized_at, "%Y-%m-%d")
            except Exception:
                u_dt = datetime.utcnow()

            existing = list(
                db.pg_grant_utilizations.find(
                    {"grant_id": grant_obj_id},
                    {"amount": 1, "status": 1}
                )
            )

            committed_total = sum(
                _safe_float(x.get("amount"))
                for x in existing
                if not is_rejected_status(x.get("status"))
            )

            balance_for_submission = _safe_float(grant.get("amount_received")) - committed_total

            if amount > balance_for_submission + 1e-6:
                msg = f"Utilization exceeds available balance (Available: ₹{balance_for_submission:,.2f})."
                if wants_json():
                    return jsonify({"ok": False, "message": msg}), 400
                flash(msg, "danger")
                return redirect(url_for("finance.grant_view", grant_id=grant_id))

            attachments = []

            try:
                from werkzeug.utils import secure_filename

                upload_dir = current_app.config.get("UPLOAD_FOLDER", "uploads")
                os.makedirs(upload_dir, exist_ok=True)

                files = request.files.getlist("attachments") if request.files else []

                for f in files:
                    if not f or not getattr(f, "filename", ""):
                        continue

                    fn = secure_filename(f.filename)
                    if not fn:
                        continue

                    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
                    saved = f"grant_{grant_id}_{stamp}_{fn}"
                    path = os.path.join(upload_dir, saved)

                    f.save(path)
                    attachments.append(saved)

            except Exception:
                attachments = []

            util_doc = {
                "grant_id": grant_obj_id,
                "pg_id": grant.get("pg_id"),
                "utilized_at": u_dt,
                "head": head,
                "amount": amount,
                "remarks": payload.get("remarks") or "",
                "attachments": attachments,

                "status": "PENDING",
                "created_by": current_user_id(),
                "created_role": current_role(),
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),

                "approved_by": None,
                "approved_at": None,
                "rejected_by": None,
                "rejected_at": None,
                "rejection_reason": "",
            }

            res = db.pg_grant_utilizations.insert_one(util_doc)
            _sync_grant_utilization_summary(db, grant_obj_id)

            if wants_json():
                return jsonify({
                    "ok": True,
                    "message": "Grant utilization submitted for approval.",
                    "utilization_id": str(res.inserted_id),
                }), 201

            flash("Grant utilization submitted for approval.", "success")
            return redirect(url_for("finance.grant_view", grant_id=grant_id))

        if wants_json():
            return jsonify({"ok": False, "message": "Invalid grant action."}), 400

        flash("Invalid grant action.", "danger")
        return redirect(url_for("finance.grant_view", grant_id=grant_id))

    utils_raw = list(
        db.pg_grant_utilizations.find({"grant_id": grant_obj_id}).sort(
            [("utilized_at", -1), ("created_at", -1)]
        )
    )

    utilized_total = sum(
        _safe_float(x.get("amount"))
        for x in utils_raw
        if is_approved_status(x.get("status"))
    )

    balance = round(_safe_float(grant.get("amount_received")) - utilized_total, 2)

    utils = []
    for i, u in enumerate(utils_raw, start=1):
        utils.append({
            "_id": str(u.get("_id")),
            "sl": i,
            "utilized_at": serialize_dt(u.get("utilized_at")),
            "head": u.get("head") or "",
            "amount": _safe_float(u.get("amount")),
            "remarks": u.get("remarks") or "",
            "status": (u.get("status") or "PENDING").upper(),
            "attachments": u.get("attachments") or [],
            "attachments_count": len(u.get("attachments") or []),
            "created_at": serialize_dt(u.get("created_at")),
            "updated_at": serialize_dt(u.get("updated_at")),
        })

    grant_json = {
        "_id": str(grant.get("_id")),
        "category": grant.get("category") or "",
        "source": grant.get("source") or "",
        "release_date": serialize_dt(grant.get("release_date")),
        "amount_received": _safe_float(grant.get("amount_received")),
        "uc_status": grant.get("uc_status") or "pending",
        "uc_submitted_date": serialize_dt(grant.get("uc_submitted_date")),
        "notes": grant.get("notes") or "",
        "utilized_total": round(utilized_total, 2),
        "balance": balance,
        "created_at": serialize_dt(grant.get("created_at")),
        "updated_at": serialize_dt(grant.get("updated_at")),
    }

    if wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")) if pg else "",
                "pg_name": (pg.get("pg_name") or pg.get("name") or "") if pg else "",
            },
            "grant": grant_json,
            "utils": utils,
            "role": current_role(),
            "can_edit": _can_write_finance(),
        }), 200

    return render_template(
        "grant_view.html",
        pg=pg,
        grant=grant,
        utils=utils_raw,
        utilized_total=utilized_total,
        balance=balance,
        role=current_role(),
    )




@finance_bp.route("/grant_utilization/<util_id>/approve", methods=["POST"])
@login_required
@roles_required("CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def approve_grant_utilization(util_id):
    db = current_app.mongo_db
    is_json = _is_json_request()

    try:
        util_obj_id = ObjectId(str(util_id))
    except Exception:
        if is_json:
            return jsonify({"ok": False, "message": "Invalid utilization ID."}), 400
        flash("Invalid utilization ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    util = db.pg_grant_utilizations.find_one({"_id": util_obj_id})
    if not util:
        if is_json:
            return jsonify({"ok": False, "message": "Utilization entry not found."}), 404
        flash("Utilization entry not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": util.get("pg_id")}) if util.get("pg_id") else None
    access_response = _require_pg_access(pg, is_json=is_json)
    if access_response:
        return access_response

    role = _role()
    cur = (util.get("status") or "PENDING").upper()

    next_status = None

    if role in ("CLF_MANAGER", "CLF_ADMIN") and cur == "PENDING":
        next_status = "CLF_APPROVED"

    elif role == "BLOCK_ADMIN" and cur in ("PENDING", "CLF_APPROVED"):
        next_status = "BLOCK_APPROVED"

    elif role == "DISTRICT_ADMIN" and cur in ("PENDING", "CLF_APPROVED", "BLOCK_APPROVED"):
        next_status = "DISTRICT_APPROVED"

    elif role in ("ADMIN", "STATE_ADMIN", "SUPER_ADMIN") and cur not in ("REJECTED", "STATE_APPROVED"):
        next_status = "STATE_APPROVED"

    if not next_status:
        msg = "Cannot approve at this stage. Please follow the approval chain."
        if is_json:
            return jsonify({"ok": False, "message": msg}), 400

        flash(msg, "danger")
        return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))

    user = _current_user_doc()
    approver_id = str(user.get("_id")) if isinstance(user, dict) and user.get("_id") else str(session.get("user_id") or "")

    db.pg_grant_utilizations.update_one(
        {"_id": util_obj_id},
        {
            "$set": {
                "status": next_status,
                "approved_by": approver_id,
                "approved_role": role,
                "approved_at": datetime.utcnow(),
                "rejected_by": None,
                "rejected_at": None,
                "rejection_reason": "",
                "updated_at": datetime.utcnow(),
            }
        },
    )

    summary = _sync_grant_utilization_summary(db, util.get("grant_id"))

    if is_json:
        return jsonify({
            "ok": True,
            "message": f"Utilization moved to {next_status}.",
            "status": next_status,
            "utilized_amount": summary["utilized_amount"],
            "balance_amount": summary["balance_amount"],
        }), 200

    flash(f"Utilization moved to {next_status}.", "success")
    return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))



@finance_bp.route("/grant_utilization/<util_id>/reject", methods=["POST"])
@login_required
@roles_required("CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def reject_grant_utilization(util_id):
    db = current_app.mongo_db
    is_json = _is_json_request()

    try:
        util_obj_id = ObjectId(str(util_id))
    except Exception:
        if is_json:
            return jsonify({"ok": False, "message": "Invalid utilization ID."}), 400
        flash("Invalid utilization ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    util = db.pg_grant_utilizations.find_one({"_id": util_obj_id})
    if not util:
        if is_json:
            return jsonify({"ok": False, "message": "Utilization entry not found."}), 404
        flash("Utilization entry not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": util.get("pg_id")}) if util.get("pg_id") else None
    access_response = _require_pg_access(pg, is_json=is_json)
    if access_response:
        return access_response

    payload = request.get_json(silent=True) or request.form
    reason = (payload.get("reason") or "Rejected").strip()

    user = _current_user_doc()
    rejecter_id = str(user.get("_id")) if isinstance(user, dict) and user.get("_id") else str(session.get("user_id") or "")

    db.pg_grant_utilizations.update_one(
        {"_id": util_obj_id},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": reason,
                "rejected_by": rejecter_id,
                "rejected_role": _role(),
                "rejected_at": datetime.utcnow(),
                "approved_by": None,
                "approved_at": None,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    summary = _sync_grant_utilization_summary(db, util.get("grant_id"))

    if is_json:
        return jsonify({
            "ok": True,
            "message": "Utilization rejected.",
            "status": "REJECTED",
            "utilized_amount": summary["utilized_amount"],
            "balance_amount": summary["balance_amount"],
        }), 200

    flash("Utilization rejected.", "warning")
    return redirect(url_for("finance.grant_view", grant_id=str(util.get("grant_id"))))




# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available
