import re

from services.audit_engine import AuditLogger
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from app.services.guards import require_unlocked_period
from flask import render_template, request, redirect, url_for, flash, current_app, session,jsonify, abort,g
from bson import ObjectId
from app.utils import safe_objectid
from datetime import datetime,timedelta
from . import pg_bp
from ..rbac import login_required, roles_required
from ..services.workflow import ensure_pg_locked, create_change_request, add_notification, save_uploaded_document
from ..services.audit import log_audit
from flask import g


PG_SECTOR_OPTIONS = ["Agri", "ARDD", "Fishery"]
AGRI_CROP_OPTIONS = [
    "Arhar", "Ash gourd", "Bhindi", "Bitter gourd", "Black gram",
    "Black pepper", "Bottle gourd", "Brinjal", "Chilli", "Cocumber",
    "Colocasia", "Cowpea", "Foxtail Millet", "Ginger", "Lab lab beans",
    "Maize", "Pineapple", "Potato", "Pumpkin", "Radish", "Mustard",
    "Ridge gourd", "Sesamum", "Turmeric", "Water Melon",
]
AGRI_FFS_MODULE_OPTIONS = [
    "FFS Module 1", "FFS Module 2", "FFS Module 3", "FFS Module 4", "FFS Module 5"
]
ARDD_ACTIVITY_OPTIONS = ["Goatery", "Piggery"]
ARDD_UNIT_OPTIONS = {
    "Goatery": ["GPU", "GFU"],
    "Piggery": ["PPU", "PFU"],
}
FISHERY_ACTIVITY_OPTIONS = ["Nursery", "Poli-culture", "Poli Culture high value"]


def _normalize_pg_sector(value):
    raw = str(value or '').strip().lower()
    mapping = {
        'agri': 'Agri',
        'ardd': 'ARDD',
        'arrd': 'ARDD',
        'fishery': 'Fishery',
    }
    return mapping.get(raw, str(value or '').strip())


def _fmt_inr(amount):
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0

    sign = "-" if amt < 0 else ""
    amt = abs(amt)

    # No decimals for dashboard KPI display.
    n = str(int(round(amt)))

    if len(n) <= 3:
        formatted = n
    else:
        last3 = n[-3:]
        rest = n[:-3]
        groups = []

        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]

        if rest:
            groups.insert(0, rest)

        formatted = ",".join(groups + [last3])

    return f"{sign}₹{formatted}"


def _sum_cashbook_rows(rows):
    """
    Sums cashbook rows safely.
    Supports schemas where a row may have:
      - amount (cash)
      - bankAmount (bank)
    If you only use 'amount', bankAmount will just be 0.
    """
    total = 0.0
    for r in (rows or []):
        try:
            total += float(r.get("amount") or 0)
        except Exception:
            pass
        try:
            total += float(r.get("bankAmount") or 0)
        except Exception:
            pass
    return round(total, 2)



def _count_rows_from_register_doc(doc):
    """Best-effort row counter for generic register payloads."""
    if not doc:
        return 0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0
    # Common shapes: {rows:[...]}, {items:[...]}, {entries:[...]}
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return 0

def _sum_qty_from_rows(doc, mode="any"):
    """Best-effort quantity sum from register documents.

    Supports:
    - data.rows / rows / items / entries / records / list
    - nested row lists
    - exact quantity fields
    - label-style fields like 'Quantity (Kg)', 'Input Quantity', 'Sold Quantity'
    """
    if not doc or not isinstance(doc, dict):
        return 0.0

    def to_float(value):
        try:
            if value in (None, "", "null", "None", "-", "NaN"):
                return 0.0

            cleaned = (
                str(value)
                .replace(",", "")
                .replace("kg", "")
                .replace("KG", "")
                .replace("Kg", "")
                .strip()
            )

            return float(cleaned) if cleaned else 0.0
        except Exception:
            return 0.0

    def norm_key(key):
        return (
            str(key or "")
            .strip()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("/", "_")
            .replace("(", "_")
            .replace(")", "_")
            .replace(".", "_")
            .lower()
        )

    input_exact_keys = {
        "input_stock", "inputstock", "stock_in", "stockin",
        "procured_qty", "procuredqty", "quantity_procured", "quantityprocured",
        "purchase_qty", "purchaseqty", "purchased_qty", "purchasedqty",
        "received_qty", "receivedqty", "in_qty", "inqty",
        "input_qty", "inputqty", "input_quantity", "inputquantity",
        "total_qty", "totalqty", "total_quantity", "totalquantity",
        "qty_kg", "qtykg", "quantity_kg", "quantitykg",
        "qty", "quantity", "weight", "weight_kg", "weightkg", "kg",
        "stock_kg", "stockkg"
    }

    output_exact_keys = {
        "output_sold", "outputsold", "sold_qty", "soldqty",
        "quantity_sold", "quantitysold", "sale_qty", "saleqty",
        "sales_qty", "salesqty", "sold_quantity", "soldquantity",
        "issued_qty", "issuedqty", "out_qty", "outqty",
        "output_qty", "outputqty", "output_quantity", "outputquantity",
        "total_qty", "totalqty", "total_quantity", "totalquantity",
        "qty_kg", "qtykg", "quantity_kg", "quantitykg",
        "qty", "quantity", "weight", "weight_kg", "weightkg", "kg",
        "stock_kg", "stockkg"
    }

    any_exact_keys = input_exact_keys | output_exact_keys | {
        "available", "available_stock", "availablestock",
        "closing_stock", "closingstock",
        "balance_stock", "balancestock",
        "stock", "stock_qty", "stockqty",
        "remaining_qty", "remainingqty",
        "remaining_quantity", "remainingquantity"
    }

    if mode == "input":
        exact_keys = input_exact_keys
        positive_words = ("input", "procured", "purchase", "purchased", "received", "in", "stock")
    elif mode == "output":
        exact_keys = output_exact_keys
        positive_words = ("output", "sold", "sale", "sales", "issued", "out")
    else:
        exact_keys = any_exact_keys
        positive_words = ("qty", "quantity", "kg", "weight", "stock")

    negative_words = (
        "rate", "price", "amount", "total_amount", "value",
        "name", "date", "remark", "description", "particular",
        "unit_price", "cost"
    )

    def is_quantity_key(key):
        nk = norm_key(key)

        if nk in exact_keys:
            return True

        # Do not treat rate/amount/value fields as quantity.
        if any(bad in nk for bad in negative_words):
            return False

        # Catch fields like:
        # Quantity (Kg), Input Quantity, Sold Quantity, Produce Qty, Stock KG
        if any(word in nk for word in ("qty", "quantity", "kg", "weight")):
            return True

        if mode == "input" and any(word in nk for word in positive_words):
            return any(x in nk for x in ("stock", "qty", "quantity", "kg", "weight"))

        if mode == "output" and any(word in nk for word in positive_words):
            return any(x in nk for x in ("qty", "quantity", "kg", "weight"))

        return False

    def extract_rows(obj):
        rows = []

        if isinstance(obj, list):
            for item in obj:
                rows.extend(extract_rows(item))
            return rows

        if not isinstance(obj, dict):
            return rows

        # If this dict itself has a quantity-looking field, treat it as one row.
        if any(is_quantity_key(k) for k in obj.keys()):
            rows.append(obj)

        for k, v in obj.items():
            nk = norm_key(k)

            if isinstance(v, list) and (
                nk in {
                    "rows", "items", "entries", "records", "list",
                    "inputs", "outputs", "products", "data",
                    "input_rows", "output_rows", "stock_rows"
                }
                or "row" in nk
                or "item" in nk
                or "product" in nk
            ):
                rows.extend(extract_rows(v))

            elif isinstance(v, dict) and nk in {
                "data", "register", "register_data", "payload", "form_data"
            }:
                rows.extend(extract_rows(v))

        return rows

    data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
    rows = extract_rows(data)

    total = 0.0

    for row in rows:
        if not isinstance(row, dict):
            continue

        for key, value in row.items():
            if is_quantity_key(key):
                qty = to_float(value)
                if qty:
                    total += qty
                    break

    return round(float(total), 2)


def _dash_num(value, default=0.0):
    try:
        if value in (None, "", "null", "None"):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = str(value).replace("₹", "").replace(",", "").strip()
        return float(cleaned) if cleaned else default
    except Exception:
        return default


def _dash_date(value, fallback=None):
    if isinstance(value, datetime):
        return value

    if not value:
        return fallback

    text = str(value).strip()

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[:20], fmt)
        except Exception:
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return fallback


def _dash_rows_from_doc(doc, *preferred_keys):
    if not isinstance(doc, dict):
        return []

    for key in preferred_keys:
        val = doc.get(key)
        if isinstance(val, list):
            return val

    data = doc.get("data")
    if isinstance(data, dict):
        for key in preferred_keys:
            val = data.get(key)
            if isinstance(val, list):
                return val

        for key in ("rows", "items", "entries", "records", "list", "receipts", "payments"):
            val = data.get(key)
            if isinstance(val, list):
                return val

    for key in ("rows", "items", "entries", "records", "list", "receipts", "payments"):
        val = doc.get(key)
        if isinstance(val, list):
            return val

    return []


def _dash_row_amount(row):
    if not isinstance(row, dict):
        return 0.0

    total = 0.0

    # Cashbook can have cash + bank amount.
    if "amount" in row or "bankAmount" in row:
        total += _dash_num(row.get("amount"))
        total += _dash_num(row.get("bankAmount"))
        return total

    for key in (
        "value",
        "total",
        "total_amount",
        "receipt_amount",
        "payment_amount",
        "cash_amount",
        "bank_amount",
        "debit",
        "credit",
        "paid_amount",
        "received_amount",
    ):
        if key in row:
            return _dash_num(row.get(key))

    return 0.0


def _dash_month_date(doc):
    try:
        year = int(doc.get("year") or 0)
        month = int(doc.get("month") or 0)
        if year and month:
            return datetime(year, month, 1)
    except Exception:
        pass

    return _dash_date(
        doc.get("date")
        or doc.get("entry_date")
        or doc.get("voucher_date")
        or doc.get("created_at")
        or doc.get("updated_at"),
        fallback=datetime.utcnow()
    )


def _dashboard_cash_metrics(db, oid, limit=10):
    """
    Builds real dashboard cash flow and recent transactions from:
    - pg_cashbooks receipts/payments
    - pg_income_expenditure
    - pg_receipt_vouchers
    - pg_payment_vouchers if available

    Contra cashbook rows are skipped from income/expense because they are only
    internal cash-bank transfers.
    """
    from datetime import timedelta

    today = datetime.utcnow().date()
    start_day = today - timedelta(days=6)

    day_map = {}
    for i in range(7):
        d = start_day + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        day_map[key] = {
            "label": d.strftime("%d %b"),
            "income": 0.0,
            "expense": 0.0,
            "net": 0.0,
        }

    recent = []

    def add_txn(dt, tx_type, description, income=0.0, expense=0.0, raw_id=None, status="Completed"):
        if not isinstance(dt, datetime):
            dt = datetime.utcnow()

        income_val = _dash_num(income)
        expense_val = _dash_num(expense)

        key = dt.strftime("%Y-%m-%d")
        if key in day_map:
            day_map[key]["income"] += income_val
            day_map[key]["expense"] += expense_val

        signed_amount = income_val if income_val else -expense_val

        if signed_amount != 0:
            recent.append({
                "id": str(raw_id or "")[-6:].upper() or dt.strftime("%H%M%S"),
                "type": tx_type or "Ledger Entry",
                "description": description or "PG transaction",
                "date": dt.strftime("%Y-%m-%d"),
                "amount": round(signed_amount, 2),
                "status": status or "Completed",
            })

    def is_contra_row(row):
        return isinstance(row, dict) and str(row.get("entryType") or "").strip().lower() == "contra"

    # 1) Cash Book
    try:
        cash_docs = list(
            db.pg_cashbooks
            .find({"pg_id": oid})
            .sort([("year", -1), ("month", -1), ("updated_at", -1)])
            .limit(24)
        )

        for doc in cash_docs:
            doc_date = _dash_month_date(doc)

            for row in doc.get("receipts", []) or []:
                if is_contra_row(row):
                    continue

                amount = _dash_row_amount(row)
                add_txn(
                    _dash_date(row.get("date"), doc_date),
                    "Cash Book Receipt",
                    row.get("particulars") or row.get("description") or row.get("remarks") or "Cash book receipt",
                    income=amount,
                    raw_id=doc.get("_id")
                )

            for row in doc.get("payments", []) or []:
                if is_contra_row(row):
                    continue

                amount = _dash_row_amount(row)
                add_txn(
                    _dash_date(row.get("date"), doc_date),
                    "Cash Book Payment",
                    row.get("particulars") or row.get("description") or row.get("remarks") or "Cash book payment",
                    expense=amount,
                    raw_id=doc.get("_id")
                )

    except Exception:
        pass

    # 2) Income / Expenditure
    try:
        ie_docs = list(
            db.pg_income_expenditure
            .find({"pg_id": oid})
            .sort([("created_at", -1), ("updated_at", -1)])
            .limit(300)
        )

        for doc in ie_docs:
            dt = _dash_date(
                doc.get("date") or doc.get("created_at") or doc.get("updated_at"),
                datetime.utcnow()
            )
            amount = _dash_num(doc.get("amount"))
            ttype = str(doc.get("type") or doc.get("txn_type") or "").lower()

            if ttype in ("income", "receipt", "receipts", "credit"):
                add_txn(
                    dt,
                    "Income",
                    doc.get("description") or doc.get("particulars") or doc.get("remarks") or "Income entry",
                    income=amount,
                    raw_id=doc.get("_id")
                )

            elif ttype in ("expense", "payment", "payments", "debit"):
                add_txn(
                    dt,
                    "Expense",
                    doc.get("description") or doc.get("particulars") or doc.get("remarks") or "Expense entry",
                    expense=amount,
                    raw_id=doc.get("_id")
                )

    except Exception:
        pass

    # 3) Receipt Voucher
    try:
        receipt_docs = list(
            db.pg_receipt_vouchers
            .find({"pg_id": oid})
            .sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)])
            .limit(200)
        )

        def _receipt_section_members(section):
            if not isinstance(section, dict):
                return []

            members = section.get("members")
            if isinstance(members, list):
                return members

            member = section.get("member")
            if isinstance(member, dict):
                return [member]

            return []

        def _receipt_member_entries(member):
            if not isinstance(member, dict):
                return []

            entries = member.get("entries")
            if isinstance(entries, list):
                return entries

            rows = member.get("rows")
            if isinstance(rows, list):
                return rows

            items = member.get("items")
            if isinstance(items, list):
                return items

            return []

        for doc in receipt_docs:
            doc_date = _dash_month_date(doc)

            # Old/simple format support
            direct_entries = _dash_rows_from_doc(doc, "entries", "rows", "items")

            if direct_entries:
                for row in direct_entries:
                    amount = _dash_row_amount(row)
                    add_txn(
                        _dash_date(row.get("date"), doc_date),
                        "Receipt Voucher",
                        row.get("commodity") or row.get("particulars") or row.get("description") or "Receipt voucher",
                        income=amount,
                        raw_id=doc.get("_id")
                    )

            # New receipt voucher structure support:
            # pg_group_receipt.members[].entries[]
            # group_member_receipt.members[].entries[]
            for section_key, tx_label in (
                ("pg_group_receipt", "PG Group Receipt"),
                ("group_member_receipt", "Group Member Receipt"),
            ):
                section = doc.get(section_key) or {}

                for member in _receipt_section_members(section):
                    member_name = (
                        member.get("member_name")
                        or member.get("name")
                        or section.get("pg_name")
                        or doc.get("pg_name")
                        or "Receipt voucher"
                    )

                    member_date = _dash_date(
                        member.get("txn_date")
                        or member.get("date")
                        or doc.get("date")
                        or doc.get("created_at")
                        or doc.get("updated_at"),
                        doc_date
                    )

                    member_details = (
                        member.get("details")
                        or member.get("description")
                        or member.get("remarks")
                        or ""
                    )

                    for row in _receipt_member_entries(member):
                        amount = _dash_row_amount(row)

                        description = (
                            row.get("commodity")
                            or row.get("particulars")
                            or row.get("description")
                            or member_details
                            or member_name
                            or "Receipt voucher"
                        )

                        add_txn(
                            member_date,
                            tx_label,
                            description,
                            income=amount,
                            raw_id=doc.get("_id")
                        )

            # Backward-compatible old frontend keys:
            # pg_section / member_section
            for section_key, tx_label in (
                ("pg_section", "PG Group Receipt"),
                ("member_section", "Group Member Receipt"),
            ):
                section = doc.get(section_key) or {}

                for member in _receipt_section_members(section):
                    member_name = (
                        member.get("member_name")
                        or member.get("name")
                        or section.get("pg_name")
                        or doc.get("pg_name")
                        or "Receipt voucher"
                    )

                    member_date = _dash_date(
                        member.get("txn_date")
                        or member.get("date")
                        or doc.get("date")
                        or doc.get("created_at")
                        or doc.get("updated_at"),
                        doc_date
                    )

                    member_details = (
                        member.get("details")
                        or member.get("description")
                        or member.get("remarks")
                        or ""
                    )

                    for row in _receipt_member_entries(member):
                        amount = _dash_row_amount(row)

                        description = (
                            row.get("commodity")
                            or row.get("particulars")
                            or row.get("description")
                            or member_details
                            or member_name
                            or "Receipt voucher"
                        )

                        add_txn(
                            member_date,
                            tx_label,
                            description,
                            income=amount,
                            raw_id=doc.get("_id")
                        )

    except Exception:
        pass

    # 4) Payment Voucher, only if collection exists
    try:
        if "pg_payment_vouchers" in db.list_collection_names():
            payment_docs = list(
                db.pg_payment_vouchers
                .find({"pg_id": oid})
                .sort([("year", -1), ("month", -1), ("updated_at", -1)])
                .limit(200)
            )

            for doc in payment_docs:
                doc_date = _dash_month_date(doc)
                entries = _dash_rows_from_doc(doc, "entries", "rows", "items")

                if entries:
                    for row in entries:
                        amount = _dash_row_amount(row)
                        add_txn(
                            _dash_date(row.get("date"), doc_date),
                            "Payment Voucher",
                            row.get("particulars") or row.get("description") or row.get("remarks") or "Payment voucher",
                            expense=amount,
                            raw_id=doc.get("_id")
                        )
                else:
                    amount = _dash_num(doc.get("total") or doc.get("total_amount") or doc.get("amount"))
                    add_txn(
                        doc_date,
                        "Payment Voucher",
                        doc.get("description") or "Payment voucher",
                        expense=amount,
                        raw_id=doc.get("_id")
                    )

    except Exception:
        pass

    chart_cash_flow = []
    for item in day_map.values():
        item["income"] = round(item["income"], 2)
        item["expense"] = round(item["expense"], 2)
        item["net"] = round(item["income"] - item["expense"], 2)
        chart_cash_flow.append(item)

    recent = sorted(recent, key=lambda x: x.get("date", ""), reverse=True)

    if limit is not None:
        recent = recent[:int(limit)]

    income_7d = round(sum(x["income"] for x in chart_cash_flow), 2)
    expense_7d = round(sum(x["expense"] for x in chart_cash_flow), 2)
    net_balance = round(income_7d - expense_7d, 2)

    return {
        "chart_cash_flow": chart_cash_flow,
        "recent_transactions": recent,
        "income_7d": income_7d,
        "expense_7d": expense_7d,
        "net_balance": net_balance,
    }


def _set_pg_status(db, pg_id, status, extra=None):
    extra = extra or {}
    if status in ("active", "approved"):
        extra.setdefault("is_locked", True)
        extra.setdefault("approved_at", datetime.utcnow())
        extra.setdefault("approved_by", session.get("user_id"))
        extra.setdefault("approval_history", [])
    db.pgs.update_one(
        {"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))},
        {"$set": {"status": status, "updated_at": datetime.utcnow(), **extra}},
    )

def _current_user_dict():
    #  MOBILE FIX: prefer g (set by JWT in rbac.py) over session (web-only)
    return {
        "user_id": getattr(g, "user_id", None) or session.get("user_id"),
        "username": getattr(g, "username", None) or session.get("username"),
        "role": getattr(g, "role", None) or session.get("role"),
    }


# ============================================================
# PG / Membership Registration Validation Workflow
# ============================================================

VALIDATION_PENDING_STATUSES = ("submitted", "resubmitted")
VALIDATION_EDIT_LOCK_STATUSES = ("submitted", "resubmitted", "approved")
VALIDATION_FORM_TYPES = {
    "pg_registration": {
        "field": "registration_validation",
        "label": "PG Registration",
        "form_endpoint": "pg.pg_registration",
    },
    "member_registration": {
        "field": "member_registration_validation",
        "label": "Membership Registration",
        "form_endpoint": "pg.pg_members",
    },
}


def _wants_json_response():
    return bool(
        request.headers.get("Authorization")
        or request.is_json
        or "application/json" in (request.headers.get("Accept", "") or "").lower()
    )


def _current_role():
    return getattr(g, "role", None) or session.get("role")


def _current_user_id():
    return getattr(g, "user_id", None) or session.get("user_id")


def _current_user_id_value():
    uid = _current_user_id()
    try:
        return ObjectId(str(uid)) if uid and ObjectId.is_valid(str(uid)) else uid
    except Exception:
        return uid


def _validation_now():
    return datetime.utcnow()


def _validation_status(doc, form_type):
    field = VALIDATION_FORM_TYPES.get(form_type, {}).get("field")
    if not field:
        return "draft"

    validation_doc = doc.get(field) or {}
    return str(validation_doc.get("status") or "draft").lower()


def _validation_doc(doc, form_type):
    field = VALIDATION_FORM_TYPES.get(form_type, {}).get("field")
    validation_doc = dict(doc.get(field) or {}) if field else {}

    validation_doc.setdefault("status", "draft")
    validation_doc.setdefault("remarks", "")
    validation_doc.setdefault("submitted_at", None)
    validation_doc.setdefault("submitted_by", None)
    validation_doc.setdefault("reviewed_by", None)
    validation_doc.setdefault("reviewed_at", None)
    validation_doc.setdefault("history", [])

    return validation_doc


def _validation_field(form_type):
    meta = VALIDATION_FORM_TYPES.get(form_type)
    return meta.get("field") if meta else None


def _validation_label(form_type):
    meta = VALIDATION_FORM_TYPES.get(form_type) or {}
    return meta.get("label") or form_type.replace("_", " ").title()


def _validation_form_url(form_type, pg_id):
    meta = VALIDATION_FORM_TYPES.get(form_type) or {}
    endpoint = meta.get("form_endpoint") or "pg.pg_view"
    try:
        return url_for(endpoint, pg_id=str(pg_id))
    except Exception:
        return url_for("pg.pg_view", pg_id=str(pg_id))


def _load_pg_for_validation(db, pg_id):
    oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    if not oid:
        return None
    return db.pgs.find_one({"_id": oid})


def _validation_json_or_redirect(ok, message, *, status=200, redirect_to=None, extra=None, category=None):
    extra = extra or {}

    if _wants_json_response():
        payload = {"ok": bool(ok), "message" if ok else "error": message}
        payload.update(extra)
        return jsonify(payload), status

    flash(message, category or ("success" if ok else "danger"))
    return redirect(redirect_to or url_for("reports.hierarchy_dashboard"))


def _pg_user_can_access_pg(pg):
    role = _current_role()
    pg_id = str(pg.get("_id"))

    if role == "PG_DATA_ENTRY":
        current_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
        return current_pg_id == pg_id

    if role == "CADRE_CC":
        try:
            allowed_pg_ids = {str(x) for x in _assigned_pg_ids_for_session()}
        except Exception:
            allowed_pg_ids = {str(x) for x in (session.get("assigned_pg_ids") or [])}
        return pg_id in allowed_pg_ids

    return False


def _block_admin_can_validate_pg(pg):
    role = _current_role()
    if role != "BLOCK_ADMIN":
        return False

    block_id = getattr(g, "block_id", None) or session.get("block_id")
    if not block_id:
        return False

    pg_block_values = [
        pg.get("block_id"),
        pg.get("Block_id"),
        pg.get("blockId"),
    ]

    return any(str(v) == str(block_id) for v in pg_block_values if v)


def _make_validation_history_entry(action, status, remarks=""):
    return {
        "action": action,
        "status": status,
        "remarks": remarks or "",
        "by": _current_user_id_value(),
        "by_role": _current_role(),
        "at": _validation_now(),
    }


def _submit_validation(db, pg, form_type):
    field = _validation_field(form_type)
    if not field:
        return False, "Invalid validation form type.", 400

    role = _current_role()
    if role not in ("PG_DATA_ENTRY", "CADRE_CC"):
        return False, "Only PG login can submit this form for validation.", 403

    if not _pg_user_can_access_pg(pg):
        return False, "You cannot submit this PG form.", 403

    current_doc = _validation_doc(pg, form_type)
    current_status = str(current_doc.get("status") or "draft").lower()

    if current_status == "approved" and form_type != "member_registration":
        return False, f"{_validation_label(form_type)} is already approved.", 409

    if current_status in VALIDATION_PENDING_STATUSES:
        return False, f"{_validation_label(form_type)} is already submitted for Block validation.", 409

    new_status = "resubmitted" if current_status in ("rejected", "approved") else "submitted"
    now = _validation_now()

    history = list(current_doc.get("history") or [])
    history.append(_make_validation_history_entry("submit", new_status, ""))

    update_doc = {
        f"{field}.status": new_status,
        f"{field}.submitted_at": now,
        f"{field}.submitted_by": _current_user_id_value(),
        f"{field}.reviewed_by": None,
        f"{field}.reviewed_at": None,
        f"{field}.history": history,
        "updated_at": now,
    }

    # Preserve rejection remarks until Block validates again; this helps PG see what was corrected.
    db.pgs.update_one({"_id": pg["_id"]}, {"$set": update_doc})

    try:
        add_notification(
            db,
            to_role="BLOCK_ADMIN",
            title=f"{_validation_label(form_type)} submitted",
            body=f'PG "{pg.get("name") or pg.get("pg_name") or pg.get("_id")}" submitted {_validation_label(form_type)} for validation.',
            link=url_for("pg.validation_review", form_type=form_type, pg_id=str(pg["_id"])),
        )
    except Exception:
        pass

    return True, f"{_validation_label(form_type)} submitted for Block validation.", 200


def _review_validation(db, pg, form_type, action, remarks=""):
    field = _validation_field(form_type)
    if not field:
        return False, "Invalid validation form type.", 400

    if not _block_admin_can_validate_pg(pg):
        return False, "Only mapped Block Admin can validate this form.", 403

    current_doc = _validation_doc(pg, form_type)
    current_status = str(current_doc.get("status") or "draft").lower()

    if current_status not in VALIDATION_PENDING_STATUSES:
        return False, f"{_validation_label(form_type)} is not pending validation.", 409

    action = str(action or "").lower().strip()
    remarks = (remarks or "").strip()
    now = _validation_now()

    if action == "approve":
        new_status = "approved"
        action_label = "approved"
        # A successful member-registration approval closes the rejection
        # cycle, so do not carry the previous rejection remarks forward.
        # Any remarks entered for this approval are still retained.
        if form_type == "member_registration":
            remarks_to_store = remarks
        else:
            remarks_to_store = remarks or current_doc.get("remarks") or ""
    elif action == "reject":
        if not remarks:
            return False, "Rejection remarks are required.", 400
        new_status = "rejected"
        action_label = "rejected"
        remarks_to_store = remarks
    else:
        return False, "Invalid validation action.", 400

    history = list(current_doc.get("history") or [])
    history.append(_make_validation_history_entry(action_label, new_status, remarks_to_store))

    update_doc = {
        f"{field}.status": new_status,
        f"{field}.remarks": remarks_to_store,
        f"{field}.reviewed_by": _current_user_id_value(),
        f"{field}.reviewed_at": now,
        f"{field}.history": history,
        "updated_at": now,
    }

    # ----------------------------------------------------------
    # PG Registration snapshot lock on rejection
    # ----------------------------------------------------------
    # When Block Admin rejects PG Registration, store the exact
    # submitted data as rejected_snapshot. Later, when PG edits and
    # resubmits, submit_pg_registration_validation() compares the new
    # snapshot with this rejected_snapshot and prepares changed_fields.
    # ----------------------------------------------------------
    if form_type == "pg_registration" and action == "reject":
        update_doc[f"{field}.rejected_snapshot"] = _pg_registration_review_snapshot(pg)
        update_doc[f"{field}.changed_fields"] = []
        update_doc[f"{field}.changed_field_keys"] = []
        update_doc[f"{field}.snapshot_rejected_at"] = now

    # ----------------------------------------------------------
    # Optional cleanup on approval
    # ----------------------------------------------------------
    # Keep submitted_snapshot and changed_fields for audit/view history.
    # Only clear rejected_snapshot because correction cycle is completed.
    # ----------------------------------------------------------
    if form_type == "pg_registration" and action == "approve":
        update_doc[f"{field}.approved_snapshot"] = _pg_registration_review_snapshot(pg)
        update_doc[f"{field}.snapshot_approved_at"] = now
        update_doc[f"{field}.remarks"] = ""
        update_doc[f"{field}.changed_fields"] = []
        update_doc[f"{field}.changed_field_keys"] = []
        update_doc[f"{field}.previous_snapshot"] = {}
        update_doc[f"{field}.rejected_snapshot"] = {}

    db.pgs.update_one({"_id": pg["_id"]}, {"$set": update_doc})

    try:
        add_notification(
            db,
            to_role="PG_DATA_ENTRY",
            title=f"{_validation_label(form_type)} {action_label}",
            body=f'Your {_validation_label(form_type)} for PG "{pg.get("name") or pg.get("pg_name") or pg.get("_id")}" was {action_label}.',
            link=_validation_form_url(form_type, pg["_id"]),
        )
    except Exception:
        pass

    return True, f"{_validation_label(form_type)} {action_label} successfully.", 200



def _validation_badge_status(pg, form_type):
    status = _validation_status(pg, form_type)
    if status not in ("draft", "submitted", "approved", "rejected", "resubmitted"):
        return "draft"
    return status

def _fmt_inr(amount):
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0

    sign = "-" if amt < 0 else ""
    amt = abs(amt)

    # No decimals for dashboard KPI display.
    n = str(int(round(amt)))

    if len(n) <= 3:
        formatted = n
    else:
        last3 = n[-3:]
        rest = n[:-3]
        groups = []

        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]

        if rest:
            groups.insert(0, rest)

        formatted = ",".join(groups + [last3])

    return f"{sign}₹{formatted}"


def _count_rows_from_register_doc(doc):
    """Best-effort row counter for generic register payloads."""
    if not doc:
        return 0
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None and isinstance(doc, dict):
        data = doc
    if not isinstance(data, dict):
        return 0
    # Common shapes: {rows:[...]}, {items:[...]}, {entries:[...]}
    for k in ("rows","items","entries","records","list"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return 0


def _pg_scope_query(pg_id):
    ids = []

    try:
        if pg_id:
            ids.append(str(pg_id))
    except Exception:
        pass

    try:
        oid = safe_objectid(pg_id)
        if oid:
            ids.append(oid)
            ids.append(str(oid))
    except Exception:
        pass

    try:
        session_pg_id = session.get("pg_id") or session.get("active_pg_id")
        if session_pg_id:
            ids.append(str(session_pg_id))
            soid = safe_objectid(session_pg_id)
            if soid:
                ids.append(soid)
                ids.append(str(soid))
    except Exception:
        pass

    clean_ids = []
    for x in ids:
        if x not in clean_ids and x not in (None, "", "None"):
            clean_ids.append(x)

    return {
        "$or": [
            {"pg_id": {"$in": clean_ids}},
            {"PG_ID": {"$in": clean_ids}},
            {"active_pg_id": {"$in": clean_ids}},
            {"producer_group_id": {"$in": clean_ids}},
        ]
    }


def _pg_metrics(db, pg_id):
    """Compute dashboard metrics for a PG from existing MongoDB collections."""
    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        return {
            "members_count": 0,
            "lakhpati_count": 0,
            "loans_count": 0,
            "outstanding_loans_amount": _fmt_inr(0),
            "categories_count": 0,
            "income_7d": _fmt_inr(0),
            "expense_7d": _fmt_inr(0),
        }

    # ---- PG Data Entry registers (counts + stock) ----
    meetings_count = 0
    minutes_count = 0
    member_ledger_entries = 0
    assets_count = 0
    receipt_vouchers_count = 0
    input_rows_count = 0
    output_rows_count = 0
    total_stock_kg = 0.0
    # Keep both legacy collections initialized even when the redesigned
    # monthly stock collection is used, because dashboard debug metrics
    # still expose their counts.
    input_docs = []
    output_docs = []
    input_stock_docs = []
    product_stock_docs = []

    try:
        meetings_count = db.pg_meetings.count_documents({"pg_id": oid})
    except Exception:
        pass

    try:
        minutes_doc = db.pg_meeting_minutes.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        minutes_count = _count_rows_from_register_doc(minutes_doc) or (1 if minutes_doc else 0)
    except Exception:
        pass

    try:
        member_doc = db.pg_member_ledgers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        member_ledger_entries = _count_rows_from_register_doc(member_doc)
    except Exception:
        pass

    try:
        asset_doc = db.pg_asset_registers.find_one({"pg_id": oid}, sort=[("updated_at", -1)])
        assets_count = _count_rows_from_register_doc(asset_doc)
    except Exception:
        pass

    try:
        receipt_vouchers_count = db.pg_receipt_vouchers.count_documents({"pg_id": oid})
    except Exception:
        pass

    try:
        stock_query = _pg_scope_query(pg_id)
        stock_docs = list(db.pg_stocks_monthly.find(stock_query).sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)]).limit(1000))
        input_stock_docs = [d for d in stock_docs if str(d.get("stock_type") or "").lower() == "input"]
        product_stock_docs = [d for d in stock_docs if str(d.get("stock_type") or "").lower() == "product"]
        input_rows_count = len(input_stock_docs)
        output_rows_count = len(product_stock_docs)
        input_stock_value = sum(float(d.get("closing_qty") or 0) for d in input_stock_docs)
        output_sold_value = sum(float(d.get("closing_qty") or 0) for d in product_stock_docs)
        total_stock_kg = input_stock_value + output_sold_value

        # Backward-compatible fallback for PGs that still have only legacy registers.
        if not stock_docs:
            input_docs = list(db.pg_input_registers.find(stock_query).sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)]).limit(500))
            output_docs = list(db.pg_output_registers.find(stock_query).sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)]).limit(500))
            input_rows_count = sum(_count_rows_from_register_doc(doc) for doc in input_docs)
            output_rows_count = sum(_count_rows_from_register_doc(doc) for doc in output_docs)
            input_stock_value = sum(_sum_qty_from_rows(doc, mode="input") for doc in input_docs)
            output_sold_value = sum(_sum_qty_from_rows(doc, mode="output") for doc in output_docs)
            total_stock_kg = max(0.0, input_stock_value - output_sold_value)
    except Exception:
        input_docs = []
        output_docs = []
        input_stock_docs = []
        product_stock_docs = []
        input_stock_value = 0.0
        output_sold_value = 0.0
        total_stock_kg = 0.0

    # ------------------------------------------------------------
    #  ONLY ACTIVE MEMBERS SHOULD COUNT IN LIVE DASHBOARD
    # - old rows without is_active are treated as active for backward compatibility
    # ------------------------------------------------------------
    active_member_filter = {
        "pg_id": oid,
        "$or": [
            {"is_active": True},
            {"is_active": {"$exists": False}}
        ]
    }

    members_count = 0
    try:
        members_count = db.pg_members.count_documents(active_member_filter)
    except Exception:
        members_count = 0

    # Lakhpati Didi count (PG members flagged + active only)
    lakhpati_count = 0
    try:
        lakhpati_count = db.pg_members.count_documents({
            "pg_id": oid,
            "lakh_pati_didi": True,
            "$or": [
                {"is_active": True},
                {"is_active": {"$exists": False}}
            ]
        })
    except Exception:
        lakhpati_count = 0

    # Loans lifecycle
    # Source of truth: PG loans = pg_loan_accounts, member loans = pg_member_loan_accounts.
    pg_loan_accounts = list(db.pg_loan_accounts.find({"pg_id": oid}))
    member_loan_accounts = list(db.pg_member_loan_accounts.find({"pg_id": oid}))

    loans_count = len(pg_loan_accounts) + len(member_loan_accounts)
    outstanding = 0.0

    for ln in (pg_loan_accounts + member_loan_accounts):
        try:
            outstanding += float(ln.get("outstanding_amount") or 0)
        except Exception:
            pass

    # Alerts (computed)
    try:
        from app.services.alerts import compute_pg_alerts
        alerts_count = len(compute_pg_alerts(db, str(oid)))
    except Exception:
        alerts_count = 0

    # Categories: try from PG profile if available, else 0
    pg_doc = db.pgs.find_one({"_id": oid}, {"categories": 1, "commodities": 1, "primary_activities": 1})
    categories_count = 0
    if pg_doc:
        for key in ("categories", "commodities", "primary_activities"):
            val = pg_doc.get(key)
            if isinstance(val, list):
                categories_count = max(categories_count, len(val))

    # Income/Expense last 7 days (if collection exists)
    income_7d = 0.0
    expense_7d = 0.0
    try:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=7)
        txns = list(db.pg_income_expenditure.find({"pg_id": oid, "created_at": {"$gte": since}}))
        for t in txns:
            amt = 0.0
            try:
                amt = float(t.get("amount") or 0)
            except Exception:
                amt = 0.0
            ttype = (t.get("type") or t.get("txn_type") or "").lower()
            if ttype in ("income", "receipt", "receipts", "credit"):
                income_7d += amt
            elif ttype in ("expense", "payment", "payments", "debit"):
                expense_7d += amt
    except Exception:
        pass

    # ---- Turnover (latest available month) ----
    turnover_latest = 0.0
    turnover_period = None
    try:
        latest = list(db.pg_market_transactions.find({"pg_id": oid}, {"year": 1, "month": 1}).sort([("year", -1), ("month", -1)]).limit(1))
        latest2 = list(db.pg_business_monthly.find({"pg_id": oid}, {"year": 1, "month": 1}).sort([("year", -1), ("month", -1)]).limit(1))
        cand = []
        if latest:
            cand.append((int(latest[0].get("year") or 0), int(latest[0].get("month") or 0)))
        if latest2:
            cand.append((int(latest2[0].get("year") or 0), int(latest2[0].get("month") or 0)))
        cand = [x for x in cand if x[0] and x[1]]
        if cand:
            y, m = sorted(cand, reverse=True)[0]
            from ..services.workflow import calc_turnover_and_stock
            metrics_tm = calc_turnover_and_stock(db, str(oid), y, m)
            turnover_latest = float(metrics_tm.get("total_turnover") or 0)
            turnover_period = f"{y}-{m:02d}"
    except Exception:
        pass

    # ---- Profit/Loss (latest available month) from income-expenditure ----
    profit_loss_latest = 0.0
    profit_period = None
    try:
        ie = db.pg_income_expenditure.find_one({"pg_id": oid}, sort=[("year", -1), ("month", -1)])
        if ie:
            profit_loss_latest = float(ie.get("excess_income_over_expenditure") or 0)
            profit_period = f"{int(ie.get('year') or 0)}-{int(ie.get('month') or 0):02d}" if ie.get("year") and ie.get("month") else None
    except Exception:
        pass

    profit_latest = max(profit_loss_latest, 0.0)
    loss_latest = abs(min(profit_loss_latest, 0.0))

    # ---- Grants ----
    grants_count = 0
    grants_received_total = 0.0
    try:
        grants = list(db.pg_grants.find({"pg_id": oid}, {"amount_received": 1}))
        grants_count = len(grants)
        grants_received_total = round(sum(float(g.get("amount_received") or 0) for g in grants), 2)
    except Exception:
        pass

    from datetime import timedelta

    chart_cash_flow = []
    chart_membership_growth = []
    chart_loan_distribution = []
    chart_output_stock = []

    # Cash flow last 7 days
    try:
        today = datetime.utcnow().date()
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            start = datetime(d.year, d.month, d.day)
            end = start + timedelta(days=1)

            income = 0.0
            expense = 0.0

            txns = db.pg_income_expenditure.find({
                "pg_id": oid,
                "created_at": {"$gte": start, "$lt": end}
            })

            for t in txns:
                amt = float(t.get("amount") or 0)
                ttype = (t.get("type") or t.get("txn_type") or "").lower()

                if ttype in ("income", "receipt", "receipts", "credit"):
                    income += amt
                elif ttype in ("expense", "payment", "payments", "debit"):
                    expense += amt

            chart_cash_flow.append({
                "label": d.strftime("%d %b"),
                "income": income,
                "expense": expense,
                "net": income - expense
            })
    except Exception:
        pass


    # Membership growth by month
    try:
        now = datetime.utcnow()
        for i in range(5, -1, -1):
            month = now.month - i
            year = now.year

            while month <= 0:
                month += 12
                year -= 1

            start = datetime(year, month, 1)
            end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

            count = db.pg_members.count_documents({
                "pg_id": oid,
                "created_at": {"$lt": end},
                "$or": [
                    {"is_active": True},
                    {"is_active": {"$exists": False}}
                ]
            })

            chart_membership_growth.append({
                "label": start.strftime("%b"),
                "members": count
            })
    except Exception:
        pass


       # Loan distribution
    try:
        disbursed = 0.0
        outstanding_total = 0.0

        for ln in pg_loan_accounts + member_loan_accounts:
            disbursed += float(
                ln.get("disbursed_amount")
                or ln.get("sanction_amount")
                or ln.get("principal")
                or ln.get("loan_amount")
                or ln.get("principal_amount")
                or 0
            )
            outstanding_total += float(ln.get("outstanding_amount") or 0)

        recovered = max(0, disbursed - outstanding_total)

        chart_loan_distribution = [
            {"label": "Disbursed", "value": disbursed},
            {"label": "Recovered", "value": recovered},
            {"label": "Outstanding", "value": outstanding_total},
        ]
    except Exception:
        pass

    # Output stock snapshot
    try:
        if stock_docs:
            available_stock_value = input_stock_value + output_sold_value
        else:
            input_stock_value = sum(_sum_qty_from_rows(doc, mode="input") for doc in input_docs)
            output_sold_value = sum(_sum_qty_from_rows(doc, mode="output") for doc in output_docs)
            available_stock_value = max(0.0, input_stock_value - output_sold_value)
            total_stock_kg = available_stock_value

        chart_output_stock = [
            {
                "label": "Stock",
                "input_stock": round(input_stock_value, 2),
                "output_sold": round(output_sold_value, 2),
                "available": round(available_stock_value, 2),
                "value": round(available_stock_value, 2),
            }
        ]
    except Exception:
        chart_output_stock = [
            {
                "label": "Stock",
                "input_stock": 0,
                "output_sold": 0,
                "available": 0,
                "value": 0,
            }
        ]

    # Real cash flow + recent transactions from cashbook/vouchers/income-expenditure
    dashboard_cash = _dashboard_cash_metrics(db, oid, limit=10)
    chart_cash_flow = dashboard_cash.get("chart_cash_flow", [])
    recent_transactions = dashboard_cash.get("recent_transactions", [])

    income_7d = dashboard_cash.get("income_7d", 0)
    expense_7d = dashboard_cash.get("expense_7d", 0)
    net_balance = dashboard_cash.get("net_balance", 0)

    return {
        "members_count": members_count,
        "meetings_count": meetings_count,
        "minutes_count": minutes_count,
        "member_ledger_entries": member_ledger_entries,
        "assets_count": assets_count,
        "receipt_vouchers_count": receipt_vouchers_count,
        "input_rows_count": input_rows_count,
        "output_rows_count": output_rows_count,
        "total_stock_kg": round(float(total_stock_kg or 0), 2),
        "lakhpati_count": lakhpati_count,
        "loans_count": loans_count,
        "outstanding_loans_amount": _fmt_inr(outstanding),
        "categories_count": categories_count,
        "income_7d": _fmt_inr(income_7d),
        "expense_7d": _fmt_inr(expense_7d),
        "net_balance": _fmt_inr(net_balance),

        "chart_cash_flow": chart_cash_flow,
        "chart_membership_growth": chart_membership_growth,
        "chart_loan_distribution": chart_loan_distribution,
                "chart_output_stock": chart_output_stock,

        "stock_debug": {
            "input_docs_count": len(input_docs),
            "output_docs_count": len(output_docs),
            "input_rows_count": input_rows_count,
            "output_rows_count": output_rows_count,
            "input_stock_value": round(float(input_stock_value or 0), 2),
            "output_sold_value": round(float(output_sold_value or 0), 2),
            "total_stock_kg": round(float(total_stock_kg or 0), 2),
        },

        "recent_transactions": recent_transactions,

        # Turnover + Profit/Loss (latest month)
        "turnover_latest": _fmt_inr(turnover_latest),
        "turnover_period": turnover_period,
        "profit_latest": _fmt_inr(profit_latest),
        "loss_latest": _fmt_inr(loss_latest),
        "profit_loss_latest": _fmt_inr(profit_loss_latest),
        "profit_period": profit_period,

        # Grants
        "has_grants": True if grants_count else False,
        "grants_count": grants_count,
        "grants_received_total": _fmt_inr(grants_received_total),
    }
    
 ## new hybrided code of dashboard
@pg_bp.route("/home")
@login_required
def pg_home():
    db = current_app.mongo_db

    pg_id = getattr(g, "pg_id", None) or session.get("pg_id")
    role  = getattr(g, "role", None) or session.get("role")

    role = getattr(g, "role", None) or session.get("role")

    # Mobile app can request a PG explicitly
    requested_pg_id = (
        request.args.get("pg_id")
        or request.args.get("pgId")
        or request.headers.get("X-PG-ID")
    )
    
    # Only allow CADRE_CC to override PG via mobile request
    if role == "CADRE_CC" and requested_pg_id:
        allowed_pg_ids = _assigned_pg_ids_for_session()
        if str(requested_pg_id) in allowed_pg_ids:
            pg_id = str(requested_pg_id)

    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    def _serialize_pg(pg_doc):
        if not pg_doc:
            return None
        return {
            "_id": str(pg_doc.get("_id")) if pg_doc.get("_id") else None,
            "name": pg_doc.get("name"),
            "sector": pg_doc.get("sector"),
            "pg_type": pg_doc.get("pg_type"),
            "Village": pg_doc.get("Village"),
            "Block": pg_doc.get("Block"),
            "District": pg_doc.get("District"),
            "State": pg_doc.get("State"),
            "formation_date": pg_doc.get("formation_date"),
            "total_members": pg_doc.get("total_members", 0),
        }

    def _get_display_name():
        return (
            getattr(g, "name", None)
            or session.get("name")
            or session.get("username")
            or "USER"
        )

    if role in ("PG_DATA_ENTRY", "CADRE_CC") and pg_id:
        oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
        pg_doc = db.pgs.find_one({"_id": oid}) if oid else None
        metrics = _pg_metrics(db, pg_id)
        display_name = _get_display_name()

        reg_validation = _validation_doc(pg_doc or {}, "pg_registration")
        member_validation = _validation_doc(pg_doc or {}, "member_registration")

        def _safe_validation(v):
            return {
                "status": str(v.get("status") or "draft").lower(),
                "remarks": v.get("remarks") or "",
                "submitted_at": v.get("submitted_at").isoformat() if isinstance(v.get("submitted_at"), datetime) else v.get("submitted_at"),
                "reviewed_at": v.get("reviewed_at").isoformat() if isinstance(v.get("reviewed_at"), datetime) else v.get("reviewed_at"),
            }

        metrics["pg_registration_validation"] = _safe_validation(reg_validation)
        metrics["member_registration_validation"] = _safe_validation(member_validation)

        if _wants_json():
            return jsonify({
                "ok": True,
                "pg": {
                    **(_serialize_pg(pg_doc) or {}),
                    "registration_validation": metrics["pg_registration_validation"],
                    "member_registration_validation": metrics["member_registration_validation"],
                },
                "metrics": metrics,
                "user_name": display_name
            }), 200

        return render_template(
            "dashboard_pg.html",
            pg=pg_doc,
            metrics=metrics,
            user_name=display_name
        )

    if _wants_json():
        return jsonify({
            "ok": False,
            "error": "PG dashboard is available only for PG/Cadre users with a valid PG scope."
        }), 403

    return redirect(url_for("reports.hierarchy_dashboard"))

@pg_bp.route("/dashboard/live-data", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_dashboard_live_data():
    db = current_app.mongo_db

    role = getattr(g, "role", None) or session.get("role")

    requested_pg_id = (
        request.args.get("pg_id")
        or request.args.get("pgId")
        or request.headers.get("X-PG-ID")
        or getattr(g, "pg_id", None)
        or session.get("pg_id")
        or session.get("active_pg_id")
    )

    if not requested_pg_id or not ObjectId.is_valid(str(requested_pg_id)):
        return jsonify({
            "ok": False,
            "error": "Valid PG ID is required."
        }), 400

    pg_obj_id = ObjectId(str(requested_pg_id))
    pg_doc = db.pgs.find_one({"_id": pg_obj_id})

    if not pg_doc:
        return jsonify({
            "ok": False,
            "error": "PG not found."
        }), 404

    if role == "PG_DATA_ENTRY":
        session_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
        if session_pg_id and session_pg_id != str(requested_pg_id):
            return jsonify({
                "ok": False,
                "error": "You cannot access this PG."
            }), 403

    if role == "CADRE_CC":
        assigned_pg_ids = session.get("assigned_pg_ids") or []
        assigned_pg_ids = {str(x) for x in assigned_pg_ids}

        if assigned_pg_ids and str(requested_pg_id) not in assigned_pg_ids:
            return jsonify({
                "ok": False,
                "error": "This PG is not assigned to you."
            }), 403

    metrics = _pg_metrics(db, str(requested_pg_id))

    reg_validation = _validation_doc(pg_doc or {}, "pg_registration")
    member_validation = _validation_doc(pg_doc or {}, "member_registration")

    def _safe_validation(v):
        return {
            "status": str(v.get("status") or "draft").lower(),
            "remarks": v.get("remarks") or "",
            "submitted_at": v.get("submitted_at").isoformat() if isinstance(v.get("submitted_at"), datetime) else v.get("submitted_at"),
            "reviewed_at": v.get("reviewed_at").isoformat() if isinstance(v.get("reviewed_at"), datetime) else v.get("reviewed_at"),
        }

    metrics["pg_registration_validation"] = _safe_validation(reg_validation)
    metrics["member_registration_validation"] = _safe_validation(member_validation)

    return jsonify({
        "ok": True,
        "pg": {
                "_id": str(pg_doc.get("_id")),
                "name": pg_doc.get("name") or pg_doc.get("pg_name") or "",
                "registration_validation": metrics["pg_registration_validation"],
                "member_registration_validation": metrics["member_registration_validation"],
            },
            "metrics": metrics,
            "updated_at": datetime.utcnow().isoformat(),
    }), 200

@pg_bp.route("/dashboard/all-transactions/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_dashboard_all_transactions(pg_id):
    db = current_app.mongo_db

    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    if not pg_id or not ObjectId.is_valid(str(pg_id)):
        if _wants_json():
            return jsonify({"ok": False, "error": "Valid PG ID is required."}), 400
        flash("Valid PG ID is required.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg_obj_id = ObjectId(str(pg_id))
    pg_doc = db.pgs.find_one({"_id": pg_obj_id})

    if not pg_doc:
        if _wants_json():
            return jsonify({"ok": False, "error": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    role = getattr(g, "role", None) or session.get("role")

    if role == "PG_DATA_ENTRY":
        session_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
        if session_pg_id and session_pg_id != str(pg_id):
            if _wants_json():
                return jsonify({"ok": False, "error": "You cannot access this PG."}), 403
            flash("You cannot access this PG.", "danger")
            return redirect(url_for("pg.pg_home"))

    if role == "CADRE_CC":
        try:
            assigned_pg_ids = set(str(x) for x in _assigned_pg_ids_for_session())
        except Exception:
            assigned_pg_ids = set(str(x) for x in (session.get("assigned_pg_ids") or []))

        if assigned_pg_ids and str(pg_id) not in assigned_pg_ids:
            if _wants_json():
                return jsonify({"ok": False, "error": "This PG is not assigned to you."}), 403
            flash("This PG is not assigned to you.", "danger")
            return redirect(url_for("pg.pg_home"))

    dashboard_cash = _dashboard_cash_metrics(db, pg_obj_id, limit=None)

    transactions = dashboard_cash.get("recent_transactions", [])

    if _wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg_doc.get("_id")),
                "name": pg_doc.get("name") or pg_doc.get("pg_name") or "PG",
            },
            "transactions": transactions,
            "summary": {
                "income_7d": _fmt_inr(dashboard_cash.get("income_7d", 0)),
                "expense_7d": _fmt_inr(dashboard_cash.get("expense_7d", 0)),
                "net_balance": _fmt_inr(dashboard_cash.get("net_balance", 0)),
            },
            "count": len(transactions),
            "updated_at": datetime.utcnow().isoformat(),
        }), 200

    return render_template(
        "dashboard_all_transactions.html",
        pg=pg_doc,
        transactions=transactions,
        income_7d=_fmt_inr(dashboard_cash.get("income_7d", 0)),
        expense_7d=_fmt_inr(dashboard_cash.get("expense_7d", 0)),
        net_balance=_fmt_inr(dashboard_cash.get("net_balance", 0)),
    )

@pg_bp.route("/profile")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "SUPER_ADMIN")
def pg_profile():
    db = current_app.mongo_db

    # If PG user, load their own PG
    pg_id = session.get("pg_id")
    pg_doc = None
    if pg_id:
        try:
            pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
        except Exception:
            pg_doc = None

    # Use your existing template name here:
    # - if your file is profile.html -> keep "profile.html"
    # - if your file is pg_profile.html -> change it
    return render_template("profile.html", pg=pg_doc)


@pg_bp.route("/view/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_view(pg_id):
    """Open a PG dashboard by id.

    - PG_DATA_ENTRY users can only open their own PG.
    - CADRE_CC users can only open assigned PGs.
    - CLF/BLOCK/District/State/Admin users can open PGs within scope.
    - Opening a PG also sets active_pg_id so PG module/sidebar links unlock.
    """
    db = current_app.mongo_db
    role = getattr(g, "role", None) or session.get("role")

    pg_obj_id = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    pg_doc = db.pgs.find_one({"_id": pg_obj_id}) if pg_obj_id else None

    if not pg_doc:
        flash("PG not found.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    current_pg_id = str(pg_doc.get("_id"))

    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        allowed_pg_ids = (
            _assigned_pg_ids_for_session()
            if role == "CADRE_CC"
            else {str(session.get("pg_id") or "")}
        )

        if current_pg_id not in allowed_pg_ids and str(pg_id) not in allowed_pg_ids:
            flash("You cannot access this PG.", "danger")
            return redirect(url_for("pg.pg_home"))

        session["active_pg_id"] = current_pg_id
        session["active_pg_name"] = pg_doc.get("name") or pg_doc.get("pg_name") or pg_doc.get("PG Name") or current_pg_id

        if role == "CADRE_CC":
            session["pg_id"] = current_pg_id

        metrics = _pg_metrics(db, current_pg_id)
        return render_template("dashboard_pg.html", pg=pg_doc, metrics=metrics)

    # Scope check for other roles
    if session.get("clf_id") and str(pg_doc.get("clf_id")) != str(session.get("clf_id")):
        flash("This PG is not under your CLF.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if session.get("block_id") and str(pg_doc.get("block_id")) != str(session.get("block_id")):
        flash("This PG is not under your Block.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if session.get("district_id") and str(pg_doc.get("district_id")) != str(session.get("district_id")):
        flash("This PG is not under your District.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    if session.get("state_id") and str(pg_doc.get("state_id")) != str(session.get("state_id")):
        flash("This PG is not under your State.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    # IMPORTANT FIX:
    # This unlocks PG sidebar/module links for Block/CLF/Admin PG viewing.
    session["active_pg_id"] = current_pg_id
    session["active_pg_name"] = pg_doc.get("name") or pg_doc.get("pg_name") or pg_doc.get("PG Name") or current_pg_id

    metrics = _pg_metrics(db, current_pg_id)
    return render_template("dashboard_pg.html", pg=pg_doc, metrics=metrics)

 

@pg_bp.route("/registration/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_registration(pg_id):

    db = current_app.mongo_db

    # -----------------------------
    # Helpers (Hybrid response)
    # -----------------------------
    def _wants_json():
        if request.headers.get("Authorization"):
            return True
        if request.is_json:
            return True
        accept = request.headers.get("Accept", "")
        if "application/json" in accept.lower():
            return True
        return False

    def _error(message, status=400, redirect_endpoint=None):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), status
        flash(message, "danger" if status >= 400 else "info")
        if redirect_endpoint:
            return redirect(redirect_endpoint)
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    def _info(message, redirect_endpoint=None, payload=None, status=200):
        if _wants_json():
            data = {"ok": True, "message": message}
            if payload:
                data.update(payload)
            return jsonify(data), status
        flash(message, "info")
        if redirect_endpoint:
            return redirect(redirect_endpoint)
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    def _success(message, redirect_endpoint=None, payload=None):
        if _wants_json():
            data = {"ok": True, "message": message}
            if payload:
                data.update(payload)
            return jsonify(data), 200
        flash(message, "success")
        if redirect_endpoint:
            return redirect(redirect_endpoint)
        return redirect(url_for("pg.pg_registration", pg_id=pg_id))

    def _oid_str(x):
        try:
            return str(x) if x is not None else ""
        except Exception:
            return ""

    def _member_shg_key(member_doc):
        """
        Stable SHG key for frontend filtering.
        Prefer SHG Code. If code is missing, fallback to SHG Name.
        """
        if not member_doc:
            return ""
        shg_code = str(
            member_doc.get("SHG Code")
            or member_doc.get("SHG_Code")
            or member_doc.get("shg_code")
            or ""
        ).strip()

        shg_name = str(
            member_doc.get("SHG Name")
            or member_doc.get("SHG_Name")
            or member_doc.get("shg_name")
            or ""
        ).strip()

        return shg_code or shg_name

    def _serialize_pg_for_json(pg_doc):
        def convert(value):
            if isinstance(value, ObjectId):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            try:
                import json
                json.dumps(value)
                return value
            except Exception:
                return str(value)

        return convert(pg_doc or {})

    # -----------------------------
    # Load PG
    # -----------------------------
    pg_obj_id = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if _wants_json():
            return jsonify({"ok": False, "error": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # -----------------------------
    # Role + access
    # -----------------------------
    role = getattr(g, "role", None) or session.get("role")
    session_pg_id = session.get("pg_id")

    if role in ("PG_DATA_ENTRY", "CADRE_CC"):
        if session_pg_id and str(session_pg_id) != str(pg_id):
            return _error("You cannot edit this PG.", 403, redirect_endpoint=url_for("pg.pg_home"))

        if role == "CADRE_CC":
            allowed_pg_ids = _assigned_pg_ids_for_session()
            if str(pg_id) not in allowed_pg_ids:
                return _error("You cannot edit this PG.", 403)

        if not session_pg_id:
            uid = getattr(g, "user_id", None)
            if uid:
                user_doc = db.users.find_one({"_id": safe_objectid(uid) or uid}) or db.users.find_one({"user_id": uid})
                assigned_pg = None
                if user_doc:
                    assigned_pg = user_doc.get("pg_id") or user_doc.get("assigned_pg_id")
                if assigned_pg and str(assigned_pg) != str(pg_id):
                    return _error("You cannot edit this PG.", 403)

    # ----------------------------------------------------------
    # Block validation workflow
    # ----------------------------------------------------------
    registration_validation = _validation_doc(pg, "pg_registration")
    registration_status = str(registration_validation.get("status") or "draft").lower()

    # Important:
    # Previously approved was fully locked.
    # New rule:
    # submitted/resubmitted = locked
    # approved = editable by PG login, but after save it goes back to resubmitted.
    REGISTRATION_TEMP_LOCK_STATUSES = ("submitted", "resubmitted")

    if request.method == "POST":
        if role not in ("PG_DATA_ENTRY", "CADRE_CC"):
            return _error("Only PG login can edit and save the PG Registration Form.", 403)

        if registration_status in REGISTRATION_TEMP_LOCK_STATUSES:
            return _error(
                "PG Registration Form is already submitted for Block Admin approval and cannot be edited until review.",
                403
            )

    # ==========================================================
    # GET
    # ==========================================================
    if request.method == "GET":
        village = pg.get("Village")
        
        # Read-only CLF display for PG Registration page.
        # CLF is shown only after the PG is assigned/mapped to a CLF.
        # This does not affect PG save/approval/validation workflow.
        clf_name = "No CLF assigned"

        clf_id = pg.get("clf_id") or pg.get("CLF_id") or pg.get("clfId")

        if clf_id:
            clf_query_values = [str(clf_id)]

            try:
                if ObjectId.is_valid(str(clf_id)):
                    clf_query_values.append(ObjectId(str(clf_id)))
            except Exception:
                pass

            clf_doc = db.clfs.find_one({"_id": {"$in": clf_query_values}}) or {}

            fetched_clf_name = (
                clf_doc.get("name")
                or clf_doc.get("clf_name")
                or clf_doc.get("CLF Name")
                or clf_doc.get("title")
                or pg.get("clf_name")
                or pg.get("CLF")
                or ""
            )

            if str(fetched_clf_name).strip():
                clf_name = str(fetched_clf_name).strip()

        safe_members = []
        current_pg_member_ids = []

        if pg.get("members"):
            for m in pg["members"]:
                mid = m.get("member_id")
                if mid:
                    current_pg_member_ids.append(mid)

                safe_members.append({
                    "member_id": _oid_str(mid),
                    "member_name": m.get("member_name"),
                    "shg_name": m.get("shg_name"),
                    "shg_code": m.get("shg_code"),
                    "shg_key": m.get("shg_key") or m.get("shg_code") or m.get("shg_name"),
                    "role": m.get("role"),
                })

        used_in_other_pgs = db.pgs.distinct(
            "members.member_id",
            {
                "Village": village,
                "_id": {"$ne": pg["_id"]},
                "members.member_id": {"$exists": True},
            }
        )

        used_in_other_pgs = [x for x in used_in_other_pgs if x not in current_pg_member_ids]

        shg_members = list(db.shg_members_master.find(
            {
                "Village": village,
                "_id": {"$nin": used_in_other_pgs}
            }
        ))

        # ------------------------------------------------------
        # NEW: Build unique SHG list from available village members
        # This powers frontend "Add SHGs" multi-select.
        # ------------------------------------------------------
        shg_options_map = {}

        for m in shg_members:
            shg_code = str(m.get("SHG Code") or m.get("SHG_Code") or "").strip()
            shg_name = str(m.get("SHG Name") or m.get("SHG_Name") or "").strip()

            if not shg_code and not shg_name:
                continue

            shg_key = shg_code or shg_name

            if shg_key not in shg_options_map:
                shg_options_map[shg_key] = {
                    "shg_key": shg_key,
                    "shg_code": shg_code,
                    "shg_name": shg_name,
                    "member_count": 0,
                }

            shg_options_map[shg_key]["member_count"] += 1

        shg_options = sorted(
            shg_options_map.values(),
            key=lambda x: (x.get("shg_name") or "", x.get("shg_code") or "")
        )

        # Existing saved PG members should pre-select their SHGs in edit mode.
        selected_shg_keys = []
        if pg.get("selected_shg_keys"):
            selected_shg_keys = [
                str(x).strip()
                for x in pg.get("selected_shg_keys", [])
                if str(x).strip()
            ]
        else:
            for sm in safe_members:
                shg_key = str(sm.get("shg_code") or sm.get("shg_name") or "").strip()
                if shg_key and shg_key not in selected_shg_keys:
                    selected_shg_keys.append(shg_key)

        if _wants_json():
            try:
                sm = []
                for m in shg_members:
                    sm.append({
                        "_id": _oid_str(m.get("_id")),
                        "Member Name": m.get("Member Name"),
                        "SHG Name": m.get("SHG Name"),
                        "SHG Code": m.get("SHG Code"),
                        "shg_key": _member_shg_key(m),
                        "Village": m.get("Village"),
                    })

                pg_payload = _serialize_pg_for_json(pg)
                pg_payload["clf_name"] = clf_name
                pg_payload["CLF"] = clf_name

                return jsonify({
                    "ok": True,
                    "pg": pg_payload,
                    "clf_name": clf_name,
                    "shg_options": _serialize_pg_for_json(shg_options),
                    "selected_shg_keys": _serialize_pg_for_json(selected_shg_keys),
                    "shg_members": _serialize_pg_for_json(sm),
                    "safe_members": _serialize_pg_for_json(safe_members),
                    "validation": _serialize_pg_for_json(registration_validation),
                    "validation_status": registration_status,
                    "validation_can_edit": registration_status not in REGISTRATION_TEMP_LOCK_STATUSES,
                }), 200
            except Exception as e:
                current_app.logger.exception("Failed to serialize PG registration response")
                return jsonify({
                    "ok": False,
                    "error": f"Failed to load PG registration data: {str(e)}"
                }), 500

        return render_template(
            "pg_registration.html",
            pg=pg,
            clf_name=clf_name,
            shg_options=shg_options,
            selected_shg_keys=selected_shg_keys,
            shg_members=shg_members,
            safe_members=safe_members,
            sector_options=PG_SECTOR_OPTIONS,
            validation=registration_validation,
            validation_status=registration_status,
            validation_remarks=registration_validation.get("remarks") or "",
            validation_can_edit=(registration_status not in REGISTRATION_TEMP_LOCK_STATUSES),
            validation_form_type="pg_registration",
        )

    # ==========================================================
    # POST
    # ==========================================================
    if request.is_json:
        body = request.get_json(silent=True) or {}
        getv = lambda k, default=None: body.get(k, default)

        def getlist(k):
            val = body.get(k, [])
            if isinstance(val, list):
                return val
            if val in (None, ""):
                return []
            return [val]
    else:
        getv = lambda k, default=None: request.form.get(k, default)

        def getlist(k):
            vals = request.form.getlist(k)
            if vals:
                return vals
            if k.endswith("[]"):
                return request.form.getlist(k[:-2])
            return request.form.getlist(k)

    required_fields = {
        "PG Type": getv("pg_type"),
        "Sector": getv("sector"),
        "Formation Date": getv("formation_date"),
        "Contact Number": getv("contact_number"),
        "Bank Name": getv("bank_name"),
        "Branch": getv("branch"),
        "Account Number": getv("account_number"),
        "IFSC": getv("ifsc"),
        "Aggregation Centre Name": getv("agg_centre_name"),
        "Aggregation Centre Address": getv("agg_centre_address"),
    }

    for field_name, value in required_fields.items():
        if not value or str(value).strip() == "":
            return _error(f"{field_name} is required.", 400)

    normalized_sector = _normalize_pg_sector(getv("sector"))
    if normalized_sector not in PG_SECTOR_OPTIONS:
        return _error("Sector must be one of: Agri, ARDD, Fishery.", 400)

    contact_number = str(getv("contact_number", "")).strip()
    if not contact_number.isdigit():
        return _error("Contact number must be numeric.", 400)

    member_ids = getlist("member_ids[]") or getlist("member_ids")
    selected_shg_keys = getlist("selected_shg_keys[]") or getlist("selected_shg_keys")

    selected_shg_keys = [
        str(x).strip()
        for x in selected_shg_keys
        if str(x).strip()
    ]

    # Keep old field names for backend compatibility.
    president_id = getv("president_id")
    secretary_id = getv("secretary_id")
    cashier_id = getv("cashier_id")

    if not member_ids:
        return _error("Please select at least one member.", 400)

    member_ids = [str(mid).strip() for mid in member_ids if str(mid).strip()]

    if len(member_ids) != len(set(member_ids)):
        return _error("Duplicate members detected.", 400)

    try:
        member_object_ids = [ObjectId(mid) for mid in member_ids]
    except Exception:
        return _error("Invalid member selection.", 400)

    if len(member_object_ids) > 40:
        return _error("Maximum 40 members allowed.", 400)

    conflict_pg = db.pgs.find_one(
        {
            "Village": pg.get("Village"),
            "_id": {"$ne": pg["_id"]},
            "members.member_id": {"$in": member_object_ids}
        },
        {"name": 1}
    )
    if conflict_pg:
        return _error(
            f'Some selected members are already assigned to another PG '
            f'("{conflict_pg.get("name", "Unknown")}"). Please remove them and try again.',
            409
        )

    members_from_db = list(db.shg_members_master.find(
        {
            "_id": {"$in": member_object_ids},
            "Village": pg.get("Village")
        }
    ))

    if len(members_from_db) != len(member_object_ids):
        return _error("Some members are invalid or do not belong to this village.", 400)

    # ----------------------------------------------------------
    # NEW: Validate selected members belong to selected SHGs.
    # This only applies if frontend sends selected_shg_keys.
    # Keeps old frontend backward compatible.
    # ----------------------------------------------------------
    if selected_shg_keys:
        selected_shg_key_set = set(selected_shg_keys)
        invalid_shg_members = []

        for m in members_from_db:
            member_shg_key = _member_shg_key(m)
            if member_shg_key not in selected_shg_key_set:
                invalid_shg_members.append(m.get("Member Name") or str(m.get("_id")))

        if invalid_shg_members:
            return _error(
                "Some selected members do not belong to the selected SHG(s). Please refresh and select again.",
                400
            )
    else:
        # Backward compatible fallback:
        # If old frontend does not send selected_shg_keys, derive them from selected members.
        selected_shg_keys = []
        for m in members_from_db:
            member_shg_key = _member_shg_key(m)
            if member_shg_key and member_shg_key not in selected_shg_keys:
                selected_shg_keys.append(member_shg_key)

    if not president_id or not secretary_id or not cashier_id:
        return _error("Please select President, Secretary and Cashier.", 400)

    if president_id not in member_ids or secretary_id not in member_ids or cashier_id not in member_ids:
        return _error("Office bearers must be selected from chosen members.", 400)

    if len({president_id, secretary_id, cashier_id}) != 3:
        return _error("President, Secretary and Cashier must be different members.", 400)

    members_array = []
    role_map = {}
    member_master_by_id = {}

    for m in members_from_db:
        mid_str = str(m["_id"])
        member_master_by_id[mid_str] = m

        role_name = "member"
        if mid_str == str(president_id):
            role_name = "president"
        elif mid_str == str(secretary_id):
            role_name = "secretary"
        elif mid_str == str(cashier_id):
            role_name = "cashier"

        role_map[mid_str] = role_name

        members_array.append({
            "member_id": m["_id"],
            "member_name": m.get("Member Name"),
            "shg_name": m.get("SHG Name"),
            "shg_code": m.get("SHG Code"),
            "shg_key": _member_shg_key(m),
            "role": role_name
        })

    data = {
        "pg_type": getv("pg_type"),
        "sector": normalized_sector,
        "formation_date": getv("formation_date"),
        "office_bearers": {
            "president_id": ObjectId(president_id),
            "secretary_id": ObjectId(secretary_id),
            "cashier_id": ObjectId(cashier_id),
            "contact_number": contact_number,
        },
        "bank_details": {
            "bank_name": getv("bank_name"),
            "branch": getv("branch"),
            "account_number": getv("account_number"),
            "ifsc": getv("ifsc"),
        },
        "aggregation_centre": {
            "name": getv("agg_centre_name"),
            "address": getv("agg_centre_address"),
        },
        "selected_shg_keys": selected_shg_keys,
        "total_members": len(members_array),
        "members": members_array,
        "updated_at": datetime.utcnow(),
    }

    # ----------------------------------------------------------
    # Approved re-edit behavior
    # ----------------------------------------------------------
    is_approved_reapproval_edit = (
        registration_status == "approved"
        and role in ("PG_DATA_ENTRY", "CADRE_CC")
    )

    # Existing locked PG behavior remains, except for approved PG Registration re-approval flow.
    if ensure_pg_locked(pg) and not is_approved_reapproval_edit:
        create_change_request(
            db,
            collection="pgs",
            doc_id=pg["_id"],
            proposed_changes=data,
            user=_current_user_dict(),
            reason="PG is locked after approval; update submitted for approval.",
        )

        add_notification(
            db,
            to_role="ADMIN",
            title="PG update pending approval",
            body=f'PG "{pg.get("name")}" has an update request pending approval.',
            link=url_for("pg.pg_view", pg_id=str(pg["_id"])),
        )

        if _wants_json():
            return jsonify({
                "ok": True,
                "message": "PG is already approved/locked. Changes submitted for approval.",
                "status": "PENDING_APPROVAL",
                "pg_id": str(pg["_id"]),
            }), 202

        flash("PG is already approved/locked. Changes submitted for approval.", "info")
        return redirect(url_for("pg.pg_view", pg_id=pg_id))

    # ==========================================================
    # MEMBERSHIP LIFECYCLE SYNC
    # ==========================================================
    current_user_id = getattr(g, "user_id", None) or session.get("user_id")
    current_user_id = str(current_user_id) if current_user_id else None
    now = datetime.utcnow()

    old_member_ids = set()
    for old_m in (pg.get("members") or []):
        old_mid = old_m.get("member_id")
        if old_mid:
            old_member_ids.add(str(old_mid))

    new_member_ids = set(str(mid) for mid in member_object_ids)

    removed_ids = old_member_ids - new_member_ids
    retained_ids = old_member_ids & new_member_ids
    added_ids = new_member_ids - old_member_ids

    # 1) Removed -> inactive
    if removed_ids:
        removed_oids = []
        for rid in removed_ids:
            try:
                removed_oids.append(ObjectId(rid))
            except Exception:
                pass

        if removed_oids:
            db.pg_members.update_many(
                {"pg_id": pg["_id"], "member_id": {"$in": removed_oids}},
                {
                    "$set": {
                        "is_active": False,
                        "removed_at": now,
                        "removed_by": current_user_id,
                        "updated_at": now,
                    }
                }
            )

    # 2) Retained -> active
    if retained_ids:
        retained_oids = []
        for rid in retained_ids:
            try:
                retained_oids.append(ObjectId(rid))
            except Exception:
                pass

        if retained_oids:
            db.pg_members.update_many(
                {"pg_id": pg["_id"], "member_id": {"$in": retained_oids}},
                {
                    "$set": {
                        "is_active": True,
                        "updated_at": now,
                    },
                    "$unset": {
                        "removed_at": "",
                        "removed_by": ""
                    }
                }
            )

    # 3) Added / Re-added
    for mid_str, master in member_master_by_id.items():
        if mid_str not in added_ids:
            continue

        mid_obj = master["_id"]

        existing_pm = db.pg_members.find_one(
            {"pg_id": pg["_id"], "member_id": mid_obj},
            {"_id": 1, "is_active": 1}
        )

        if existing_pm:
            db.pg_members.update_one(
                {"_id": existing_pm["_id"]},
                {
                    "$set": {
                        "name": master.get("Member Name"),
                        "shg_name": master.get("SHG Name"),
                        "shg_code": master.get("SHG Code"),
                        "shg_key": _member_shg_key(master),
                        "role": role_map.get(mid_str, "member"),
                        "is_active": True,
                        "reactivated_at": now,
                        "updated_at": now,
                    },
                    "$unset": {
                        "removed_at": "",
                        "removed_by": ""
                    }
                }
            )
            continue

        db.pg_members.insert_one({
            "pg_id": pg["_id"],
            "member_id": mid_obj,
            "name": master.get("Member Name"),
            "shg_name": master.get("SHG Name"),
            "shg_code": master.get("SHG Code"),
            "shg_key": _member_shg_key(master),
            "role": role_map.get(mid_str, "member"),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
            "reactivated_at": None
        })

    # 4) Sync selected member basics
    for mid_str, master in member_master_by_id.items():
        try:
            mid_obj = master["_id"]
        except Exception:
            continue

        db.pg_members.update_one(
            {"pg_id": pg["_id"], "member_id": mid_obj},
            {
                "$set": {
                    "name": master.get("Member Name"),
                    "shg_name": master.get("SHG Name"),
                    "shg_code": master.get("SHG Code"),
                    "shg_key": _member_shg_key(master),
                    "role": role_map.get(mid_str, "member"),
                    "is_active": True,
                    "updated_at": now,
                },
                "$unset": {
                    "removed_at": "",
                    "removed_by": ""
                }
            },
            upsert=False
        )

    db.pgs.update_one({"_id": pg["_id"]}, {"$set": data})

    # ----------------------------------------------------------
    # Validation status update after save
    # ----------------------------------------------------------

    # If Block had rejected the form, saving correction should keep it editable
    # and still marked as rejected until PG submits again.
    # This allows submit-validation to mark the next submission as resubmitted.
    if registration_status == "rejected":
        db.pgs.update_one(
            {"_id": pg["_id"]},
            {"$set": {
                "registration_validation.corrected_at": datetime.utcnow(),
                "registration_validation.corrected_by": _current_user_id_value(),
                "updated_at": datetime.utcnow(),
            }}
        )

    # NEW:
    # If already approved and PG edits it again,
    # send it back to Block Admin as resubmitted.
    if registration_status == "approved":
        db.pgs.update_one(
            {"_id": pg["_id"]},
            {"$set": {
                "registration_validation.status": "resubmitted",
                "registration_validation.resubmitted_at": datetime.utcnow(),
                "registration_validation.resubmitted_by": _current_user_id_value(),
                "registration_validation.remarks": "",
                "updated_at": datetime.utcnow(),
            }}
        )

        add_notification(
            db,
            to_role="BLOCK_ADMIN",
            title="PG Registration resubmitted",
            body=f'PG "{pg.get("name")}" has edited an approved registration and resubmitted it for Block approval.',
            link=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
        )

    log_audit(
        db,
        action="update",
        collection="pgs",
        doc_id=pg["_id"],
        user=_current_user_dict(),
        after=data
    )

    if registration_status == "approved":
        return _success(
            "PG registration updated and resubmitted for Block Admin approval.",
            payload={
                "pg_id": str(pg["_id"]),
                "validation_status": "resubmitted"
            }
        )

    return _success("PG registration details updated.", payload={"pg_id": str(pg["_id"])})



@pg_bp.route("/submit/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN")
@require_unlocked_period(scope='pg')
def pg_submit_for_authorization(pg_id):
    """After data entry, submit PG for state authorization."""
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})

    #  MOBILE FIX: helper to detect mobile/JSON requests
    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    if not pg:
        if _wants_json():
            return jsonify({"ok": False, "error": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    #  MOBILE FIX: read role from g (JWT) with session fallback
    role       = getattr(g, "role",   None) or session.get("role")
    token_pg   = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")

    if role == "PG_DATA_ENTRY" and token_pg and token_pg != pg_id:
        if _wants_json():
            return jsonify({"ok": False, "error": "You cannot submit this PG."}), 403
        flash("You cannot submit this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    _set_pg_status(db, pg_id, "submitted", {"submitted_at": datetime.utcnow(), "submitted_by": _current_user_dict().get("user_id")})

    if _wants_json():
        return jsonify({"ok": True, "message": "PG submitted for State Authorization.", "pg_id": str(pg["_id"])})
    flash("PG submitted for State Authorization.", "success")
    return redirect(url_for("pg.pg_registration", pg_id=pg_id))


@pg_bp.route("/authorization", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_authorization():
    """State Authorization step.

    ADMIN (State Admin) sees only PGs under their state.
    SUPER_ADMIN can see all.
    """
    db = current_app.mongo_db
    state_id = session.get("state_id")

    if request.method == "POST":
        pg_id = request.form.get("pg_id")
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip()
        if not pg_id or action not in ("approve", "reject"):
            flash("Invalid request.", "danger")
            return redirect(url_for("pg.pg_authorization"))

        extra = {"authorized_at": datetime.utcnow(), "authorized_by": session.get("user_id")}
        if action == "approve":
            _set_pg_status(db, pg_id, "active", extra)
            flash("PG authorized and activated.", "success")
        else:
            _set_pg_status(db, pg_id, "rejected", {**extra, "rejection_reason": reason or None})
            flash("PG rejected.", "warning")

        return redirect(url_for("pg.pg_authorization"))

    q = {"status": "submitted"}
    if state_id and session.get("role") == "ADMIN":
        q["state_id"] = ObjectId(state_id)
    pgs = list(db.pgs.find(q).sort("submitted_at", -1))
    return render_template("pg_authorization.html", pgs=pgs)


# PG Members Management in membership register

@pg_bp.route("/members/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def pg_members(pg_id):
    print("pg_members HIT | pg_id =", pg_id)
    db = current_app.mongo_db

    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    def _deny(message, status=400):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), status
        flash(message, "danger" if status >= 400 else "info")
        return redirect(url_for("pg.pg_members", pg_id=pg_id))

    try:
        pg_obj_id = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    except Exception:
        if _wants_json():
            return jsonify({"ok": False, "error": "Invalid PG ID."}), 400
        flash("Invalid PG ID.", "danger")
        return redirect(url_for("pg.pg_home"))

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        if _wants_json():
            return jsonify({"ok": False, "error": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    # ----------------------------------------------------------
    # Membership validation workflow guard
    # submitted/resubmitted = locked
    # approved = editable again row-wise
    # ----------------------------------------------------------
    role = getattr(g, "role", None) or session.get("role")
    member_validation = _validation_doc(pg, "member_registration")
    member_validation_status = str(member_validation.get("status") or "draft").lower()

    if request.method == "POST":
        if role not in ("PG_DATA_ENTRY", "CADRE_CC"):
            return _deny("Only PG login can edit and save the Membership Registration Form.", 403)

        # IMPORTANT:
        # Do not lock approved here.
        # Approved must allow PG to edit saved rows or complete blank rows.
        if member_validation_status in ("submitted", "resubmitted"):
            return _deny(
                "Membership Registration Form is already submitted and cannot be edited until Block Admin review.",
                403
            )

    # Members selected during PG Registration
    selected = pg.get("members") or []
    selected_ids = []
    selected_id_set = set()

    for m in selected:
        mid = m.get("member_id")
        if not mid:
            continue
        try:
            mid_obj = ObjectId(mid) if not isinstance(mid, ObjectId) else mid
            selected_ids.append(mid_obj)
            selected_id_set.add(str(mid_obj))
        except Exception:
            pass

    # Load master records for these selected members
    master_by_id = {}
    if selected_ids:
        for doc in db.shg_members_master.find({"_id": {"$in": selected_ids}}):
            master_by_id[str(doc["_id"])] = doc

    # Load existing active PG member docs
    existing_by_member = {}
    for doc in db.pg_members.find({
        "pg_id": pg_obj_id,
        "$or": [
            {"is_active": True},
            {"is_active": {"$exists": False}}
        ]
    }):
        key = str(doc.get("member_id") or doc.get("_id"))
        existing_by_member[key] = doc

    def normalize_key(k: str) -> str:
        if not isinstance(k, str):
            return ""
        return (
            k.strip()
            .lower()
            .replace("_", " ")
            .replace("-", " ")
            .replace(".", "")
        )

    def pick(master_doc, *keys):
        if not master_doc:
            return None

        for k in keys:
            if k in master_doc:
                v = master_doc.get(k)
                if v not in (None, "", []):
                    return v

        norm_map = {}
        for mk, mv in master_doc.items():
            nk = normalize_key(mk)
            if nk and nk not in norm_map:
                norm_map[nk] = mv

        for k in keys:
            nk = normalize_key(k)
            if nk in norm_map:
                v = norm_map.get(nk)
                if v not in (None, "", []):
                    return v

        for mk, mv in master_doc.items():
            if mv in (None, "", []):
                continue
            mkn = normalize_key(mk)
            if any(normalize_key(x) in mkn for x in keys):
                return mv

        return None

    def _member_details_saved(doc, sector_name):
        """
        Row-wise saved detection.
        Master data alone should not mark a member row as saved.
        A row is saved only when required editable fields are present.
        """
        if not doc:
            return False

        required_common = [
            "contact",
            "bank_name",
            "branch",
            "account_number",
            "membership_fee_paid",
        ]

        for key in required_common:
            value = doc.get(key)
            if value in (None, "", []):
                return False

        sector_name = _normalize_pg_sector(sector_name)

        if sector_name == "Agri":
            return bool(doc.get("agri_crop") and doc.get("agri_ffs_module"))

        if sector_name == "ARDD":
            return bool(doc.get("ardd_activity") and doc.get("ardd_unit"))

        if sector_name == "Fishery":
            return bool(doc.get("fishery_activity"))

        return True

    # ============================================================
    # Individual row-wise save
    # ============================================================
    if request.method == "POST":
        _is_mobile = _wants_json()

        if _is_mobile:
            _body = request.get_json(silent=True) or {}
            _get = lambda k, default="": (_body.get(k) or default)
        else:
            _get = lambda k, default="": (request.form.get(k) or default)

        mid = _get("member_id", "").strip()
        if not mid:
            return _deny("Member ID missing.", 400)

        try:
            mid_obj = ObjectId(mid)
        except Exception:
            return _deny("Invalid Member ID.", 400)

        # Save allowed only if member is still selected in current PG Registration
        if str(mid_obj) not in selected_id_set:
            return _deny("This member is no longer active in the current PG selection.", 403)

        existing_doc = db.pg_members.find_one({
            "pg_id": pg_obj_id,
            "member_id": mid_obj,
            "$or": [
                {"is_active": True},
                {"is_active": {"$exists": False}}
            ]
        }) or {}

        inactive_doc = db.pg_members.find_one({
            "pg_id": pg_obj_id,
            "member_id": mid_obj,
            "is_active": False
        })

        if inactive_doc and not existing_doc:
            return _deny("This member is inactive and cannot be edited until re-added in PG Registration.", 403)

        master = master_by_id.get(mid)
        current_sector = _normalize_pg_sector(pg.get("sector"))

        agri_crop = _get("agri_crop", "").strip()
        agri_ffs_module = _get("agri_ffs_module", "").strip()
        ardd_activity = _get("ardd_activity", "").strip()
        ardd_unit = _get("ardd_unit", "").strip()
        fishery_activity = _get("fishery_activity", "").strip()

        if current_sector == "Agri":
            if agri_crop not in AGRI_CROP_OPTIONS:
                return _deny("Please select a valid Agri crop.", 400)
            if agri_ffs_module not in AGRI_FFS_MODULE_OPTIONS:
                return _deny("Please select a valid FFS Module.", 400)

        elif current_sector == "ARDD":
            if ardd_activity not in ARDD_ACTIVITY_OPTIONS:
                return _deny("Please select a valid ARDD activity.", 400)
            if ardd_unit not in ARDD_UNIT_OPTIONS.get(ardd_activity, []):
                return _deny("Please select a valid ARDD unit.", 400)

        elif current_sector == "Fishery":
            if fishery_activity not in FISHERY_ACTIVITY_OPTIONS:
                return _deny("Please select a valid Fishery activity.", 400)

        contact = _get("contact", "").strip()
        bank_name = _get("bank_name", "").strip()
        branch = _get("branch", "").strip()
        account_number = _get("account_number", "").strip()
        membership_fee_raw = _get("membership_fee_paid", "").strip()
        lakh_raw = _get("lakh_pati_didi", "").strip().lower()

        membership_fee_paid = None
        if membership_fee_raw != "":
            try:
                membership_fee_paid = float(membership_fee_raw)
            except Exception:
                membership_fee_paid = None

        update_set = {
            "pg_id": pg_obj_id,
            "member_id": mid_obj,

            "name": pick(master, "Member Name", "Name", "Member_Name"),
            "spouse_name": pick(
                master,
                "Father/Mother/Spouse Name",
                "Spouse Name",
                "Spouse/Husband Name",
                "Husband Name",
                "Father/Husband Name",
                "Father Name",
                "Mother Name"
            ),
            "category": pick(master, "Category", "Caste Category", "Social Category"),
            "shg_name": pick(master, "SHG Name", "SHG_Name"),
            "shg_code": pick(master, "SHG Code", "SHG_Code"),

            "is_active": True,
            "removed_at": None,
            "removed_by": None,

            "updated_at": datetime.utcnow(),
        }

        # Do not overwrite old values with blank values
        if contact != "":
            update_set["contact"] = contact
        elif not existing_doc.get("contact"):
            update_set["contact"] = pick(master, "Contact Number", "Mobile", "Mobile No", "Phone")

        if bank_name != "":
            update_set["bank_name"] = bank_name

        if branch != "":
            update_set["branch"] = branch

        if account_number != "":
            update_set["account_number"] = account_number

        if membership_fee_paid is not None:
            update_set["membership_fee_paid"] = membership_fee_paid

        update_set["lakh_pati_didi"] = True if lakh_raw in ("1", "true", "on", "yes") else False

        update_set["agri_crop"] = agri_crop if current_sector == "Agri" else ""
        update_set["agri_ffs_module"] = agri_ffs_module if current_sector == "Agri" else ""
        update_set["ardd_activity"] = ardd_activity if current_sector == "ARDD" else ""
        update_set["ardd_unit"] = ardd_unit if current_sector == "ARDD" else ""
        update_set["fishery_activity"] = fishery_activity if current_sector == "Fishery" else ""

        db.pg_members.update_one(
            {"pg_id": pg_obj_id, "member_id": mid_obj},
            {
                "$set": update_set,
                "$setOnInsert": {
                    "created_at": datetime.utcnow(),
                    "reactivated_at": None
                }
            },
            upsert=True
        )

        # Rejected correction returns to draft
        if member_validation_status == "rejected":
            db.pgs.update_one(
                {"_id": pg_obj_id},
                {"$set": {
                    "member_registration_validation.status": "draft",
                    "member_registration_validation.corrected_at": datetime.utcnow(),
                    "member_registration_validation.corrected_by": _current_user_id_value(),
                    "updated_at": datetime.utcnow(),
                }}
            )

        # Approved edit returns to resubmitted
        if member_validation_status == "approved":
            db.pgs.update_one(
                {"_id": pg_obj_id},
                {"$set": {
                    "member_registration_validation.status": "resubmitted",
                    "member_registration_validation.resubmitted_at": datetime.utcnow(),
                    "member_registration_validation.resubmitted_by": _current_user_id_value(),
                    "member_registration_validation.remarks": "",
                    "updated_at": datetime.utcnow(),
                }}
            )

        if _is_mobile:
            return jsonify({"ok": True, "message": "Member details saved successfully."}), 200

        flash("Member details saved successfully.", "success")
        return redirect(url_for("pg.pg_members", pg_id=pg_id))

    # ============================================================
    # Build rows in same order as PG Registration
    # ============================================================
    rows = []
    current_sector = _normalize_pg_sector(pg.get("sector"))
    page_locked = member_validation_status in ("submitted", "resubmitted")

    for m in selected:
        mid = m.get("member_id")
        if not mid:
            continue

        mid_str = str(mid)
        master = master_by_id.get(mid_str)
        existing = existing_by_member.get(mid_str) or {}

        details_saved = _member_details_saved(existing, current_sector)

        # submitted/resubmitted = locked all rows
        # approved/draft/rejected = row-wise behavior
        row_locked = page_locked
        row_readonly = True if details_saved else False

        spouse_val = (
            existing.get("spouse_name")
            or pick(
                master,
                "Father/Mother/Spouse Name",
                "Spouse Name",
                "Spouse/Husband Name",
                "Husband Name",
                "Father/Husband Name",
                "Father Name",
                "Mother Name"
            )
        )

        rows.append({
            "member_id": mid_str,
            "name": existing.get("name") or pick(master, "Member Name", "Name", "Member_Name") or m.get("member_name"),
            "spouse_name": spouse_val,
            "category": existing.get("category") or pick(master, "Category", "Caste Category", "Social Category"),
            "shg_name": existing.get("shg_name") or pick(master, "SHG Name", "SHG_Name") or m.get("shg_name"),

            "details_saved": details_saved,
            "row_locked": row_locked,
            "row_readonly": row_readonly,

            "contact": existing.get("contact") or pick(master, "Contact Number", "Mobile", "Mobile No", "Phone"),
            "bank_name": existing.get("bank_name") or "",
            "branch": existing.get("branch") or "",
            "account_number": existing.get("account_number") or "",
            "membership_fee_paid": existing.get("membership_fee_paid") if existing.get("membership_fee_paid") is not None else "",
            "lakh_pati_didi": True if existing.get("lakh_pati_didi") else False,

            "agri_crop": existing.get("agri_crop") or "",
            "agri_ffs_module": existing.get("agri_ffs_module") or "",
            "ardd_activity": existing.get("ardd_activity") or "",
            "ardd_unit": existing.get("ardd_unit") or "",
            "fishery_activity": existing.get("fishery_activity") or "",
        })

    if _wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "name": pg.get("name"),
                "sector": current_sector,
            },
            "sector_meta": {
                "sectors": PG_SECTOR_OPTIONS,
                "agri_crops": AGRI_CROP_OPTIONS,
                "agri_ffs_modules": AGRI_FFS_MODULE_OPTIONS,
                "ardd_activities": ARDD_ACTIVITY_OPTIONS,
                "ardd_units": ARDD_UNIT_OPTIONS,
                "fishery_activities": FISHERY_ACTIVITY_OPTIONS,
            },
            "rows": rows,
            "validation": {
                "status": str(member_validation.get("status") or "draft").lower(),
                "remarks": member_validation.get("remarks") or "",
                "submitted_at": member_validation.get("submitted_at").isoformat() if isinstance(member_validation.get("submitted_at"), datetime) else member_validation.get("submitted_at"),
                "reviewed_at": member_validation.get("reviewed_at").isoformat() if isinstance(member_validation.get("reviewed_at"), datetime) else member_validation.get("reviewed_at"),
            },
            "validation_status": member_validation_status,
            "validation_can_edit": member_validation_status not in ("submitted", "resubmitted"),
        }), 200

    return render_template(
        "pg_members.html",
        pg=pg,
        rows=rows,
        current_sector=current_sector,
        agri_crops=AGRI_CROP_OPTIONS,
        agri_ffs_modules=AGRI_FFS_MODULE_OPTIONS,
        ardd_activities=ARDD_ACTIVITY_OPTIONS,
        ardd_unit_options=ARDD_UNIT_OPTIONS,
        fishery_activities=FISHERY_ACTIVITY_OPTIONS,
        validation=member_validation,
        validation_status=member_validation_status,
        validation_remarks=member_validation.get("remarks") or "",
        validation_can_edit=(member_validation_status not in ("submitted", "resubmitted")),
        validation_form_type="member_registration",
    )




def _add_six_months(dt):
    if not isinstance(dt, datetime):
        return None

    month = dt.month + 6
    year = dt.year + ((month - 1) // 12)
    month = ((month - 1) % 12) + 1

    day = min(dt.day, 28)

    try:
        return dt.replace(year=year, month=month, day=day)
    except Exception:
        return dt + timedelta(days=180)


def _parse_pg_formation_date(pg_doc):
    formation_date = pg_doc.get("formation_date")

    if isinstance(formation_date, datetime):
        return formation_date

    if isinstance(formation_date, str) and formation_date.strip():
        try:
            return datetime.strptime(formation_date.strip(), "%Y-%m-%d")
        except Exception:
            pass

    return datetime.utcnow()




# ============================================================
# AGRI CROP MANAGEMENT / CROP PLANNING
# ============================================================

@pg_bp.route("/crop-management/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN","CADRE_CC")
def crop_management(pg_id):
    db = current_app.mongo_db
    pg_doc = _load_pg_or_404(db, pg_id)

    sector = (pg_doc.get("sector") or pg_doc.get("pg_sector") or "").strip()

    if sector.lower() != "agri":
        flash("Crop Management is currently available only for Agri PGs.", "warning")
        return redirect(request.referrer or url_for("pg.pg_home"))

    role = session.get("role") or ""

    if role == "PG_DATA_ENTRY":
        base_template = "base.html"
    elif role in ["CLF_MANAGER", "CLF_ADMIN"]:
        base_template = "base_clf.html"
    elif role == "CADRE_CC":
        base_template = "base_cadre.html"
    else:
        base_template = "base.html"

    return render_template(
        "crop_management.html",
        base_template=base_template,
        pg=pg_doc,
        pg_id=str(pg_doc["_id"]),
        agri_ffs_modules=AGRI_FFS_MODULE_OPTIONS,
        agri_crops=AGRI_CROP_OPTIONS,
    )



@pg_bp.route("/api/crop-management/<pg_id>/members", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN","CADRE_CC")
def api_crop_management_members(pg_id):
    db = current_app.mongo_db
    pg_doc = _load_pg_or_404(db, pg_id)

    sector = (pg_doc.get("sector") or pg_doc.get("pg_sector") or "").strip()
    if sector.lower() != "agri":
        return jsonify({
            "ok": False,
            "message": "Crop Management is currently available only for Agri PGs.",
            "members": []
        }), 400

    ffs_module = (request.args.get("ffs_module") or "").strip()

    if not ffs_module:
        return jsonify({
            "ok": False,
            "message": "Please select FFS Module.",
            "members": []
        }), 400

    members_cursor = db.pg_members.find({
        "pg_id": pg_doc["_id"],
        "is_active": True,
        "agri_ffs_module": ffs_module
    }).sort("name", 1)

    pg_first_cycle_date = _parse_pg_formation_date(pg_doc)

    members = []
    for m in members_cursor:
        first_cycle_date = m.get("first_crop_cycle_date") or pg_first_cycle_date
        current_cycle_start = m.get("crop_cycle_start") or first_cycle_date
        next_crop_cycle_date = m.get("next_crop_cycle_date") or m.get("crop_cycle_end") or _add_six_months(current_cycle_start)

        members.append({
            "member_pg_id": str(m.get("_id")),
            "member_id": str(m.get("member_id")) if m.get("member_id") else "",
            "name": m.get("name") or "",
            "spouse_name": m.get("spouse_name") or "",
            "shg_name": m.get("shg_name") or "",
            "shg_code": m.get("shg_code") or "",
            "role": m.get("role") or "",
            "current_ffs_module": m.get("agri_ffs_module") or "",
            "current_crop": m.get("agri_crop") or "",
            "crop_cycle_no": int(m.get("crop_cycle_no") or 1),

            "first_crop_cycle_date": first_cycle_date.strftime("%Y-%m-%d") if isinstance(first_cycle_date, datetime) else "",
            "crop_cycle_start": current_cycle_start.strftime("%Y-%m-%d") if isinstance(current_cycle_start, datetime) else "",
            "next_crop_cycle_date": next_crop_cycle_date.strftime("%Y-%m-%d") if isinstance(next_crop_cycle_date, datetime) else "",
        })


    return jsonify({
        "ok": True,
        "message": "Members loaded successfully.",
        "pg_id": str(pg_doc["_id"]),
        "ffs_module": ffs_module,
        "members": members,
        "ffs_modules": AGRI_FFS_MODULE_OPTIONS,
        "crops": AGRI_CROP_OPTIONS,
    })


@pg_bp.route("/api/crop-management/<pg_id>/save", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN","CADRE_CC")
@require_unlocked_period(scope="pg")
def api_crop_management_save(pg_id):
    db = current_app.mongo_db
    pg_doc = _load_pg_or_404(db, pg_id)

    sector = (pg_doc.get("sector") or pg_doc.get("pg_sector") or "").strip()
    if sector.lower() != "agri":
        return jsonify({
            "ok": False,
            "message": "Crop Management is currently available only for Agri PGs."
        }), 400

    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or []

    if not isinstance(rows, list) or not rows:
        return jsonify({
            "ok": False,
            "message": "No member crop changes received."
        }), 400

    cycle_start_raw = (payload.get("cycle_start") or "").strip()
    cycle_end_raw = (payload.get("cycle_end") or "").strip()

    now = datetime.utcnow()

    try:
        if cycle_start_raw:
            cycle_start = datetime.strptime(cycle_start_raw, "%Y-%m-%d")
        else:
            cycle_start = now

        if cycle_end_raw:
            cycle_end = datetime.strptime(cycle_end_raw, "%Y-%m-%d")
        else:
            cycle_end = _add_six_months(cycle_start)
    except Exception:
        return jsonify({
            "ok": False,
            "message": "Invalid cycle start or cycle end date."
        }), 400

    user_oid = safe_objectid(session.get("user_id"))
    user_role = session.get("role") or session.get("user_role") or ""

    changed_count = 0
    skipped_count = 0
    history_docs = []

    for row in rows:
        member_pg_id_raw = row.get("member_pg_id") or row.get("_id")
        member_pg_oid = safe_objectid(member_pg_id_raw)

        if not member_pg_oid:
            skipped_count += 1
            continue

        new_ffs_module = (row.get("new_ffs_module") or "").strip()
        new_crop = (row.get("new_crop") or "").strip()

        if not new_ffs_module or not new_crop:
            skipped_count += 1
            continue

        if new_ffs_module not in AGRI_FFS_MODULE_OPTIONS:
            skipped_count += 1
            continue

        if new_crop not in AGRI_CROP_OPTIONS:
            skipped_count += 1
            continue

        member_doc = db.pg_members.find_one({
            "_id": member_pg_oid,
            "pg_id": pg_doc["_id"],
            "is_active": True
        })

        if not member_doc:
            skipped_count += 1
            continue

        old_ffs_module = member_doc.get("agri_ffs_module") or ""
        old_crop = member_doc.get("agri_crop") or ""

        # Skip if nothing changed
        if old_ffs_module == new_ffs_module and old_crop == new_crop:
            skipped_count += 1
            continue

        next_cycle_no = int(member_doc.get("crop_cycle_no") or 1) + 1

        history_docs.append({
            "pg_id": pg_doc["_id"],
            "member_pg_id": member_doc["_id"],
            "member_id": member_doc.get("member_id"),

            "member_name": member_doc.get("name") or "",
            "spouse_name": member_doc.get("spouse_name") or "",
            "shg_name": member_doc.get("shg_name") or "",
            "shg_code": member_doc.get("shg_code") or "",

            "sector": "Agri",

            "previous_ffs_module": old_ffs_module,
            "previous_crop": old_crop,

            "new_ffs_module": new_ffs_module,
            "new_crop": new_crop,

            "cycle_no": next_cycle_no,
            "first_crop_cycle_date": member_doc.get("first_crop_cycle_date") or _parse_pg_formation_date(pg_doc),
            "cycle_start": cycle_start,
            "cycle_end": cycle_end,
            "current_cycle_start": cycle_start,
            "next_crop_cycle_date": cycle_end,

            "created_by": user_oid,
            "created_by_role": user_role,
            "created_at": now,
        })

        db.pg_members.update_one(
            {
                "_id": member_doc["_id"],
                "pg_id": pg_doc["_id"]
            },
            {
                "$set": {
                    "agri_ffs_module": new_ffs_module,
                    "agri_crop": new_crop,

                    "first_crop_cycle_date": member_doc.get("first_crop_cycle_date") or _parse_pg_formation_date(pg_doc),
                    "crop_cycle_no": next_cycle_no,
                    "crop_cycle_start": cycle_start,
                    "crop_cycle_end": cycle_end,
                    "next_crop_cycle_date": cycle_end,

                    "crop_cycle_updated_at": now,
                    "crop_cycle_updated_by": user_oid,

                    "updated_at": now,
                }
            }
        )

        changed_count += 1

    if history_docs:
        db.pg_crop_cycles.insert_many(history_docs)

    try:
        log_audit(
            "crop_management_save",
            "pg_members",
            str(pg_doc["_id"]),
            {
                "changed_count": changed_count,
                "skipped_count": skipped_count,
                "sector": "Agri",
            }
        )
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "message": f"Crop cycle updated for {changed_count} member(s).",
        "changed_count": changed_count,
        "skipped_count": skipped_count,
    })








@pg_bp.route("/lakhpati/<pg_id>")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_lakhpati(pg_id):
    """List active members marked as Lakhpati Didi for a PG (web + app)."""
    db = current_app.mongo_db

    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    def _deny(message, status=400):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), status
        flash(message, "danger" if status >= 400 else "warning")
        return redirect(url_for("pg.pg_home"))

    # Defensive: links can be generated with pg_id=None if session scope is missing.
    if not pg_id or str(pg_id).lower() == "none":
        pg_id = getattr(g, "pg_id", None) or session.get("pg_id") or session.get("active_pg_id")

    if not pg_id or not ObjectId.is_valid(str(pg_id)):
        return _deny("PG scope not found. Please open a PG first or re-login.", 400)

    pg_obj_id = ObjectId(str(pg_id))
    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        return _deny("PG not found.", 404)

    # Scope restriction for PG user (hybrid: g first, then session)
    role = getattr(g, "role", None) or session.get("role")
    auth_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")

    if role == "PG_DATA_ENTRY" and auth_pg_id != str(pg_id):
        return _deny("You cannot access this PG.", 403)

    # Only ACTIVE lakhpati members
    member_docs = list(
        db.pg_members.find(
            {
                "pg_id": pg_obj_id,
                "lakh_pati_didi": True,
                "$or": [
                    {"is_active": True},
                    {"is_active": {"$exists": False}}
                ]
            }
        ).sort([("name", 1)])
    )

    if _wants_json():
        rows = []
        for m in member_docs:
            rows.append({
                "_id": str(m.get("_id")),
                "pg_id": str(m.get("pg_id")) if m.get("pg_id") else "",
                "member_id": str(m.get("member_id")) if m.get("member_id") else "",
                "name": m.get("name", ""),
                "spouse_name": m.get("spouse_name", ""),
                "category": m.get("category", ""),
                "shg_name": m.get("shg_name", ""),
                "shg_code": m.get("shg_code", ""),
                "contact": m.get("contact", ""),
                "photo_id_number": m.get("photo_id_number", ""),
                "bank_name": m.get("bank_name", ""),
                "branch": m.get("branch", ""),
                "account_number": m.get("account_number", ""),
                "membership_fee_paid": m.get("membership_fee_paid", ""),
                "lakh_pati_didi": True,
                "is_active": m.get("is_active", True),
            })

        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "name": pg.get("name", ""),
            },
            "rows": rows,
        }), 200

    return render_template("lakhpati_didi.html", pg=pg, members=member_docs)

@pg_bp.route("/export/<pg_id>/summary.csv")
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def export_pg_summary_csv(pg_id):
    """Download PG summary (members + key KPIs) as CSV."""
    import csv
    from io import StringIO
    db = current_app.mongo_db
    if not pg_id or str(pg_id).lower() == "none" or not ObjectId.is_valid(str(pg_id)):
        abort(400)

    pg = db.pgs.find_one({"_id": ObjectId(str(pg_id))})
    if not pg:
        abort(404)

    if session.get("role") == "PG_DATA_ENTRY" and str(session.get("pg_id")) != str(pg_id):
        abort(403)

    # KPI snapshot
    kpi = _pg_metrics(db, pg_id)

    # Members
    mems = list(db.pg_members.find({"pg_id": ObjectId(str(pg_id))}).sort([("name", 1)]))

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["PG_ID", "PG_NAME", "TURNOVER_LATEST", "PROFIT_LOSS_LATEST", "GRANTS_RECEIVED", "LAKHPATI_DIDI_COUNT"])
    w.writerow([
        str(pg.get("_id")),
        pg.get("name") or "",
        kpi.get("turnover_latest") or "₹0",
        kpi.get("profit_latest") or "₹0",
        kpi.get("grants_received_total") or "₹0",
        kpi.get("lakhpati_count") or 0,
    ])
    w.writerow([])
    w.writerow(["MEMBER_NAME", "SPOUSE_NAME", "CATEGORY", "SHG_NAME", "CONTACT", "PHOTO_ID", "BANK", "BRANCH", "ACCOUNT", "FEE_PAID", "LAKHPATI_DIDI"])
    for m in mems:
        w.writerow([
            m.get("name") or "",
            m.get("spouse_name") or "",
            m.get("category") or "",
            m.get("shg_name") or "",
            m.get("contact") or "",
            m.get("photo_id_number") or "",
            m.get("bank_name") or "",
            m.get("branch") or "",
            m.get("account_number") or "",
            "Yes" if m.get("membership_fee_paid") else "No",
            "Yes" if m.get("lakh_pati_didi") else "No",
        ])

    from flask import Response
    filename = f"pg_{pg_id}_summary.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@pg_bp.route("/submit/<pg_id>", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN")
@require_unlocked_period(scope='pg')
def submit_for_authorization(pg_id):
    """Submit a PG for state authorization.

    Workflow alignment:
    - CLF/PG user completes registration.
    - Then submits to the State Admin for authorization.
    """
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})

    #  MOBILE FIX: helper to detect mobile/JSON requests
    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    if not pg:
        if _wants_json():
            return jsonify({"ok": False, "error": "PG not found."}), 404
        flash("PG not found.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    # PG_DATA_ENTRY can only submit their own PG.
    #  MOBILE FIX: read role/pg_id from g (JWT) with session fallback
    role     = getattr(g, "role",   None) or session.get("role")
    token_pg = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")

    if role == "PG_DATA_ENTRY" and token_pg and token_pg != pg_id:
        if _wants_json():
            return jsonify({"ok": False, "error": "You cannot submit this PG."}), 403
        flash("You cannot submit this PG.", "danger")
        return redirect(url_for("pg.pg_home"))

    _set_pg_status(db, pg_id, "submitted", {
        "submitted_at": datetime.utcnow(),
        "submitted_by": _current_user_dict().get("user_id"),
    })

    if _wants_json():
        return jsonify({"ok": True, "message": "Submitted to State for authorization.", "pg_id": str(pg["_id"])})
    flash("Submitted to State for authorization.", "success")
    return redirect(url_for("pg.pg_registration", pg_id=pg_id))


@pg_bp.route("/authorization", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
@require_unlocked_period(scope='pg')
def state_authorization():
    """State Admin: approve/reject PG registrations."""
    db = current_app.mongo_db
    state_id = session.get("state_id")
    if not state_id:
        flash("No state scope found for your account.", "danger")
        return redirect(url_for("reports.state_dashboard"))

    if request.method == "POST":
        pg_id = request.form.get("pg_id")
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip() or None
        pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))), "state_id": ObjectId(state_id)})
        if not pg:
            flash("PG not found in your state.", "danger")
            return redirect(url_for("pg.state_authorization"))

        if action == "approve":
            _set_pg_status(db, pg_id, "active", {
                "authorized_at": datetime.utcnow(),
                "authorized_by": session.get("user_id"),
                "rejection_reason": None,
            })
            flash("PG authorized and activated.", "success")
        elif action == "reject":
            _set_pg_status(db, pg_id, "rejected", {
                "authorized_at": datetime.utcnow(),
                "authorized_by": session.get("user_id"),
                "rejection_reason": reason,
            })
            flash("PG rejected.", "warning")
        else:
            flash("Invalid action.", "danger")

        return redirect(url_for("pg.state_authorization"))

    submitted = list(db.pgs.find({
        "state_id": ObjectId(state_id),
        "status": {"$in": ["submitted", "rejected", "active", "draft"]},
    }).sort([("updated_at", -1)]).limit(300))

    return render_template("pg_authorization.html", pgs=submitted)


@pg_bp.route("/upload_document/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN", "CRCTA", "ANS", "CRCITARD")
@require_unlocked_period(scope='pg')
def pg_upload_document(pg_id):
    db = current_app.mongo_db
    pg = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.pg_home"))

    if request.method == "POST":
        f = request.files.get("document")
        doc_type = request.form.get("doc_type") or "registration_form"
        if not f or not f.filename:
            flash("Please choose a file.", "warning")
            return redirect(url_for("pg.pg_upload_document", pg_id=pg_id))

        doc = save_uploaded_document(current_app, pg_id=pg_id, file_storage=f, doc_type=doc_type, user=_current_user_dict())
        flash("Document uploaded successfully.", "success")
        return redirect(url_for("pg.pg_upload_document", pg_id=pg_id))

    docs = list(db.pg_documents.find({"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}).sort("uploaded_at", -1))
    return render_template("pg_upload_document.html", pg=pg, docs=docs)

# ----------------------------
# PG Registers (UI menu pages)
# ----------------------------

@pg_bp.route('/registers/meeting-minutes')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def meeting_minute_book():
    #  MOBILE FIX: use g.pg_id (JWT) with session fallback
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "meeting-minutes"})
    return render_template('meeting_minute_book.html', pg_id=pg_id)



@pg_bp.route('/registers/meeting-minutes/history')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def meeting_minutes_history():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')

    if request.headers.get("Authorization") or request.is_json:
        return jsonify({
            "ok": True,
            "pg_id": str(pg_id or ""),
            "register": "meeting-minutes-history"
        })

    return render_template('meeting_minutes_history.html', pg_id=pg_id)


@pg_bp.route('/registers/member-ledger')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def member_ledger():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "member-ledger"})
    return render_template('member_ledger.html', pg_id=pg_id)

# ============================================================
# Loan Ledger
# Final flow:
# - CLF/Block/Admin create member loans in pg_member_loan_accounts
# - PG login only records repayment/register entries in pg_loan_ledgers
# - Loan Ledger always connects to pg_member_loan_accounts._id
# ============================================================

def _ledger_json_safe(value):
    """Make Mongo/ObjectId/datetime values JSON safe."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_ledger_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _ledger_json_safe(v) for k, v in value.items()}
    return value


def _ledger_float(value, default=0.0):
    try:
        if value in (None, "", "null", "None"):
            return default
        return float(value)
    except Exception:
        return default


def _ledger_int(value, default=0):
    try:
        if value in (None, "", "null", "None"):
            return default
        return int(float(value))
    except Exception:
        return default


def _ledger_pick_number(row, *keys):
    if not isinstance(row, dict):
        return 0.0
    for key in keys:
        if key in row and row.get(key) not in (None, "", "null"):
            return _ledger_float(row.get(key))
    return 0.0


def _ledger_member_name(member_doc):
    if not member_doc:
        return ""
    return (
        member_doc.get("name")
        or member_doc.get("member_name")
        or member_doc.get("Member Name")
        or ""
    )


def _ledger_member_guardian(member_doc):
    if not member_doc:
        return ""
    return (
        member_doc.get("spouse_name")
        or member_doc.get("father_mother_spouse")
        or member_doc.get("Father/Mother/Spouse Name")
        or member_doc.get("Spouse Name")
        or member_doc.get("Father Name")
        or member_doc.get("Mother Name")
        or ""
    )


def _ledger_member_lookup(db, loan_doc):
    member_id = loan_doc.get("member_id")
    member_doc = None

    if member_id:
        try:
            member_oid = member_id if isinstance(member_id, ObjectId) else ObjectId(str(member_id))
            member_doc = db.pg_members.find_one({"_id": member_oid}) or {}
        except Exception:
            member_doc = {}

    return member_doc or {}


def _ledger_loan_amount(loan_doc):
    return _ledger_float(
        loan_doc.get("principal")
        or loan_doc.get("loan_amount")
        or loan_doc.get("principal_amount")
        or loan_doc.get("amount")
        or 0
    )


def _ledger_schedule_json(schedule):
    rows = []
    for item in (schedule or []):
        if not isinstance(item, dict):
            continue

        rows.append({
            "instalment_no": _ledger_int(item.get("instalment_no") or item.get("installment_no")),
            "due_date": _ledger_json_safe(item.get("due_date")),
            "emi": _ledger_float(item.get("emi")),
            "principal": _ledger_float(item.get("principal")),
            "interest": _ledger_float(item.get("interest")),
            "balance": _ledger_float(item.get("balance")),
            "is_paid": bool(item.get("is_paid")),
            "paid_at": _ledger_json_safe(item.get("paid_at")),
            "paid_amount": _ledger_float(item.get("paid_amount")),
            "principal_paid": _ledger_float(item.get("principal_paid")),
            "interest_paid": _ledger_float(item.get("interest_paid")),
        })

    return rows


def _ledger_next_unpaid_installment(schedule):
    """
    Finds next unpaid installment from member loan schedule.
    Used by frontend Add Row so PG does not calculate manually.
    """
    schedule = schedule or []

    for item in schedule:
        if not isinstance(item, dict):
            continue

        if not item.get("is_paid"):
            principal_due = _ledger_float(item.get("principal"))
            interest_due = _ledger_float(item.get("interest"))
            total_due = principal_due + interest_due

            return {
                "instalment_no": _ledger_int(item.get("instalment_no") or item.get("installment_no")),
                "date_disb": _ledger_json_safe(item.get("due_date")),
                "due_date": _ledger_json_safe(item.get("due_date")),
                "principal_due": round(principal_due, 2),
                "principal_repaid": 0,
                "interest_due": round(interest_due, 2),
                "interest_paid": 0,
                "total_due": round(total_due, 2),
                "next_installment_date": _ledger_json_safe(item.get("due_date")),
                "schedule_balance": _ledger_float(item.get("balance")),
            }

    return None


def _serialize_member_loan_for_ledger(db, loan_doc):
    member_doc = _ledger_member_lookup(db, loan_doc)

    principal = _ledger_loan_amount(loan_doc)
    outstanding = loan_doc.get("outstanding_amount")
    if outstanding in (None, ""):
        outstanding = principal

    schedule = loan_doc.get("schedule") or []
    installments_total = (
        _ledger_int(loan_doc.get("installments_total"))
        or len(schedule)
        or _ledger_int(loan_doc.get("tenure_months"))
    )

    return {
        "_id": str(loan_doc.get("_id")),
        "loan_id": str(loan_doc.get("_id")),
        "loan_no": loan_doc.get("loan_no") or "",
        "pg_id": str(loan_doc.get("pg_id")) if loan_doc.get("pg_id") else "",
        "member_id": str(loan_doc.get("member_id")) if loan_doc.get("member_id") else "",

        "member_name": _ledger_member_name(member_doc),
        "father_mother_spouse": _ledger_member_guardian(member_doc),

        "loan_amount": principal,
        "principal": principal,
        "purpose": loan_doc.get("purpose") or loan_doc.get("loan_purpose") or "",
        "loan_purpose": loan_doc.get("purpose") or loan_doc.get("loan_purpose") or "",

        "roi": _ledger_float(loan_doc.get("roi")),
        "tenure_months": _ledger_int(loan_doc.get("tenure_months")),
        "no_of_installments": installments_total,

        "status": (loan_doc.get("status") or "active").lower(),

        "principal_repaid": _ledger_float(loan_doc.get("principal_repaid")),
        "interest_paid": _ledger_float(loan_doc.get("interest_paid")),
        "total_paid": _ledger_float(loan_doc.get("total_paid")),

        "principal_outstanding": _ledger_float(
            loan_doc.get("principal_outstanding")
            if loan_doc.get("principal_outstanding") is not None
            else outstanding
        ),
        "interest_due": _ledger_float(loan_doc.get("interest_due")),
        "outstanding_amount": _ledger_float(outstanding),
        "overdue_amount": _ledger_float(loan_doc.get("overdue_amount")),

        "installments_total": installments_total,
        "installments_paid": _ledger_int(loan_doc.get("installments_paid")),
        "installments_pending": _ledger_int(loan_doc.get("installments_pending")),

        "next_due_date": _ledger_json_safe(loan_doc.get("next_due_date")),
        "schedule": _ledger_schedule_json(schedule),
        "next_unpaid_installment": _ledger_next_unpaid_installment(schedule),

        "created_at": _ledger_json_safe(loan_doc.get("created_at")),
        "updated_at": _ledger_json_safe(loan_doc.get("updated_at")),
    }



def _ledger_payload_totals(payload):
    """
    Reads repayment entries from Loan Ledger payload/document.

    Supports:
    - entries: [...]
    - rows: [...]
    - top-level principal/interest fields

    Also supports schedule-linked fields:
    - instalment_no / installment_no
    """
    payload = payload or {}

    rows = payload.get("entries")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    if not isinstance(rows, list):
        rows = []

    total_principal_due = 0.0
    total_principal_repaid = 0.0
    total_interest_due = 0.0
    total_interest_paid = 0.0
    paid_entry_count = 0

    paid_installment_numbers = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        principal_due = _ledger_pick_number(
            row,
            "principal_due",
            "principalDue",
            "principal_due_for_month",
            "principalDueForMonth",
            "pr_due"
        )
        principal_repaid = _ledger_pick_number(
            row,
            "principal_repaid",
            "principalRepaid",
            "principal_paid",
            "principalPaid",
            "pr_paid"
        )
        interest_due = _ledger_pick_number(
            row,
            "interest_due",
            "interestDue",
            "int_due"
        )
        interest_paid = _ledger_pick_number(
            row,
            "interest_paid",
            "interestPaid",
            "int_paid"
        )

        installment_no = _ledger_int(
            row.get("instalment_no")
            or row.get("installment_no")
            or row.get("inst_no")
        )

        total_principal_due += principal_due
        total_principal_repaid += principal_repaid
        total_interest_due += interest_due
        total_interest_paid += interest_paid

        if principal_repaid > 0 or interest_paid > 0:
            paid_entry_count += 1
            if installment_no:
                paid_installment_numbers.add(installment_no)

    top_principal_due = _ledger_pick_number(
        payload,
        "principal_due",
        "principalDue",
        "principal_due_for_month",
        "principalDueForMonth",
        "pr_due"
    )
    top_principal_repaid = _ledger_pick_number(
        payload,
        "principal_repaid",
        "principalRepaid",
        "principal_paid",
        "principalPaid",
        "pr_paid"
    )
    top_interest_due = _ledger_pick_number(
        payload,
        "interest_due",
        "interestDue",
        "int_due"
    )
    top_interest_paid = _ledger_pick_number(
        payload,
        "interest_paid",
        "interestPaid",
        "int_paid"
    )

    total_principal_due += top_principal_due
    total_principal_repaid += top_principal_repaid
    total_interest_due += top_interest_due
    total_interest_paid += top_interest_paid

    if top_principal_repaid > 0 or top_interest_paid > 0:
        paid_entry_count += 1

    return {
        "principal_due": round(total_principal_due, 2),
        "principal_repaid": round(total_principal_repaid, 2),
        "interest_due": round(total_interest_due, 2),
        "interest_paid": round(total_interest_paid, 2),
        "paid_entry_count": int(paid_entry_count),
        "paid_installment_numbers": sorted(list(paid_installment_numbers)),
    }




def _recalculate_member_loan_from_ledgers(db, *, pg_oid, loan_doc):
    """
    Recalculates member loan summary from saved Loan Ledger records.

    Also syncs the schedule:
    - marks installment paid when repayment is fully paid
    - keeps partially paid installment open
    - updates next_due_date from next unpaid schedule row
    """
    loan_id = str(loan_doc["_id"])
    principal = _ledger_loan_amount(loan_doc)

    ledger_docs = list(db.pg_loan_ledgers.find({
        "pg_id": pg_oid,
        "loan_id": loan_id,
        "loan_type": "member"
    }))

    principal_due_total = 0.0
    principal_repaid_total = 0.0
    interest_due_total = 0.0
    interest_paid_total = 0.0
    paid_entries_total = 0

    # installment_no wise paid amount
    installment_paid_map = {}

    for ledger_doc in ledger_docs:
        totals = _ledger_payload_totals(ledger_doc)

        principal_due_total += totals["principal_due"]
        principal_repaid_total += totals["principal_repaid"]
        interest_due_total += totals["interest_due"]
        interest_paid_total += totals["interest_paid"]
        paid_entries_total += totals["paid_entry_count"]

        rows = ledger_doc.get("entries")
        if not isinstance(rows, list):
            rows = ledger_doc.get("rows")
        if not isinstance(rows, list):
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            inst_no = _ledger_int(
                row.get("instalment_no")
                or row.get("installment_no")
                or row.get("inst_no")
            )

            if not inst_no:
                continue

            principal_paid = _ledger_pick_number(
                row,
                "principal_repaid",
                "principalRepaid",
                "principal_paid",
                "principalPaid",
                "pr_paid"
            )
            interest_paid = _ledger_pick_number(
                row,
                "interest_paid",
                "interestPaid",
                "int_paid"
            )

            if inst_no not in installment_paid_map:
                installment_paid_map[inst_no] = {
                    "principal_paid": 0.0,
                    "interest_paid": 0.0,
                    "paid_amount": 0.0,
                    "paid_at": None,
                }

            installment_paid_map[inst_no]["principal_paid"] += principal_paid
            installment_paid_map[inst_no]["interest_paid"] += interest_paid
            installment_paid_map[inst_no]["paid_amount"] += principal_paid + interest_paid

            paid_date = (
                row.get("paid_at")
                or row.get("payment_date")
                or row.get("date_disb")
                or row.get("disbursement_date")
            )
            if paid_date:
                installment_paid_map[inst_no]["paid_at"] = paid_date

    schedule = loan_doc.get("schedule") or []
    updated_schedule = []
    installments_paid_from_schedule = 0
    next_due_date = None
    overdue_amount = 0.0

    now = datetime.utcnow()

    for item in schedule:
        if not isinstance(item, dict):
            continue

        inst_no = _ledger_int(item.get("instalment_no") or item.get("installment_no"))
        due_principal = _ledger_float(item.get("principal"))
        due_interest = _ledger_float(item.get("interest"))
        emi = _ledger_float(item.get("emi")) or (due_principal + due_interest)

        paid_info = installment_paid_map.get(inst_no, {})
        principal_paid = round(_ledger_float(paid_info.get("principal_paid")), 2)
        interest_paid = round(_ledger_float(paid_info.get("interest_paid")), 2)
        paid_amount = round(principal_paid + interest_paid, 2)

        full_due = round(due_principal + due_interest, 2)
        is_paid = paid_amount >= full_due and full_due > 0

        new_item = dict(item)
        new_item["principal_paid"] = principal_paid
        new_item["interest_paid"] = interest_paid
        new_item["paid_amount"] = paid_amount
        new_item["is_paid"] = bool(is_paid)

        if paid_info.get("paid_at"):
            new_item["paid_at"] = paid_info.get("paid_at")
        elif is_paid and not new_item.get("paid_at"):
            new_item["paid_at"] = now

        if is_paid:
            installments_paid_from_schedule += 1
        else:
            if next_due_date is None:
                next_due_date = new_item.get("due_date")

            due_date = new_item.get("due_date")
            try:
                if due_date and due_date <= now:
                    overdue_amount += max(emi - paid_amount, 0)
            except Exception:
                pass

        updated_schedule.append(new_item)

    principal_outstanding = max(principal - principal_repaid_total, 0)
    interest_outstanding = max(interest_due_total - interest_paid_total, 0)
    outstanding_amount = principal_outstanding + interest_outstanding
    total_paid = principal_repaid_total + interest_paid_total

    installments_total = (
        _ledger_int(loan_doc.get("installments_total"))
        or len(updated_schedule)
        or _ledger_int(loan_doc.get("tenure_months"))
    )

    if updated_schedule:
        installments_paid = installments_paid_from_schedule
    else:
        installments_paid = min(paid_entries_total, installments_total) if installments_total else paid_entries_total

    installments_pending = max(installments_total - installments_paid, 0) if installments_total else 0

    old_status = (loan_doc.get("status") or "active").lower()
    if outstanding_amount <= 0 and principal > 0:
        new_status = "closed"
    else:
        new_status = "active" if old_status == "closed" else old_status

    update_doc = {
        "principal_due": round(principal_due_total, 2),
        "principal_repaid": round(principal_repaid_total, 2),

        # This stores current unpaid interest amount
        "interest_due": round(interest_outstanding, 2),
        "interest_paid": round(interest_paid_total, 2),

        "total_paid": round(total_paid, 2),
        "principal_outstanding": round(principal_outstanding, 2),
        "outstanding_amount": round(outstanding_amount, 2),
        "overdue_amount": round(overdue_amount, 2),

        "installments_total": installments_total,
        "installments_paid": installments_paid,
        "installments_pending": installments_pending,

        "next_due_date": next_due_date,
        "schedule": updated_schedule,

        "status": new_status,
        "updated_at": datetime.utcnow(),
    }

    db.pg_member_loan_accounts.update_one(
        {"_id": loan_doc["_id"]},
        {"$set": update_doc}
    )

    refreshed = db.pg_member_loan_accounts.find_one({"_id": loan_doc["_id"]}) or {}
    return refreshed



def _get_member_loan_for_ledger_or_404(db, *, pg_oid, loan_id):
    loan_oid = safe_objectid(loan_id)
    if not loan_oid:
        abort(400, "Invalid member loan ID")

    loan_doc = db.pg_member_loan_accounts.find_one({
        "_id": loan_oid,
        "pg_id": pg_oid
    })

    if not loan_doc:
        abort(404, "Member loan account not found for this PG")

    return loan_doc


@pg_bp.route('/registers/loan-ledger')
@login_required
@roles_required('PG_DATA_ENTRY', 'CADRE_CC', 'CLF_MANAGER', 'CLF_ADMIN', 'BLOCK_ADMIN', 'DISTRICT_ADMIN', 'ADMIN', 'SUPER_ADMIN')
def loan_ledger():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')

    if request.headers.get("Authorization") or request.is_json:
        return jsonify({
            "ok": True,
            "pg_id": str(pg_id or ""),
            "register": "loan-ledger",
            "mode": "member-loan-repayment-ledger"
        })

    return render_template('loan_ledger.html', pg_id=pg_id)


@pg_bp.route("/loan-ledger/member-loans", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_loan_ledger_member_loans():
    """
    Used by Loan Ledger frontend.

    Returns only member loans created by CLF/Block/Admin from pg_member_loan_accounts.
    PG login can select from this list and save repayments in Loan Ledger.
    """
    db = current_app.mongo_db
    pg_id = _ctx_pg_id()

    if not pg_id:
        abort(400, "Missing PG context")

    pg_doc = _load_pg_or_404(db, pg_id)
    pg_oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))

    include_closed = request.args.get("include_closed") in ("1", "true", "yes")

    loan_query = {"pg_id": pg_oid}
    if not include_closed:
        loan_query["status"] = {"$ne": "closed"}

    loans_raw = list(
        db.pg_member_loan_accounts
        .find(loan_query)
        .sort([("created_at", -1), ("updated_at", -1)])
    )

    loans = [_serialize_member_loan_for_ledger(db, loan) for loan in loans_raw]

    return jsonify({
        "ok": True,
        "pg": {
            "_id": str(pg_doc.get("_id")),
            "name": pg_doc.get("name") or pg_doc.get("pg_name") or "",
        },
        "loans": loans,
        "can_create_member_loan": False,
        "can_save_ledger": (getattr(g, "role", None) or session.get("role")) == "PG_DATA_ENTRY",
    })





# ============================================================
# NEW SECTOR-BASED LOAN LEDGER WORKFLOW
# PG sector → Member → Crop/Activity/Unit → Repayment Ledger
# Collections:
#   1) pg_sector_member_loan_accounts
#   2) pg_sector_loan_ledgers
# ============================================================

SECTOR_LEDGER_ACCOUNT_COLL = "pg_sector_member_loan_accounts"
SECTOR_LEDGER_COLL = "pg_sector_loan_ledgers"

SECTOR_INTEREST_RATE = {
    "Agri": 0.00375,      # based on Agri sheet pattern
    "Fishery": 0.00275,   # based on Fishery sheet pattern
    "ARDD": 0.00250,      # based on ARDD sheet pattern
}

SECTOR_MORATORIUM_MONTHS = {
    "Agri": 4,      # first 4 months no repayment
    "Fishery": 1,   # first month no repayment
    "ARDD": 0,      # ARDD can have multiple loan releases month-wise
}


def _sll_now():
    return datetime.utcnow()


def _sll_role():
    return getattr(g, "role", None) or session.get("role")


def _sll_wants_json():
    return bool(
        request.headers.get("Authorization")
        or request.is_json
        or "application/json" in request.headers.get("Accept", "").lower()
    )


def _sll_float(value, default=0.0):
    try:
        if value in (None, "", "null", "None", "-", "NaN"):
            return default
        return float(str(value).replace("₹", "").replace(",", "").strip())
    except Exception:
        return default


def _sll_int(value, default=0):
    try:
        if value in (None, "", "null", "None", "-", "NaN"):
            return default
        return int(float(value))
    except Exception:
        return default


def _sll_str(value):
    return str(value or "").strip()


def _sll_oid(value):
    return safe_objectid(value) or None


def _sll_json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [_sll_json_safe(v) for v in value]

    if isinstance(value, dict):
        return {k: _sll_json_safe(v) for k, v in value.items()}

    return value


def _sll_pg_id():
    return (
        getattr(g, "pg_id", None)
        or session.get("pg_id")
        or session.get("active_pg_id")
        or request.args.get("pg_id")
        or request.headers.get("X-PG-ID")
    )


def _sll_load_pg_or_404(db):
    pg_id = _sll_pg_id()

    if not pg_id:
        abort(400, "Missing PG context.")

    pg_oid = safe_objectid(pg_id)

    if not pg_oid:
        abort(400, "Invalid PG context.")

    pg_doc = db.pgs.find_one({"_id": pg_oid})

    if not pg_doc:
        abort(404, "PG not found.")

    role = _sll_role()

    # PG login can only access own PG.
    if role == "PG_DATA_ENTRY":
        login_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
        if login_pg_id and login_pg_id != str(pg_oid):
            abort(403, "You cannot access this PG.")

    # CADRE users can access only assigned PGs, if assignment list exists.
    if role == "CADRE_CC":
        assigned_pg_ids = session.get("assigned_pg_ids") or []
        assigned_pg_ids = {str(x) for x in assigned_pg_ids}
        if assigned_pg_ids and str(pg_oid) not in assigned_pg_ids:
            abort(403, "This PG is not assigned to you.")

    return pg_doc, pg_oid


def _sll_pg_name(pg_doc):
    return (
        pg_doc.get("name")
        or pg_doc.get("pg_name")
        or pg_doc.get("PG Name")
        or ""
    )


def _sll_pg_sector(pg_doc):
    return _normalize_pg_sector(
        pg_doc.get("sector")
        or pg_doc.get("Sector")
        or pg_doc.get("pg_sector")
        or ""
    )


def _sll_member_name(member_doc):
    return (
        member_doc.get("name")
        or member_doc.get("member_name")
        or member_doc.get("Member Name")
        or ""
    )


def _sll_member_guardian(member_doc):
    return (
        member_doc.get("spouse_name")
        or member_doc.get("father_mother_spouse")
        or member_doc.get("Father/Mother/Spouse Name")
        or member_doc.get("Spouse Name")
        or member_doc.get("Father Name")
        or member_doc.get("Mother Name")
        or ""
    )


def _sll_member_contact(member_doc):
    return (
        member_doc.get("contact")
        or member_doc.get("Contact Number")
        or member_doc.get("Mobile")
        or member_doc.get("Mobile No")
        or ""
    )


def _sll_member_id_from_doc(member_doc):
    return str(
        member_doc.get("member_id")
        or member_doc.get("_id")
        or ""
    )


def _sll_member_query(pg_oid, member_id):
    mid_oid = safe_objectid(member_id)

    if mid_oid:
        return {
            "pg_id": pg_oid,
            "$or": [
                {"_id": mid_oid},
                {"member_id": mid_oid},
                {"member_id": str(mid_oid)},
            ]
        }

    return {
        "pg_id": pg_oid,
        "$or": [
            {"member_id": member_id},
            {"_id": member_id},
        ]
    }


def _sll_load_member_or_404(db, pg_oid, member_id):
    if not member_id:
        abort(400, "Member is required.")

    member_doc = db.pg_members.find_one(_sll_member_query(pg_oid, member_id))

    if not member_doc:
        abort(404, "Member not found under this PG.")

    if member_doc.get("is_active") is False:
        abort(400, "This member is inactive.")

    return member_doc


def _sll_pick_list(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x or "").strip()]

    if isinstance(value, str):
        # support comma-separated old values if any
        return [x.strip() for x in value.split(",") if x.strip()]

    return []


def _sll_member_sector_items(member_doc, sector):
    """
    Returns member-linked crop/activity/unit for selected PG sector.

    Agri:
      item_type = crop
      item_name = agri_crop / crop / crops[]

    Fishery:
      item_type = fishery_activity
      item_name = fishery_activity

    ARDD:
      item_type = ardd_unit
      item_name = GPU/GFU/PPU/PFU
      activity = Goatery/Piggery
    """

    items = []

    if sector == "Agri":
        crops = []

        for key in (
            "agri_crop",
            "crop",
            "crop_name",
            "selected_crop",
            "crops",
            "agri_crops",
        ):
            val = member_doc.get(key)
            crops.extend(_sll_pick_list(val))

        # remove duplicate crops
        clean = []
        for crop in crops:
            if crop and crop not in clean:
                clean.append(crop)

        for crop in clean:
            items.append({
                "item_key": f"agri::{crop}",
                "item_type": "crop",
                "item_name": crop,
                "label": crop,
                "activity": "",
                "unit": "",
            })

    elif sector == "Fishery":
        activities = []

        for key in (
            "fishery_activity",
            "activity",
            "fishery_type",
            "fishery_activities",
        ):
            val = member_doc.get(key)
            activities.extend(_sll_pick_list(val))

        clean = []
        for activity in activities:
            if activity and activity not in clean:
                clean.append(activity)

        for activity in clean:
            items.append({
                "item_key": f"fishery::{activity}",
                "item_type": "fishery_activity",
                "item_name": activity,
                "label": activity,
                "activity": activity,
                "unit": "",
            })

    elif sector == "ARDD":
        ardd_activity = (
            member_doc.get("ardd_activity")
            or member_doc.get("activity")
            or member_doc.get("livestock_activity")
            or ""
        )

        ardd_units = []

        for key in (
            "ardd_unit",
            "unit",
            "unit_type",
            "ardd_units",
        ):
            val = member_doc.get(key)
            ardd_units.extend(_sll_pick_list(val))

        clean_units = []
        for unit in ardd_units:
            if unit and unit not in clean_units:
                clean_units.append(unit)

        for unit in clean_units:
            items.append({
                "item_key": f"ardd::{ardd_activity}::{unit}",
                "item_type": "ardd_unit",
                "item_name": unit,
                "label": f"{ardd_activity} - {unit}" if ardd_activity else unit,
                "activity": ardd_activity,
                "unit": unit,
            })

    return items


def _sll_parse_item_key(sector, item_key):
    raw = _sll_str(item_key)

    if not raw:
        abort(400, "Crop/activity/unit is required.")

    parts = raw.split("::")

    if sector == "Agri":
        if len(parts) >= 2 and parts[0] == "agri":
            return {
                "item_key": raw,
                "item_type": "crop",
                "item_name": parts[1],
                "activity": "",
                "unit": "",
            }

        return {
            "item_key": f"agri::{raw}",
            "item_type": "crop",
            "item_name": raw,
            "activity": "",
            "unit": "",
        }

    if sector == "Fishery":
        if len(parts) >= 2 and parts[0] == "fishery":
            return {
                "item_key": raw,
                "item_type": "fishery_activity",
                "item_name": parts[1],
                "activity": parts[1],
                "unit": "",
            }

        return {
            "item_key": f"fishery::{raw}",
            "item_type": "fishery_activity",
            "item_name": raw,
            "activity": raw,
            "unit": "",
        }

    if sector == "ARDD":
        if len(parts) >= 3 and parts[0] == "ardd":
            return {
                "item_key": raw,
                "item_type": "ardd_unit",
                "item_name": parts[2],
                "activity": parts[1],
                "unit": parts[2],
            }

        return {
            "item_key": f"ardd::::{raw}",
            "item_type": "ardd_unit",
            "item_name": raw,
            "activity": "",
            "unit": raw,
        }

    abort(400, "Invalid PG sector.")


def _sll_validate_item_for_member(member_doc, sector, item_key):
    item = _sll_parse_item_key(sector, item_key)
    allowed_items = _sll_member_sector_items(member_doc, sector)
    allowed_keys = {x.get("item_key") for x in allowed_items}

    if allowed_keys and item["item_key"] not in allowed_keys:
        abort(400, "Selected crop/activity/unit is not linked with this member.")

    return item


def _sll_account_key(pg_oid, member_doc, sector, item):
    member_id = _sll_member_id_from_doc(member_doc)

    return {
        "pg_id": pg_oid,
        "member_id": member_id,
        "sector": sector,
        "item_key": item["item_key"],
    }


def _sll_find_account(db, pg_oid, member_doc, sector, item, loan_account_id=None):
    query = _sll_account_key(pg_oid, member_doc, sector, item)
    if loan_account_id:
        account_oid = safe_objectid(loan_account_id)
        if not account_oid:
            return None
        query["_id"] = account_oid
        return db[SECTOR_LEDGER_ACCOUNT_COLL].find_one(query)

    current = db[SECTOR_LEDGER_ACCOUNT_COLL].find_one({**query, "is_current": True})
    if current:
        return current

    # Legacy accounts predate cycle tracking. If there are several records,
    # load the newest one as the current cycle.
    return db[SECTOR_LEDGER_ACCOUNT_COLL].find_one(
        query,
        sort=[("loan_cycle_no", -1), ("created_at", -1), ("updated_at", -1)]
    )


def _sll_find_existing_member_loan_source(db, pg_oid, member_doc, item=None):
    item = item or {}

    ids = []
    for val in (
        member_doc.get("_id"),
        member_doc.get("member_id"),
        str(member_doc.get("_id") or ""),
        str(member_doc.get("member_id") or ""),
    ):
        if val and val not in ids:
            ids.append(val)
        oid = safe_objectid(val)
        if oid and oid not in ids:
            ids.append(oid)

    q = {
        "pg_id": pg_oid,
        "member_id": {"$in": ids},
        "status": {"$ne": "closed"},
    }

    item_name = str(item.get("item_name") or "").strip().lower()
    activity = str(item.get("activity") or "").strip().lower()
    unit = str(item.get("unit") or "").strip().lower()

    loans = list(
        db.pg_member_loan_accounts
        .find(q)
        .sort([("created_at", -1), ("updated_at", -1)])
    )

    if not loans:
        return None

    def score(loan):
        text = " ".join([
            str(loan.get("purpose") or ""),
            str(loan.get("loan_purpose") or ""),
            str(loan.get("loan_no") or ""),
            str(loan.get("sector") or ""),
            str(loan.get("crop") or ""),
            str(loan.get("activity") or ""),
            str(loan.get("unit") or ""),
        ]).lower()

        s = 0
        if item_name and item_name in text:
            s += 20
        if activity and activity in text:
            s += 10
        if unit and unit in text:
            s += 10
        return s

    loans.sort(key=score, reverse=True)
    return loans[0]




def _sll_create_or_update_account(db, pg_doc, pg_oid, member_doc, sector, item, payload=None, force_new=False):
    payload = payload or {}
    now = _sll_now()

    q = _sll_account_key(pg_oid, member_doc, sector, item)
    existing = None if force_new else _sll_find_account(db, pg_oid, member_doc, sector, item)

    source_loan = _sll_find_existing_member_loan_source(db, pg_oid, member_doc, item)

    source_principal = 0.0
    source_purpose = ""
    source_start_date = ""
    source_loan_id = ""

    if source_loan:
        source_principal = _sll_float(
            source_loan.get("principal")
            or source_loan.get("loan_amount")
            or source_loan.get("principal_amount")
            or source_loan.get("amount")
            or 0
        )
        source_purpose = source_loan.get("purpose") or source_loan.get("loan_purpose") or ""
        source_start_date = _sll_str(
            source_loan.get("start_date")
            or source_loan.get("loan_start_date")
            or source_loan.get("created_at")
            or ""
        )[:10]
        source_loan_id = str(source_loan.get("_id") or "")

    incoming_loan_amount = payload.get("loan_amount")
    incoming_status = payload.get("status")
    incoming_start_date = payload.get("loan_start_date")
    incoming_purpose = payload.get("loan_purpose")

    final_loan_amount = (
        _sll_float(incoming_loan_amount)
        if incoming_loan_amount not in (None, "")
        else _sll_float(existing.get("loan_amount")) if existing else source_principal
    )
    final_purpose = (
        _sll_str(incoming_purpose)
        if incoming_purpose not in (None, "")
        else (existing.get("loan_purpose") if existing else "") or source_purpose
    )
    final_start_date = (
        _sll_str(incoming_start_date)
        if incoming_start_date not in (None, "")
        else (existing.get("loan_start_date") if existing else "") or source_start_date
    )

    related_accounts = list(db[SECTOR_LEDGER_ACCOUNT_COLL].find(q, {"loan_cycle_no": 1, "is_current": 1}))
    next_cycle_no = max([_sll_int(row.get("loan_cycle_no"), 1) for row in related_accounts] or [0]) + 1
    is_new_cycle = force_new or not existing

    set_doc = {
        "pg_id": pg_oid,
        "pg_name": _sll_pg_name(pg_doc),
        "member_id": q["member_id"],
        "member_name": _sll_member_name(member_doc),
        "father_mother_spouse": _sll_member_guardian(member_doc),
        "sector": sector,
        "item_key": item["item_key"],
        "item_type": item["item_type"],
        "item_name": item["item_name"],
        "activity": item.get("activity") or "",
        "unit": item.get("unit") or "",
        "source_member_loan_id": source_loan_id,
        "loan_amount": final_loan_amount,
        "loan_purpose": final_purpose,
        "loan_start_date": final_start_date,
        "interest_rate": SECTOR_INTEREST_RATE.get(sector, 0),
        "moratorium_months": SECTOR_MORATORIUM_MONTHS.get(sector, 0),
        "status": _sll_str(incoming_status or (existing.get("status") if existing else "active")).lower(),
        "updated_at": now,
    }

    if is_new_cycle:
        # Keep each completed loan as its own account so its repayment rows
        # remain unchanged when the member receives a later loan.
        db[SECTOR_LEDGER_ACCOUNT_COLL].update_many(q, {"$set": {"is_current": False}})
        set_doc.update({
            "loan_cycle_no": next_cycle_no,
            "is_current": True,
            "status": "active",
            "summary": {},
            "outstanding_amount": final_loan_amount,
            "principal_paid": 0.0,
            "interest_paid": 0.0,
            "total_repayment": 0.0,
            "created_at": now,
        })
        res = db[SECTOR_LEDGER_ACCOUNT_COLL].insert_one(set_doc)
        set_doc["_id"] = res.inserted_id
        return set_doc

    set_doc.update({
        "loan_cycle_no": _sll_int(existing.get("loan_cycle_no"), 1),
        "is_current": True,
    })
    db[SECTOR_LEDGER_ACCOUNT_COLL].update_one({"_id": existing["_id"]}, {"$set": set_doc})
    existing.update(set_doc)
    return existing





def _sll_account_json(account):
    if not account:
        return {}

    principal = _sll_float(account.get("loan_amount"))
    summary = account.get("summary") or {}
    outstanding = account.get("outstanding_amount")
    if outstanding is None:
        outstanding = summary.get("principal_outstanding")
    if outstanding is None:
        outstanding = principal
    outstanding = max(0.0, _sll_float(outstanding))

    saved_status = _sll_str(account.get("status") or "active").lower()
    if saved_status in ("closed", "discontinued"):
        loan_status = saved_status
    else:
        loan_status = "paid" if principal > 0 and outstanding <= 0.005 else "outstanding"

    return _sll_json_safe({
        "_id": account.get("_id"),
        "loan_account_id": account.get("_id"),
        "loan_cycle_no": _sll_int(account.get("loan_cycle_no"), 1),
        "is_current": bool(account.get("is_current", True)),
        "pg_id": account.get("pg_id"),
        "pg_name": account.get("pg_name"),
        "member_id": account.get("member_id"),
        "member_name": account.get("member_name"),
        "father_mother_spouse": account.get("father_mother_spouse"),
        "sector": account.get("sector"),
        "item_key": account.get("item_key"),
        "item_type": account.get("item_type"),
        "item_name": account.get("item_name"),
        "activity": account.get("activity"),
        "unit": account.get("unit"),
        "loan_amount": principal,
        "loan_purpose": account.get("loan_purpose") or "",
        "loan_start_date": account.get("loan_start_date") or "",
        "interest_rate": _sll_float(account.get("interest_rate")),
        "moratorium_months": _sll_int(account.get("moratorium_months")),
        "status": loan_status,
        "loan_status": loan_status,
        "outstanding_amount": outstanding,
        "summary": summary,
        "created_at": account.get("created_at"),
        "updated_at": account.get("updated_at"),
    })


def _sll_clean_entry(raw, index):
    raw = raw or {}

    date_value = (
        raw.get("date")
        or raw.get("entry_date")
        or raw.get("repayment_date")
        or raw.get("transaction_date")
        or ""
    )

    return {
        "sl_no": index + 1,
        "date": _sll_str(date_value),
        "remarks": _sll_str(raw.get("remarks")),
        "new_loan_amount": _sll_float(
            raw.get("new_loan_amount")
            or raw.get("loan_release")
            or raw.get("additional_loan")
            or 0
        ),
        "principal_paid": _sll_float(
            raw.get("principal_paid")
            or raw.get("principal_repaid")
            or raw.get("principal")
            or 0
        ),
        "interest_paid": _sll_float(
            raw.get("interest_paid")
            or raw.get("interest")
            or 0
        ),
    }


def _sll_month_diff_from_start(start_date, entry_date):
    if not start_date or not entry_date:
        return None

    try:
        sd = datetime.strptime(str(start_date)[:10], "%Y-%m-%d")
        ed = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d")
        return ((ed.year - sd.year) * 12) + (ed.month - sd.month) + 1
    except Exception:
        return None


def _sll_recalculate_entries(account, entries):
    """
    Row calculation rule:

    opening_loan_amount:
      previous closing balance

    For first row:
      opening balance = assigned loan amount

    For ARDD:
      new_loan_amount can be entered in any month and added to previous balance.

    For Agri/Fishery:
      new_loan_amount is normally zero; assigned loan amount is the starting balance.

    User-entered fields:
      date
      principal_paid
      interest_paid
      new_loan_amount only for ARDD or later loan additions

    Auto-calculated:
      opening_loan_amount
      current_month_new_loan
      total_loan_amount
      suggested_interest
      total_repayment
      principal_outstanding
      closing_loan_balance
      KPI summary
    """

    sector = account.get("sector") or ""
    assigned_loan_amount = _sll_float(account.get("loan_amount"))
    interest_rate = _sll_float(account.get("interest_rate"))
    moratorium_months = _sll_int(account.get("moratorium_months"))
    loan_start_date = account.get("loan_start_date") or ""

    calculated = []

    opening_balance = assigned_loan_amount
    total_extra_loan = 0.0
    total_principal_paid = 0.0
    total_interest_paid = 0.0
    total_repayment = 0.0

    for idx, raw in enumerate(entries or []):
        row = _sll_clean_entry(raw, idx)

        # For Agri/Fishery, new loan release is not expected.
        # But backend keeps it only if frontend/admin sends it intentionally.
        new_loan_amount = row["new_loan_amount"]

        if sector in ("Agri", "Fishery") and idx == 0:
            # first row carries assigned loan as opening balance
            pass

        if sector in ("Agri", "Fishery"):
            # Prevent accidental repeated loan additions in normal sectors.
            # If you want later top-up support, remove this line.
            new_loan_amount = 0.0

        loan_before_repayment = opening_balance + new_loan_amount

        month_no_from_start = _sll_month_diff_from_start(
            loan_start_date,
            row.get("date")
        )

        in_moratorium = (
            month_no_from_start is not None
            and moratorium_months > 0
            and month_no_from_start <= moratorium_months
        )

        principal_paid = row["principal_paid"]
        interest_paid = row["interest_paid"]

        # We do not hard-block old entries if no start date exists.
        # But when start date is available, moratorium rule is enforced.
        if in_moratorium and (principal_paid > 0 or interest_paid > 0):
            raise ValueError(
                f"{sector} repayment is not allowed in first {moratorium_months} month(s)."
            )

        if principal_paid > loan_before_repayment:
            raise ValueError(
                f"Row {idx + 1}: Principal paid cannot be greater than available loan balance."
            )

        suggested_interest = loan_before_repayment * interest_rate
        repayment_total = principal_paid + interest_paid
        closing_balance = max(0.0, loan_before_repayment - principal_paid)

        total_extra_loan += new_loan_amount
        total_principal_paid += principal_paid
        total_interest_paid += interest_paid
        total_repayment += repayment_total

        row.update({
            "sl_no": idx + 1,
            "opening_loan_amount": round(opening_balance, 2),
            "current_month_new_loan": round(new_loan_amount, 2),
            "total_loan_amount": round(loan_before_repayment, 2),
            "suggested_interest": round(suggested_interest, 2),
            "principal_paid": round(principal_paid, 2),
            "interest_paid": round(interest_paid, 2),
            "total_repayment": round(repayment_total, 2),
            "principal_outstanding": round(closing_balance, 2),
            "closing_loan_balance": round(closing_balance, 2),
            "month_no_from_start": month_no_from_start,
            "in_moratorium": bool(in_moratorium),
        })

        calculated.append(row)
        opening_balance = closing_balance

    total_loan_released = assigned_loan_amount + total_extra_loan

    summary = {
        "sector": sector,
        "base_loan_amount": round(assigned_loan_amount, 2),
        "additional_loan_amount": round(total_extra_loan, 2),
        "total_loan_amount": round(total_loan_released, 2),
        "principal_paid": round(total_principal_paid, 2),
        "interest_paid": round(total_interest_paid, 2),
        "total_repayment": round(total_repayment, 2),
        "principal_outstanding": round(opening_balance, 2),
        "closing_loan_balance": round(opening_balance, 2),
        "interest_rate": interest_rate,
        "moratorium_months": moratorium_months,
        "entries_count": len(calculated),
    }

    return calculated, summary


def _sll_ledger_base_query(pg_oid, account):
    return {
        "pg_id": pg_oid,
        "loan_account_id": str(account.get("_id")),
        "sector": account.get("sector"),
        "member_id": account.get("member_id"),
        "item_key": account.get("item_key"),
    }


def _sll_account_payment_state(db, account):
    """Return status/outstanding from the cycle's saved repayment ledger."""
    if not account:
        return {"status": "outstanding", "outstanding_amount": 0.0}

    principal = max(0.0, _sll_float(account.get("loan_amount")))
    summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    outstanding = summary.get("principal_outstanding")
    if outstanding is None:
        outstanding = summary.get("closing_loan_balance")

    if outstanding is None and account.get("_id"):
        ledger_doc = db[SECTOR_LEDGER_COLL].find_one(
            _sll_ledger_base_query(account.get("pg_id"), account),
            sort=[("updated_at", -1), ("created_at", -1)]
        )
        if ledger_doc:
            ledger_summary = ledger_doc.get("summary") or {}
            outstanding = ledger_summary.get("principal_outstanding")
            if outstanding is None:
                outstanding = ledger_summary.get("closing_loan_balance")

    if outstanding is None:
        outstanding = account.get("outstanding_amount")
    if outstanding is None:
        outstanding = principal
    outstanding = max(0.0, _sll_float(outstanding))

    saved_status = _sll_str(account.get("status") or "active").lower()
    if saved_status in ("closed", "discontinued"):
        status = saved_status
    else:
        status = "paid" if principal > 0 and outstanding <= 0.005 else "outstanding"

    return {"status": status, "outstanding_amount": round(outstanding, 2)}


def _sll_member_loan_history(db, pg_oid, member_doc, sector=None):
    member_id = _sll_member_id_from_doc(member_doc)
    query = {"pg_id": pg_oid, "member_id": member_id}
    if sector:
        query["sector"] = sector

    accounts = list(
        db[SECTOR_LEDGER_ACCOUNT_COLL]
        .find(query)
        .sort([("loan_cycle_no", -1), ("created_at", -1), ("updated_at", -1)])
    )
    history = []
    for account in accounts:
        state = _sll_account_payment_state(db, account)
        history.append({
            "loan_account_id": str(account.get("_id")),
            "loan_cycle_no": _sll_int(account.get("loan_cycle_no"), 1),
            "is_current": bool(account.get("is_current", False)),
            "sector": account.get("sector") or "",
            "item_key": account.get("item_key") or "",
            "item_name": account.get("item_name") or "",
            "activity": account.get("activity") or "",
            "unit": account.get("unit") or "",
            "loan_amount": _sll_float(account.get("loan_amount")),
            "loan_start_date": account.get("loan_start_date") or "",
            "loan_purpose": account.get("loan_purpose") or "",
            "status": state["status"],
            "outstanding_amount": state["outstanding_amount"],
            "created_at": _sll_json_safe(account.get("created_at")),
        })
    return history


@pg_bp.route("/loan-ledger/sector/history", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger_history():
    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)
    member_id = request.args.get("member_id") or request.args.get("memberId")
    member_doc = _sll_load_member_or_404(db, pg_oid, member_id)
    return jsonify({
        "ok": True,
        "pg_id": str(pg_oid),
        "member": _sll_member_json(member_doc),
        "history": _sll_member_loan_history(db, pg_oid, member_doc),
    })


def _sll_get_period():
    year = request.args.get("year")
    month = request.args.get("month")

    if year in (None, "", "null"):
        year = None
    else:
        year = _sll_int(year)

    if month in (None, "", "null"):
        month = None
    else:
        month = _sll_int(month)

    if month is not None and (month < 1 or month > 12):
        abort(400, "Invalid month.")

    return year, month


def _sll_period_history(db, pg_oid, account, limit=60):
    base_q = _sll_ledger_base_query(pg_oid, account)

    docs = list(
        db[SECTOR_LEDGER_COLL]
        .find(base_q, {"year": 1, "month": 1, "summary": 1, "updated_at": 1, "created_at": 1})
        .sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)])
        .limit(limit)
    )

    history = []

    for doc in docs:
        year = doc.get("year")
        month = doc.get("month")

        try:
            label = datetime(2000, int(month), 1).strftime("%B") + f" {year}"
        except Exception:
            label = "Saved Record"

        history.append({
            "_id": str(doc.get("_id")),
            "year": year,
            "month": month,
            "label": label,
            "entries_count": (doc.get("summary") or {}).get("entries_count", 0),
            "principal_outstanding": (doc.get("summary") or {}).get("principal_outstanding", 0),
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else "",
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else "",
        })

    return history


def _sll_member_json(member_doc):
    return _sll_json_safe({
        "_id": member_doc.get("_id"),
        "member_id": member_doc.get("member_id") or member_doc.get("_id"),
        "name": _sll_member_name(member_doc),
        "father_mother_spouse": _sll_member_guardian(member_doc),
        "contact": _sll_member_contact(member_doc),
        "shg_name": member_doc.get("shg_name") or member_doc.get("SHG Name") or "",
        "shg_code": member_doc.get("shg_code") or member_doc.get("SHG Code") or "",
    })


def _sll_member_active_query(pg_oid):
    return {
        "pg_id": pg_oid,
        "$or": [
            {"is_active": True},
            {"is_active": {"$exists": False}},
        ]
    }


# ------------------------------------------------------------
# Page route mode marker
# Existing /registers/loan-ledger can keep rendering template.
# Frontend will call new JSON APIs below.
# ------------------------------------------------------------

@pg_bp.route("/loan-ledger/sector/bootstrap", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger_bootstrap():
    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)

    sector = _sll_pg_sector(pg_doc)

    if sector not in PG_SECTOR_OPTIONS:
        return jsonify({
            "ok": False,
            "error": "PG sector is missing or invalid. Please update PG Registration first."
        }), 400

    members_raw = list(
        db.pg_members
        .find(_sll_member_active_query(pg_oid))
        .sort([("name", 1), ("member_name", 1)])
    )

    members = []

    for member in members_raw:
        items = _sll_member_sector_items(member, sector)

        members.append({
            **_sll_member_json(member),
            "items_count": len(items),
            "items": items,
        })

    return jsonify({
        "ok": True,
        "mode": "sector-based-loan-ledger",
        "pg": {
            "_id": str(pg_oid),
            "name": _sll_pg_name(pg_doc),
            "sector": sector,
        },
        "rules": {
            "interest_rate": SECTOR_INTEREST_RATE.get(sector, 0),
            "moratorium_months": SECTOR_MORATORIUM_MONTHS.get(sector, 0),
            "editable_fields": (
                ["date", "new_loan_amount", "principal_paid", "interest_paid"]
                if sector == "ARDD"
                else ["date", "principal_paid", "interest_paid"]
            ),
        },
        "members": members,
        "can_save_ledger": _sll_role() == "PG_DATA_ENTRY",
    })


@pg_bp.route("/loan-ledger/sector/member-items", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger_member_items():
    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)

    sector = _sll_pg_sector(pg_doc)
    member_id = request.args.get("member_id") or request.args.get("memberId")

    member_doc = _sll_load_member_or_404(db, pg_oid, member_id)
    items = _sll_member_sector_items(member_doc, sector)

    return jsonify({
        "ok": True,
        "pg": {
            "_id": str(pg_oid),
            "name": _sll_pg_name(pg_doc),
            "sector": sector,
        },
        "member": _sll_member_json(member_doc),
        "items": items,
    })


@pg_bp.route("/loan-ledger/sector/account", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger_account():
    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)
    sector = _sll_pg_sector(pg_doc)

    if request.method == "GET":
        member_id = request.args.get("member_id") or request.args.get("memberId")
        item_key = request.args.get("item_key") or request.args.get("itemKey")
        loan_account_id = request.args.get("loan_account_id") or request.args.get("loanAccountId")
        payload = {}
    else:
        payload = request.get_json(silent=True) or {}
        member_id = payload.get("member_id") or payload.get("memberId")
        item_key = payload.get("item_key") or payload.get("itemKey")
        loan_account_id = payload.get("loan_account_id") or payload.get("loanAccountId")

    member_doc = _sll_load_member_or_404(db, pg_oid, member_id)
    item = _sll_validate_item_for_member(member_doc, sector, item_key)
    account = _sll_find_account(db, pg_oid, member_doc, sector, item, loan_account_id=loan_account_id)

    # A requested historical cycle must never fall through into the path that
    # creates a new empty account. That would make a stale/bad cycle ID mutate
    # data simply by opening it.
    if loan_account_id and not account:
        abort(404, "Loan cycle not found for this member and item.")

    if request.method == "POST":
        role = _sll_role()
        if role not in ("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN"):
            abort(403, "You are not allowed to assign/update loan amount.")

        create_new = bool(payload.get("new_loan") or payload.get("create_new_cycle"))
        if create_new:
            if role != "PG_DATA_ENTRY":
                abort(403, "Only PG login can create a new member loan cycle from Loan Ledger.")
            current_account = _sll_find_account(db, pg_oid, member_doc, sector, item)
            if current_account:
                current_state = _sll_account_payment_state(db, current_account)
                if current_state["status"] != "paid":
                    return jsonify({
                        "ok": False,
                        "error": "A new loan can be created only after the current loan is fully repaid.",
                        "loan_status": current_state["status"],
                        "outstanding_amount": current_state["outstanding_amount"],
                    }), 400
            if _sll_float(payload.get("loan_amount")) <= 0:
                return jsonify({"ok": False, "error": "Enter a loan amount greater than zero for the new loan."}), 400
            account = _sll_create_or_update_account(
                db, pg_doc=pg_doc, pg_oid=pg_oid, member_doc=member_doc,
                sector=sector, item=item, payload=payload, force_new=True
            )
        else:
            if account and _sll_account_payment_state(db, account)["status"] == "paid":
                return jsonify({
                    "ok": False,
                    "error": "This loan is fully repaid. Create a new loan cycle to record another loan.",
                }), 400
            account = _sll_create_or_update_account(
                db, pg_doc=pg_doc, pg_oid=pg_oid, member_doc=member_doc,
                sector=sector, item=item, payload=payload
            )

        state = _sll_account_payment_state(db, account)
        return jsonify({
            "ok": True,
            "message": "New loan cycle created successfully." if create_new else "Sector loan account saved successfully.",
            "account": _sll_account_json({**account, **state}),
            "history": _sll_member_loan_history(db, pg_oid, member_doc),
        })

    if not account:
        account = _sll_create_or_update_account(
            db, pg_doc=pg_doc, pg_oid=pg_oid, member_doc=member_doc,
            sector=sector, item=item, payload={}
        )

    state = _sll_account_payment_state(db, account)
    return jsonify({
        "ok": True,
        "pg": {"_id": str(pg_oid), "name": _sll_pg_name(pg_doc), "sector": sector},
        "member": _sll_member_json(member_doc),
        "item": item,
        "account": _sll_account_json({**account, **state}),
        "history": _sll_member_loan_history(db, pg_oid, member_doc),
        "rules": {
            "interest_rate": SECTOR_INTEREST_RATE.get(sector, 0),
            "moratorium_months": SECTOR_MORATORIUM_MONTHS.get(sector, 0),
        },
    })




@pg_bp.route("/loan-ledger/sector/ledger", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger():
    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)
    sector = _sll_pg_sector(pg_doc)

    if sector not in PG_SECTOR_OPTIONS:
        return jsonify({
            "ok": False,
            "error": "PG sector is missing or invalid. Please update PG Registration first."
        }), 400

    if request.method == "GET":
        payload = {}
        member_id = request.args.get("member_id") or request.args.get("memberId")
        item_key = request.args.get("item_key") or request.args.get("itemKey")
        loan_account_id = request.args.get("loan_account_id") or request.args.get("loanAccountId")
    else:
        payload = request.get_json(silent=True) or {}
        member_id = payload.get("member_id") or payload.get("memberId")
        item_key = payload.get("item_key") or payload.get("itemKey")
        loan_account_id = payload.get("loan_account_id") or payload.get("loanAccountId")

    member_doc = _sll_load_member_or_404(db, pg_oid, member_id)
    item = _sll_validate_item_for_member(member_doc, sector, item_key)

    account = _sll_find_account(db, pg_oid, member_doc, sector, item, loan_account_id=loan_account_id)
    if not account:
        account = _sll_create_or_update_account(
            db,
            pg_doc=pg_doc,
            pg_oid=pg_oid,
            member_doc=member_doc,
            sector=sector,
            item=item,
            payload=payload
        )
    elif request.method == "POST":
        account = _sll_create_or_update_account(
            db,
            pg_doc=pg_doc,
            pg_oid=pg_oid,
            member_doc=member_doc,
            sector=sector,
            item=item,
            payload=payload
        )

    base_q = _sll_ledger_base_query(pg_oid, account)
    base_q["ledger_scope"] = "cumulative"

    if request.method == "GET":
        if request.args.get("history") in ("1", "true", "yes"):
            latest_doc = db[SECTOR_LEDGER_COLL].find_one(
                base_q,
                sort=[("updated_at", -1), ("created_at", -1)]
            )
            return jsonify({
                "ok": True,
                "history": [_sll_json_safe(latest_doc)] if latest_doc else []
            })

        doc = db[SECTOR_LEDGER_COLL].find_one(
            base_q,
            sort=[("updated_at", -1), ("created_at", -1)]
        )

        if not doc:
            calculated, summary = _sll_recalculate_entries(account, [])
            return jsonify({
                "ok": True,
                "pg": {
                    "_id": str(pg_oid),
                    "name": _sll_pg_name(pg_doc),
                    "sector": sector,
                },
                "member": _sll_member_json(member_doc),
                "item": item,
                "account": _sll_account_json(account),
                "entries": calculated,
                "summary": summary,
            })

        doc = _sll_json_safe(doc)
        doc["ok"] = True
        doc["pg"] = {
            "_id": str(pg_oid),
            "name": _sll_pg_name(pg_doc),
            "sector": sector,
        }
        doc["member"] = _sll_member_json(member_doc)
        doc["item"] = item
        doc["account"] = _sll_account_json(account)
        return jsonify(doc)

    role = _sll_role()
    if role != "PG_DATA_ENTRY":
        abort(403, "Only PG login can save Loan Ledger repayment entries.")

    state = _sll_account_payment_state(db, account)
    status = _sll_str(account.get("status") or "active").lower()
    if status in ("closed", "discontinued"):
        abort(400, "This loan account is closed/discontinued. Ledger update is not allowed.")
    if state["status"] == "paid":
        return jsonify({
            "ok": False,
            "error": "This loan is fully repaid. Its history is read-only; create a new loan cycle for another loan.",
        }), 400

    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        abort(400, "entries must be a list.")

    try:
        calculated_entries, summary = _sll_recalculate_entries(account, entries)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    before = db[SECTOR_LEDGER_COLL].find_one(base_q)
    now = _sll_now()

    doc = {
        **base_q,
        "ledger_scope": "cumulative",
        "pg_name": _sll_pg_name(pg_doc),
        "member_name": _sll_member_name(member_doc),
        "father_mother_spouse": _sll_member_guardian(member_doc),
        "item_type": item["item_type"],
        "item_name": item["item_name"],
        "activity": item.get("activity") or "",
        "unit": item.get("unit") or "",
        "loan_amount": _sll_float(account.get("loan_amount")),
        "loan_start_date": account.get("loan_start_date") or "",
        "loan_purpose": account.get("loan_purpose") or "",
        "interest_rate": _sll_float(account.get("interest_rate")),
        "moratorium_months": _sll_int(account.get("moratorium_months")),
        "entries": calculated_entries,
        "summary": summary,
        "updated_at": now,
    }

    if not before:
        doc["created_at"] = now
        res = db[SECTOR_LEDGER_COLL].insert_one(doc)
        doc["_id"] = res.inserted_id

        log_audit(
            db,
            action=f"{SECTOR_LEDGER_COLL}:create",
            collection=SECTOR_LEDGER_COLL,
            doc_id=res.inserted_id,
            user=_current_user_dict(),
            after=doc,
            meta={
                "pg_id": str(pg_oid),
                "sector": sector,
                "member_id": str(base_q.get("member_id")),
                "item_key": item["item_key"],
                "ledger_scope": "cumulative",
            }
        )
    else:
        db[SECTOR_LEDGER_COLL].update_one(
            {"_id": before["_id"]},
            {"$set": doc}
        )
        doc["_id"] = before["_id"]

        log_audit(
            db,
            action=f"{SECTOR_LEDGER_COLL}:update",
            collection=SECTOR_LEDGER_COLL,
            doc_id=before["_id"],
            user=_current_user_dict(),
            before=before,
            after=doc,
            meta={
                "pg_id": str(pg_oid),
                "sector": sector,
                "member_id": str(base_q.get("member_id")),
                "item_key": item["item_key"],
                "ledger_scope": "cumulative",
            }
        )

    db[SECTOR_LEDGER_ACCOUNT_COLL].update_one(
        {"_id": account["_id"]},
        {
            "$set": {
                "summary": summary,
                "outstanding_amount": summary.get("principal_outstanding", 0),
                "principal_paid": summary.get("principal_paid", 0),
                "interest_paid": summary.get("interest_paid", 0),
                "total_repayment": summary.get("total_repayment", 0),
                "status": "paid" if _sll_float(summary.get("principal_outstanding")) <= 0.005 and _sll_float(account.get("loan_amount")) > 0 else "active",
                "updated_at": now,
            }
        }
    )

    refreshed_account = db[SECTOR_LEDGER_ACCOUNT_COLL].find_one({"_id": account["_id"]})

    return jsonify({
        "ok": True,
        "message": "Sector-based Loan Ledger saved successfully.",
        "pg": {
            "_id": str(pg_oid),
            "name": _sll_pg_name(pg_doc),
            "sector": sector,
        },
        "member": _sll_member_json(member_doc),
        "item": item,
        "account": _sll_account_json({**refreshed_account, **_sll_account_payment_state(db, refreshed_account)}),
        "entries": calculated_entries,
        "summary": summary,
    })






@pg_bp.route("/loan-ledger/sector/summary", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_sector_loan_ledger_summary():
    """
    Summary/KPI endpoint for selected member + crop/activity/unit.
    """

    db = current_app.mongo_db
    pg_doc, pg_oid = _sll_load_pg_or_404(db)
    sector = _sll_pg_sector(pg_doc)

    member_id = request.args.get("member_id") or request.args.get("memberId")
    item_key = request.args.get("item_key") or request.args.get("itemKey")

    member_doc = _sll_load_member_or_404(db, pg_oid, member_id)
    item = _sll_validate_item_for_member(member_doc, sector, item_key)

    account = _sll_find_account(db, pg_oid, member_doc, sector, item)

    if not account:
        account = _sll_create_or_update_account(
            db,
            pg_doc=pg_doc,
            pg_oid=pg_oid,
            member_doc=member_doc,
            sector=sector,
            item=item,
            payload={}
        )

    latest_ledger = db[SECTOR_LEDGER_COLL].find_one(
        _sll_ledger_base_query(pg_oid, account),
        sort=[("year", -1), ("month", -1), ("updated_at", -1)]
    )

    summary = {}

    if latest_ledger:
        summary = latest_ledger.get("summary") or {}
    else:
        _, summary = _sll_recalculate_entries(account, [])

    return jsonify({
        "ok": True,
        "pg": {
            "_id": str(pg_oid),
            "name": _sll_pg_name(pg_doc),
            "sector": sector,
        },
        "member": _sll_member_json(member_doc),
        "item": item,
        "account": _sll_account_json(account),
        "summary": summary,
    })









# ---------- Loan Ledger: repayment register for member loans ----------
@pg_bp.route("/loan-ledger/<loan_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_loan_ledger(loan_id):
    db = current_app.mongo_db
    pg_id = _ctx_pg_id()

    if not pg_id:
        abort(400, "Missing PG context")

    pg_doc = _load_pg_or_404(db, pg_id)
    pg_oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))

    # Important: Loan Ledger must connect only to pg_member_loan_accounts
    loan_doc = _get_member_loan_for_ledger_or_404(
        db,
        pg_oid=pg_oid,
        loan_id=loan_id
    )

    member_doc = _ledger_member_lookup(db, loan_doc)
    loan_payload = _serialize_member_loan_for_ledger(db, loan_doc)

    coll = "pg_loan_ledgers"
    year, month = _get_period_from_args()

    base_q = {
        "pg_id": pg_oid,
        "loan_id": str(loan_doc["_id"]),
        "loan_type": "member"
    }

    q = dict(base_q)
    if year is not None:
        q["year"] = year
    if month is not None:
        q["month"] = month

    if request.method == "GET":
        if request.args.get("history") in ("1", "true", "yes"):
            return jsonify({
                "ok": True,
                "history": _period_history(db, coll, base_q)
            })

        doc = None
        if year is not None and month is not None:
            doc = db[coll].find_one(q)

        if not doc:
            doc = db[coll].find_one(base_q, sort=[("updated_at", -1)])

        if not doc:
            return jsonify({
                "ok": True,
                "pg_id": str(pg_oid),
                "pg_name": pg_doc.get("name") or pg_doc.get("pg_name") or "",
                "loan_id": str(loan_doc["_id"]),
                "loan_type": "member",
                "loan": loan_payload,
                "member": {
                    "_id": str(member_doc.get("_id")) if member_doc.get("_id") else "",
                    "name": _ledger_member_name(member_doc),
                    "father_mother_spouse": _ledger_member_guardian(member_doc),
                },
                "year": year,
                "month": month,
                "entries": [],
                "meta": {},
            })

        doc = _ledger_json_safe(doc)
        doc["ok"] = True
        doc["pg_name"] = doc.get("pg_name") or pg_doc.get("name") or pg_doc.get("pg_name") or ""
        doc["loan"] = loan_payload
        doc["member"] = {
            "_id": str(member_doc.get("_id")) if member_doc.get("_id") else "",
            "name": _ledger_member_name(member_doc),
            "father_mother_spouse": _ledger_member_guardian(member_doc),
        }

        return jsonify(doc)

    # PG login is allowed to save repayment/register entries.
    # CLF/Block/Admin create the member loan in Member Loans page, not here.
    role = getattr(g, "role", None) or session.get("role")
    if role != "PG_DATA_ENTRY":
        abort(403, "Only PG login can save Loan Ledger repayment entries.")

    current_status = (loan_doc.get("status") or "active").lower()
    if current_status in ("closed", "discontinued"):
        abort(400, "This member loan is closed/discontinued. Ledger update is not allowed.")

    payload = request.get_json(silent=True) or {}

    pg_name = pg_doc.get("name") or pg_doc.get("pg_name") or ""
    member_name = _ledger_member_name(member_doc)
    member_guardian = _ledger_member_guardian(member_doc)

    before = db[coll].find_one(q)
    now = datetime.utcnow()

    doc = dict(payload)
    doc.update({
        "pg_id": pg_oid,
        "pg_name": pg_name,
        "loan_id": str(loan_doc["_id"]),
        "loan_type": "member",
        "loan_no": loan_doc.get("loan_no") or "",
        "member_id": str(loan_doc.get("member_id")) if loan_doc.get("member_id") else "",
        "member_name": member_name,
        "father_mother_spouse": member_guardian,
        "loan_amount": _ledger_loan_amount(loan_doc),
        "loan_tenure": _ledger_int(loan_doc.get("tenure_months")),
        "no_of_installments": (
            _ledger_int(loan_doc.get("installments_total"))
            or len(loan_doc.get("schedule") or [])
            or _ledger_int(loan_doc.get("tenure_months"))
        ),
        "loan_purpose": loan_doc.get("purpose") or loan_doc.get("loan_purpose") or "",
        "year": year,
        "month": month,
        "updated_at": now,
    })

    totals = _ledger_payload_totals(doc)
    doc["summary"] = {
        "principal_due": totals["principal_due"],
        "principal_repaid": totals["principal_repaid"],
        "interest_due": totals["interest_due"],
        "interest_paid": totals["interest_paid"],
    }

    if not before:
        doc["created_at"] = now
        res = db[coll].insert_one(doc)

        log_audit(
            db,
            action=f"{coll}:create",
            collection=coll,
            doc_id=res.inserted_id,
            user=_current_user_dict(),
            after=doc,
            meta={
                "pg_id": str(pg_oid),
                "loan_id": str(loan_doc["_id"]),
                "loan_type": "member",
                "year": year,
                "month": month,
            }
        )
    else:
        db[coll].update_one({"_id": before["_id"]}, {"$set": doc})

        log_audit(
            db,
            action=f"{coll}:update",
            collection=coll,
            doc_id=before["_id"],
            user=_current_user_dict(),
            before=before,
            after=doc,
            meta={
                "pg_id": str(pg_oid),
                "loan_id": str(loan_doc["_id"]),
                "loan_type": "member",
                "year": year,
                "month": month,
            }
        )

    refreshed_loan = _recalculate_member_loan_from_ledgers(
        db,
        pg_oid=pg_oid,
        loan_doc=loan_doc
    )

    return jsonify({
        "ok": True,
        "message": "Loan Ledger saved and Member Loan Account updated successfully.",
        "loan": _serialize_member_loan_for_ledger(db, refreshed_loan),
    })





@pg_bp.route('/registers/receipt-voucher')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def receipt_voucher():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "receipt-voucher"})
    return render_template('receipt_voucher.html', pg_id=pg_id)



@pg_bp.route('/registers/input')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def input_register():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "input"})
    return render_template('input_register.html', pg_id=pg_id)

@pg_bp.route('/registers/asset')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def asset_register():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "asset"})
    return render_template('asset_register.html', pg_id=pg_id)

@pg_bp.route('/registers/ledger-book')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def ledger_book():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "ledger-book"})
    return render_template('ledger_book.html', pg_id=pg_id)

@pg_bp.route('/registers/output')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def output_register():
    pg_id = getattr(g, "pg_id", None) or session.get('pg_id') or session.get('active_pg_id')
    if request.headers.get("Authorization") or request.is_json:
        return jsonify({"ok": True, "pg_id": str(pg_id or ""), "register": "output"})
    return render_template('output_register.html', pg_id=pg_id)



def _assigned_pg_ids_for_session():
    vals = session.get("assigned_pg_ids") or getattr(g, "assigned_pg_ids", None) or []
    return {str(x) for x in vals}

# ----------------------------
# PG Registers — DATA APIs (MongoDB + Audit)
# These APIs power the "templates only" register pages so data is persisted
# and higher roles can view PG activity (scope-respecting).
# ----------------------------

def _ctx_pg_id():
    """Return the active PG context.
    Web  => session['pg_id'] / session['active_pg_id']
    Mobile JWT => g.pg_id (decoded from token by rbac._authenticate_request)
    Fallback => query-string / form field 'pg_id'
    """
    #  MOBILE FIX: g.pg_id is populated by JWT decode in rbac.py
    return (
        getattr(g, "pg_id", None)
        or session.get("pg_id")
        or session.get("active_pg_id")
        or request.args.get("pg_id")
        or (request.get_json(silent=True) or {}).get("pg_id")
        or request.form.get("pg_id")
    )


def _enforce_pg_scope(pg_doc):
    """Abort with 403 if current user is not allowed to access this PG document.
     MOBILE FIX: reads from g (JWT) with session fallback so mobile JWT users
    are scoped exactly the same as web session users.
    """

    #  MOBILE FIX: use g first (JWT), fall back to session (web)
    role     = getattr(g, "role",        None) or session.get("role")
    pg_id_s  = str(getattr(g, "pg_id",  None) or session.get("pg_id") or "")
    clf_id   = getattr(g, "clf_id",      None) or session.get("clf_id")
    block_id = getattr(g, "block_id",    None) or session.get("block_id")
    dist_id  = getattr(g, "district_id", None) or session.get("district_id")
    state_id = getattr(g, "state_id",    None) or session.get("state_id")

    # PG user can only see own PG
    if role == "PG_DATA_ENTRY":
        if str(pg_doc.get("_id")) != pg_id_s:
            abort(403)
    if role == "CADRE_CC":
        if str(pg_doc.get("_id")) not in _assigned_pg_ids_for_session():
            abort(403)

    # Hierarchy scoping for other roles
    if clf_id and str(pg_doc.get("clf_id")) != str(clf_id):
        abort(403)

    if block_id and str(pg_doc.get("block_id")) != str(block_id):
        abort(403)

    if dist_id and str(pg_doc.get("district_id")) != str(dist_id):
        abort(403)

    if state_id and str(pg_doc.get("state_id")) != str(state_id):
        abort(403)


def _load_pg_or_404(db, pg_id):
    try:
        oid = (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))
    except Exception:
        abort(400, "Invalid PG ID")
    pg_doc = db.pgs.find_one({"_id": oid})
    if not pg_doc:
        abort(404, "PG not found")
    _enforce_pg_scope(pg_doc)
    return pg_doc


def _upsert_pg_period_doc(db, *, collection: str, pg_id: str, year: int | None, month: int | None, payload: dict, user: dict):
    """Store a single period document for a PG. Creates or updates with audit logging."""
    q = {"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}
    if year is not None:
        q["year"] = int(year)
    if month is not None:
        q["month"] = int(month)

    before = db[collection].find_one(q)
    now = datetime.utcnow()

    doc = dict(payload or {})
    doc.update({
        "pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))),
        "year": int(year) if year is not None else None,
        "month": int(month) if month is not None else None,
        "updated_at": now,
    })
    if not before:
        doc["created_at"] = now
        res = db[collection].insert_one(doc)
        log_audit(db, action=f"{collection}:create", collection=collection, doc_id=res.inserted_id, user=user, after=doc, meta={"pg_id": pg_id, "year": year, "month": month})
        return res.inserted_id
    else:
        db[collection].update_one({"_id": before["_id"]}, {"$set": doc})
        log_audit(db, action=f"{collection}:update", collection=collection, doc_id=before["_id"], user=user, before=before, after=doc, meta={"pg_id": pg_id, "year": year, "month": month})
        return before["_id"]


def _get_period_from_args():
    year = request.args.get("year") or request.form.get("year")
    month = request.args.get("month") or request.form.get("month")
    try:
        year = int(year) if year not in (None, "", "null") else None
    except Exception:
        year = None
    try:
        month = int(month) if month not in (None, "", "null") else None
    except Exception:
        month = None
    return year, month


def _period_history(db, collection, query, limit=60):
    projection = {
        "year": 1,
        "month": 1,
        "updated_at": 1,
        "created_at": 1,
        "data": 1,
    }

    # Cashbook history needs these fields for saved month summary cards.
    if collection == "pg_cashbooks":
        projection.update({
            "opening": 1,
            "opening_cash": 1,
            "opening_bank": 1,
            "receipts": 1,
            "payments": 1,
            "totals": 1,
            "meta": 1,
        })

    docs = list(
        db[collection]
        .find(query, projection)
        .sort([("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)])
        .limit(limit)
    )

    def _month_name(month):
        try:
            return datetime(2000, int(month), 1).strftime("%b")
        except Exception:
            return "Unknown"

    history = []

    for doc in docs:
        data = doc.get("data") or {}
        rows = data.get("rows") or data.get("assets") or []

        year = doc.get("year")
        month = doc.get("month")

        # Asset register fallback: web old docs may have year/month null.
        if (not year or not month) and rows:
            first_date = str(rows[0].get("purchase_date") or "").strip()
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    d = datetime.strptime(first_date, fmt)
                    year = d.year
                    month = d.month
                    break
                except Exception:
                    pass

        label = (
            f"{_month_name(month)} {year}"
            if year and month
            else f"Saved {doc.get('updated_at').strftime('%d %b %Y')}" if doc.get("updated_at") else "Saved Record"
        )

        item = {
            "_id": str(doc.get("_id")),
            "year": int(year) if year else None,
            "month": int(month) if month else None,
            "period": f"{year}-{int(month):02d}" if year and month else "",
            "label": label,
            "row_count": len(rows) if isinstance(rows, list) else 0,
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else "",
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else "",
        }

        # Extra summary only for cashbook.
        # Other register histories remain unchanged.
        if collection == "pg_cashbooks":
            receipts = _cashbook_clean_rows(doc.get("receipts") or [])
            payments = _cashbook_clean_rows(doc.get("payments") or [])

            opening = doc.get("opening") if isinstance(doc.get("opening"), dict) else {}
            opening_cash = _cashbook_float(opening.get("cash") if "cash" in opening else doc.get("opening_cash"))
            opening_bank = _cashbook_float(opening.get("bank") if "bank" in opening else doc.get("opening_bank"))

            normalized_opening = {
                "cash": round(opening_cash, 2),
                "bank": round(opening_bank, 2),
                "total": round(opening_cash + opening_bank, 2),
                "source": opening.get("source") or "saved",
                "source_year": opening.get("source_year"),
                "source_month": opening.get("source_month"),
            }

            totals = _cashbook_calculate_totals(normalized_opening, receipts, payments)

            item.update({
                "receipt_count": len(receipts),
                "payment_count": len(payments),
                "row_count": len(receipts) + len(payments),
                "opening": normalized_opening,
                "totals": totals,
               "summary": {
    "opening_total": totals.get("opening_total", 0),

    "receipt_total": totals.get("receipt_total", 0),
    "payment_total": totals.get("payment_total", 0),

    "contra_cash_to_bank": totals.get("contra_cash_to_bank", 0),
    "contra_bank_to_cash": totals.get("contra_bank_to_cash", 0),
    "contra_total": totals.get("contra_total", 0),

    "closing_total": totals.get("closing_total", 0),
    "closing_cash": totals.get("closing_cash", 0),
    "closing_bank": totals.get("closing_bank", 0),
},
            })

        history.append(item)

    return history


# ============================================================
# Cash Book balance helpers
# ============================================================

def _cashbook_float(value, default=0.0):
    try:
        if value in (None, "", "null", "None", "-", "NaN"):
            return float(default)
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = (
            str(value)
            .replace("₹", "")
            .replace(",", "")
            .strip()
        )
        return float(cleaned) if cleaned else float(default)
    except Exception:
        return float(default)


def _cashbook_int(value, default=None):
    try:
        if value in (None, "", "null", "None"):
            return default
        return int(value)
    except Exception:
        return default


def _cashbook_month_bounds(year, month):
    try:
        year = int(year)
        month = int(month)
        start = datetime(year, month, 1)

        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)

        return start, end
    except Exception:
        return None, None


def _cashbook_parse_row_date(value):
    if isinstance(value, datetime):
        return value

    text = str(value or "").strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt)
        except Exception:
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _cashbook_clean_rows(rows):
    """
    Normalize cashbook rows before save/load.

    amount     = cash amount
    bankAmount = bank amount

    Keeps contra metadata:
      entryType, contraType, contraGroupId
    """
    clean = []

    if not isinstance(rows, list):
        return clean

    for row in rows:
        if not isinstance(row, dict):
            continue

        new_row = dict(row)

        new_row["id"] = str(
            new_row.get("id")
            or new_row.get("_id")
            or f"row_{datetime.utcnow().timestamp()}"
        ).strip()

        new_row["date"] = str(new_row.get("date") or "").strip()

        new_row["particulars"] = str(
            new_row.get("particulars")
            or new_row.get("description")
            or ""
        ).strip()

        new_row["lf"] = str(
            new_row.get("lf")
            or new_row.get("ledgerFolio")
            or ""
        ).strip()

        new_row["ledgerFolio"] = str(
            new_row.get("ledgerFolio")
            or new_row.get("lf")
            or ""
        ).strip()

        new_row["amount"] = round(_cashbook_float(
            new_row.get("amount")
            or new_row.get("cashAmount")
            or new_row.get("cash_amount")
            or 0
        ), 2)

        new_row["bankAmount"] = round(_cashbook_float(
            new_row.get("bankAmount")
            or new_row.get("bank_amount")
            or new_row.get("bank")
            or 0
        ), 2)

        new_row["remarks"] = str(new_row.get("remarks") or "").strip()

        if new_row.get("entryType"):
            new_row["entryType"] = str(new_row.get("entryType") or "").strip()

        if new_row.get("contraType"):
            new_row["contraType"] = str(new_row.get("contraType") or "").strip()

        if new_row.get("contraGroupId"):
            new_row["contraGroupId"] = str(new_row.get("contraGroupId") or "").strip()

        has_text = any(
            str(new_row.get(k) or "").strip()
            for k in (
                "date",
                "particulars",
                "lf",
                "ledgerFolio",
                "remarks",
                "entryType",
                "contraType",
                "contraGroupId",
            )
        )

        has_amount = bool(new_row["amount"] or new_row["bankAmount"])

        if has_text or has_amount:
            clean.append(new_row)

    return clean


def _cashbook_validate_row_dates(rows, year, month, label):
    """
    Every filled cashbook row must have a valid date inside selected month/year.
    """
    try:
        year = int(year)
        month = int(month)
    except Exception:
        return False, "Invalid cashbook period."

    for idx, row in enumerate(rows or [], start=1):
        if not isinstance(row, dict):
            continue

        has_value = bool(
            str(row.get("particulars") or "").strip()
            or str(row.get("lf") or "").strip()
            or str(row.get("ledgerFolio") or "").strip()
            or str(row.get("remarks") or "").strip()
            or _cashbook_float(row.get("amount"))
            or _cashbook_float(row.get("bankAmount"))
        )

        if not has_value:
            continue

        row_date = _cashbook_parse_row_date(row.get("date"))

        if not row_date:
            return False, f"{label} row {idx} must have a valid date."

        if int(row_date.year) != year or int(row_date.month) != month:
            month_name = datetime(year, month, 1).strftime("%b %Y")
            return False, f"{label} row {idx} date must be inside {month_name}."

    return True, ""


def _cashbook_amount_parts(row):
    """
    Existing frontend/backend convention:
      amount     = cash amount
      bankAmount = bank amount
    """
    if not isinstance(row, dict):
        return 0.0, 0.0

    cash = _cashbook_float(
        row.get("amount")
        or row.get("cashAmount")
        or row.get("cash_amount")
        or 0
    )

    bank = _cashbook_float(
        row.get("bankAmount")
        or row.get("bank_amount")
        or row.get("bank")
        or 0
    )

    return round(cash, 2), round(bank, 2)


def _cashbook_is_contra(row):
    return isinstance(row, dict) and str(row.get("entryType") or "").strip().lower() == "contra"


def _cashbook_sum_parts(rows, *, include_contra=True, only_contra=False):
    cash_total = 0.0
    bank_total = 0.0

    for row in (rows or []):
        is_contra = _cashbook_is_contra(row)

        if not include_contra and is_contra:
            continue

        if only_contra and not is_contra:
            continue

        cash, bank = _cashbook_amount_parts(row)
        cash_total += cash
        bank_total += bank

    return {
        "cash": round(cash_total, 2),
        "bank": round(bank_total, 2),
        "total": round(cash_total + bank_total, 2),
    }


def _cashbook_contra_totals(receipts, payments):
    """
    Contra entries are internal cash/bank transfers.

    Frontend creates 2 rows per contra:
      Cash -> Bank:
        receipt bankAmount + payment cash amount
      Bank -> Cash:
        receipt cash amount + payment bankAmount

    Because each transfer appears twice, divide by 2 for display/report transfer total.
    """
    cash_to_bank = 0.0
    bank_to_cash = 0.0

    for row in list(receipts or []) + list(payments or []):
        if not _cashbook_is_contra(row):
            continue

        cash, bank = _cashbook_amount_parts(row)
        row_total = cash + bank
        contra_type = str(row.get("contraType") or "").strip().lower()

        if contra_type == "cash_to_bank":
            cash_to_bank += row_total

        elif contra_type == "bank_to_cash":
            bank_to_cash += row_total

    cash_to_bank = round(cash_to_bank / 2, 2)
    bank_to_cash = round(bank_to_cash / 2, 2)

    return {
        "cash_to_bank": cash_to_bank,
        "bank_to_cash": bank_to_cash,
        "total": round(cash_to_bank + bank_to_cash, 2),
    }


def _cashbook_calculate_totals(opening, receipts, payments):
    opening_cash = _cashbook_float((opening or {}).get("cash"))
    opening_bank = _cashbook_float((opening or {}).get("bank"))

    # Closing balance must include contra rows because contra changes cash/bank split.
    receipt_for_balance = _cashbook_sum_parts(receipts, include_contra=True)
    payment_for_balance = _cashbook_sum_parts(payments, include_contra=True)

    # Real receipt/payment totals must exclude contra rows.
    receipt_real = _cashbook_sum_parts(receipts, include_contra=False)
    payment_real = _cashbook_sum_parts(payments, include_contra=False)

    contra = _cashbook_contra_totals(receipts, payments)

    closing_cash = opening_cash + receipt_for_balance["cash"] - payment_for_balance["cash"]
    closing_bank = opening_bank + receipt_for_balance["bank"] - payment_for_balance["bank"]

    return {
        "opening_cash": round(opening_cash, 2),
        "opening_bank": round(opening_bank, 2),
        "opening_total": round(opening_cash + opening_bank, 2),

        # Real receipt totals only.
        "receipt_cash": receipt_real["cash"],
        "receipt_bank": receipt_real["bank"],
        "receipt_total": receipt_real["total"],

        # Real payment totals only.
        "payment_cash": payment_real["cash"],
        "payment_bank": payment_real["bank"],
        "payment_total": payment_real["total"],

        # Internal transfers.
        "contra_cash_to_bank": contra["cash_to_bank"],
        "contra_bank_to_cash": contra["bank_to_cash"],
        "contra_total": contra["total"],

        # Closing balance includes both real rows and contra rows.
        "closing_cash": round(closing_cash, 2),
        "closing_bank": round(closing_bank, 2),
        "closing_total": round(closing_cash + closing_bank, 2),

        "left_cash_total": round(opening_cash + receipt_for_balance["cash"], 2),
        "left_bank_total": round(opening_bank + receipt_for_balance["bank"], 2),
        "right_cash_total": round(payment_for_balance["cash"] + closing_cash, 2),
        "right_bank_total": round(payment_for_balance["bank"] + closing_bank, 2),
    }


def _cashbook_json_safe_doc(doc):
    if not isinstance(doc, dict):
        return doc

    out = dict(doc)

    if out.get("_id") is not None:
        out["_id"] = str(out["_id"])

    if out.get("pg_id") is not None:
        out["pg_id"] = str(out["pg_id"])

    for key in ("created_at", "updated_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()

    return out


def _cashbook_previous_doc(db, pg_oid, year, month):
    """
    Find the latest saved cashbook before the selected year/month.
    Used to carry forward previous closing balance.
    """
    try:
        year = int(year)
        month = int(month)
    except Exception:
        return None

    if not pg_oid or not year or not month:
        return None

    return db.pg_cashbooks.find_one(
        {
            "pg_id": pg_oid,
            "$or": [
                {"year": {"$lt": year}},
                {"year": year, "month": {"$lt": month}},
            ],
        },
        sort=[("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)],
    )


def _cashbook_opening_from_payload(payload):
    """
    Build opening balance from frontend payload.
    """
    if not isinstance(payload, dict):
        payload = {}

    opening = payload.get("opening") if isinstance(payload.get("opening"), dict) else {}

    cash = _cashbook_float(
        opening.get("cash")
        if "cash" in opening else
        opening.get("opening_cash")
        if "opening_cash" in opening else
        payload.get("opening_cash")
    )

    bank = _cashbook_float(
        opening.get("bank")
        if "bank" in opening else
        opening.get("opening_bank")
        if "opening_bank" in opening else
        payload.get("opening_bank")
    )

    source = (
        opening.get("source")
        or payload.get("opening_source")
        or "manual"
    )

    source_year = opening.get("source_year") or payload.get("source_year")
    source_month = opening.get("source_month") or payload.get("source_month")

    return {
        "cash": round(cash, 2),
        "bank": round(bank, 2),
        "total": round(cash + bank, 2),
        "source": source,
        "source_year": source_year,
        "source_month": source_month,
        "readonly": False,
    }


def _cashbook_opening_for_period(db, pg_oid, year, month, current_doc=None):
    """
    Opening balance priority:
    1. If selected month already has saved opening, use it.
    2. Else carry forward previous saved month's closing balance.
    3. Else allow manual opening balance.
    """
    try:
        year = int(year)
        month = int(month)
    except Exception:
        year = None
        month = None

    if isinstance(current_doc, dict) and isinstance(current_doc.get("opening"), dict):
        opening = current_doc.get("opening") or {}

        cash = _cashbook_float(
            opening.get("cash")
            if "cash" in opening else
            current_doc.get("opening_cash")
        )

        bank = _cashbook_float(
            opening.get("bank")
            if "bank" in opening else
            current_doc.get("opening_bank")
        )

        return {
            "cash": round(cash, 2),
            "bank": round(bank, 2),
            "total": round(cash + bank, 2),
            "source": opening.get("source") or "saved",
            "source_year": opening.get("source_year"),
            "source_month": opening.get("source_month"),
            "readonly": True,
        }

    prev_doc = _cashbook_previous_doc(db, pg_oid, year, month)

    if isinstance(prev_doc, dict):
        prev_totals = prev_doc.get("totals") if isinstance(prev_doc.get("totals"), dict) else {}

        cash = _cashbook_float(prev_totals.get("closing_cash"))
        bank = _cashbook_float(prev_totals.get("closing_bank"))

        if not prev_totals:
            prev_opening = prev_doc.get("opening") if isinstance(prev_doc.get("opening"), dict) else {}
            prev_receipts = _cashbook_clean_rows(prev_doc.get("receipts") or [])
            prev_payments = _cashbook_clean_rows(prev_doc.get("payments") or [])
            recalculated = _cashbook_calculate_totals(prev_opening, prev_receipts, prev_payments)
            cash = _cashbook_float(recalculated.get("closing_cash"))
            bank = _cashbook_float(recalculated.get("closing_bank"))

        return {
            "cash": round(cash, 2),
            "bank": round(bank, 2),
            "total": round(cash + bank, 2),
            "source": "carried_forward",
            "source_year": prev_doc.get("year"),
            "source_month": prev_doc.get("month"),
            "readonly": True,
        }

    return {
        "cash": 0.0,
        "bank": 0.0,
        "total": 0.0,
        "source": "manual",
        "source_year": None,
        "source_month": None,
        "readonly": False,
    }



def _cashbook_error_response(message, *, status=400, title="Cash Book Error", details=None, debug=None, totals=None):
    payload = {
        "ok": False,
        "title": title,
        "error": message,
        "message": message,
    }

    if details:
        payload["details"] = details

    if debug:
        payload["debug"] = str(debug)

    if totals is not None:
        payload["totals"] = totals

    return jsonify(payload), status

# ---------- Cash Book (used by finance.cashbook template) ----------
@pg_bp.route("/api/cashbook/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_cashbook(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)

    year, month = _get_period_from_args()
    coll = "pg_cashbooks"

    pg_oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    if not pg_oid:
        return jsonify({"ok": False, "error": "Invalid PG ID."}), 400

    base_q = {"pg_id": pg_oid}

    if request.method == "GET":
        if request.args.get("history") in ("1", "true", "yes"):
            return jsonify({"history": _period_history(db, coll, base_q)})

        doc = None

        if year and month:
            doc = db[coll].find_one({**base_q, "year": year, "month": month})
        else:
            doc = db[coll].find_one(
                base_q,
                sort=[("year", -1), ("month", -1), ("updated_at", -1), ("created_at", -1)]
            )

            if doc:
                year = doc.get("year")
                month = doc.get("month")

        pg_doc = db.pgs.find_one({"_id": pg_oid}, {"name": 1, "pg_name": 1, "PG Name": 1}) or {}
        pg_name = pg_doc.get("name") or pg_doc.get("pg_name") or pg_doc.get("PG Name") or ""

        opening = _cashbook_opening_for_period(db, pg_oid, year, month, current_doc=doc) if (year and month) else {
            "cash": 0.0,
            "bank": 0.0,
            "total": 0.0,
            "source": "manual",
            "readonly": False,
        }

        if not doc:
            receipts = []
            payments = []
            totals = _cashbook_calculate_totals(opening, receipts, payments)

            return jsonify({
                "ok": True,
                "exists": False,
                "pg_id": str(pg_oid),
                "pg_name": pg_name,
                "year": year,
                "month": month,
                "opening": opening,
                "receipts": receipts,
                "payments": payments,
                "totals": totals,
                "meta": {},
            })

        doc = _cashbook_json_safe_doc(doc)

        receipts = _cashbook_clean_rows(doc.get("receipts") or [])
        payments = _cashbook_clean_rows(doc.get("payments") or [])

        # For old docs where totals are missing/wrong, return recalculated totals.
        totals = _cashbook_calculate_totals(opening, receipts, payments)

        doc["ok"] = True
        doc["exists"] = True
        doc["pg_name"] = pg_name
        doc["year"] = year or doc.get("year")
        doc["month"] = month or doc.get("month")
        doc["opening"] = opening
        doc["receipts"] = receipts
        doc["payments"] = payments
        doc["totals"] = totals

        return jsonify(doc)

    # POST: only PG login can write cashbook.
    role = getattr(g, "role", None) or session.get("role")
    if role != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}

    # Support both query/form period and JSON period.
    payload_year = _cashbook_int(payload.get("year"), year)
    payload_month = _cashbook_int(payload.get("month"), month)

    year = payload_year
    month = payload_month

    if not year or not month:
        return jsonify({
            "ok": False,
            "error": "Cashbook year and month are required.",
        }), 400

    if month < 1 or month > 12:
        return jsonify({
            "ok": False,
            "error": "Invalid cashbook month.",
        }), 400

    existing_doc = db[coll].find_one({**base_q, "year": year, "month": month})

    receipts = _cashbook_clean_rows(payload.get("receipts", []))
    payments = _cashbook_clean_rows(payload.get("payments", []))

    valid, error = _cashbook_validate_row_dates(receipts, year, month, "Receipt")
    if not valid:
        return jsonify({"ok": False, "error": error}), 400

    valid, error = _cashbook_validate_row_dates(payments, year, month, "Payment")
    if not valid:
        return jsonify({"ok": False, "error": error}), 400

    # Opening source:
    # - existing saved month: keep posted opening unless payload explicitly sends opening
    # - first unsaved month: use payload opening if manual, otherwise carried-forward previous closing
    payload_opening = _cashbook_opening_from_payload(payload)

    if existing_doc:
        existing_opening = _cashbook_opening_for_period(db, pg_oid, year, month, current_doc=existing_doc)

        # If frontend sends opening, accept it only if old source/manual or existing doc had no opening.
        existing_has_opening = isinstance(existing_doc.get("opening"), dict) and existing_doc.get("opening")

        if existing_has_opening:
            opening = {
                "cash": existing_opening["cash"],
                "bank": existing_opening["bank"],
                "total": existing_opening["total"],
                "source": existing_opening.get("source") or "saved",
                "readonly": True,
            }
        else:
            opening = payload_opening
            opening["readonly"] = payload_opening.get("source") != "manual"
    else:
        carried_opening = _cashbook_opening_for_period(db, pg_oid, year, month, current_doc=None)

        # If no previous record exists, user can set manual opening from frontend.
        if carried_opening.get("source") == "manual":
            opening = payload_opening
            opening["source"] = "manual"
            opening["readonly"] = False
        else:
            opening = carried_opening

    totals = _cashbook_calculate_totals(opening, receipts, payments)

    if totals["closing_cash"] < -0.009:
        return _cashbook_error_response(
            "Cash payment exceeds available cash balance.",
            status=400,
            title="Insufficient Cash Balance",
            details=(
            "Cash balance cannot be negative.\n\n"
            "Formula:\n"
            "Closing Cash = Opening Cash + Real Receipt Cash + Bank to Cash Transfer "
            "- Real Payment Cash - Cash to Bank Transfer\n\n"
            "Please add opening cash / receipt cash, or reduce cash payment / cash-to-bank transfer."
            ),
            totals=totals,
        )

    if totals["closing_bank"] < -0.009:
        return _cashbook_error_response(
            "Bank payment exceeds available bank balance.",
            status=400,
            title="Insufficient Bank Balance",
            details=(
                "Bank balance cannot be negative.\n\n"
                "Formula:\n"
                "Closing Bank = Opening Bank + Real Receipt Bank + Cash to Bank Transfer "
                "- Real Payment Bank - Bank to Cash Transfer\n\n"
                "Please add opening bank / receipt bank, or reduce bank payment / bank-to-cash transfer."
            ),
            totals=totals,
        )

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    save_payload = {
        "opening": {
            "cash": round(_cashbook_float(opening.get("cash")), 2),
            "bank": round(_cashbook_float(opening.get("bank")), 2),
            "total": round(_cashbook_float(opening.get("cash")) + _cashbook_float(opening.get("bank")), 2),
            "source": opening.get("source") or "manual",
            "source_year": opening.get("source_year"),
            "source_month": opening.get("source_month"),
        },
        "opening_cash": totals["opening_cash"],
        "opening_bank": totals["opening_bank"],
        "receipts": receipts,
        "payments": payments,
        "totals": totals,
        "meta": meta,
    }

    try:
        _upsert_pg_period_doc(
            db,
            collection=coll,
            pg_id=pg_id,
            year=year,
            month=month,
            payload=save_payload,
            user=_current_user_dict(),
        )

        saved_doc = db[coll].find_one({**base_q, "year": year, "month": month})
        saved_doc = _cashbook_json_safe_doc(saved_doc or {})

        return jsonify({
            "ok": True,
            "message": "Cash Book saved successfully.",
            "year": year,
            "month": month,
            "opening": save_payload["opening"],
            "receipts": receipts,
            "payments": payments,
            "totals": totals,
            "doc": saved_doc,
        })

    except Exception as e:
        current_app.logger.exception("Cash Book save failed")

        return _cashbook_error_response(
            "Backend failed while saving Cash Book.",
            status=500,
            title="Save failed",
            details="The server could not write the Cash Book record. Check the technical issue below.",
            debug=str(e),
            totals=totals,
        )

# ---------- Ledger Book ----------
@pg_bp.route("/ledger/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER","CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_ledger_book(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()

    coll = "pg_ledger_books"

    if request.method == "GET":
        base_q = {"pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))}
        if request.args.get("history") in ("1", "true", "yes"):
            return jsonify({"history": _period_history(db, coll, base_q)})
        doc = db[coll].find_one({**base_q, "year": year, "month": month}) if (year and month) else db[coll].find_one(base_q, sort=[("updated_at", -1)])
        if not doc:
            pg_doc = db.pgs.find_one(
                {"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))},
                {"name": 1, "Block": 1}
            ) or {}

            return jsonify({
                "entries": [],
                "pg_name": pg_doc.get("name", ""),
                "block_name": pg_doc.get("Block", ""),
                "year": year,
                "month": month
            })
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        pg_doc = db.pgs.find_one(
            {"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))},
            {"name": 1, "Block": 1}
        ) or {}

        doc["pg_name"] = doc.get("pg_name") or pg_doc.get("name", "")
        doc["block_name"] = doc.get("block_name") or pg_doc.get("Block", "")
        return jsonify(doc)

    role = getattr(g, "role", None) or session.get("role")
    if role != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}
    _upsert_pg_period_doc(db, collection=coll, pg_id=pg_id, year=year, month=month, payload={
        "entries": payload.get("entries", []),
        "meta": payload.get("meta", {}),
    }, user=_current_user_dict())
    return jsonify({"ok": True, "message": "Saved successfully"})



# ---------- Receipt Voucher ----------
@pg_bp.route("/receipt-voucher/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_receipt_voucher(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)

    year, month = _get_period_from_args()
    coll = "pg_receipt_vouchers"

    pg_oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    pg_doc = db.pgs.find_one({"_id": pg_oid}, {"name": 1}) or {}
    current_pg_name = pg_doc.get("name", "")

    def _num(v):
        try:
            if v in ("", None):
                return 0
            return float(v)
        except Exception:
            return 0

    def _clean_entries(entries):
        cleaned = []
        if not isinstance(entries, list):
            return cleaned

        for row in entries:
            if not isinstance(row, dict):
                continue

            commodity = str(row.get("commodity", "") or "").strip()
            grade = str(row.get("grade", "") or "").strip()
            rate = _num(row.get("rate"))
            volume = _num(row.get("volume"))
            value = _num(row.get("value"))

            if not commodity and not grade and not rate and not volume and not value:
                continue

            cleaned.append({
                "commodity": commodity,
                "grade": grade,
                "rate": rate,
                "volume": volume,
                "value": value,
            })

        return cleaned

    def _clean_signatures(signatures):
        if not isinstance(signatures, dict):
            signatures = {}
        return {
            "member": signatures.get("member", "") or "",
            "udyog": signatures.get("udyog", "") or "",
        }

    def _has_signature(signatures):
        signatures = signatures or {}
        return bool(signatures.get("member") or signatures.get("udyog"))

    def _has_member_data(member):
        if not isinstance(member, dict):
            return False

        entries = _clean_entries(member.get("entries", []))
        signatures = _clean_signatures(member.get("signatures", {}))

        return bool(
            entries
            or str(member.get("txn_date", "") or "").strip()
            or str(member.get("details", "") or "").strip()
            or _has_signature(signatures)
        )

    def _member_identity(member):
        """Keep a member's receipts separate by stable member ID and date.

        Older receipt-voucher documents contain only a name, so name remains
        the fallback identity for backward compatibility.
        """
        member_id = str(member.get("member_id") or "").strip()
        member_name = str(member.get("member_name", "") or "").strip()
        txn_date = str(member.get("txn_date", "") or "").strip()
        identity = member_id or member_name
        return f"{identity}::{txn_date}" if identity else ""

    def _split_legacy_member_key(key):
        key = str(key or "").strip()
        if not key or key == "__no_member__":
            return "", ""

        if "__" in key:
            code, name = key.split("__", 1)
            return code.strip(), name.strip()

        return "", key

    def _make_member(raw, fallback_code="", fallback_name=""):
        if not isinstance(raw, dict):
            raw = {}

        member = {
            "member_id": str(raw.get("member_id", "") or "").strip(),
            "member_code": str(raw.get("member_code", "") or "").strip(),
            "member_name": str(raw.get("member_name", fallback_name) or fallback_name or "").strip(),
            "txn_date": str(raw.get("txn_date", "") or "").strip(),
            "details": str(raw.get("details", "") or "").strip(),
            "entries": _clean_entries(raw.get("entries", [])),
            "signatures": _clean_signatures(raw.get("signatures", {})),
        }

        return member

    def _members_from_payload_section(section):
        """
        Accepts both new and old frontend shapes.

        New supported:
        {
          "members": [...]
        }

        or:
        {
          "member": {...}
        }

        Old supported:
        {
          "member_tables": {
            "300001__Name": {...}
          },
          "member_code": "...",
          "member_name": "...",
          "entries": [...]
        }
        """
        members = []

        if not isinstance(section, dict):
            return members

        # New format: members array
        if isinstance(section.get("members"), list):
            for item in section.get("members", []):
                member = _make_member(item)
                if _member_identity(member) and _has_member_data(member):
                    members.append(member)

        # New format: single member object
        if isinstance(section.get("member"), dict):
            member = _make_member(section.get("member"))
            if _member_identity(member) and _has_member_data(member):
                members.append(member)

        # Old format: member_tables object
        member_tables = section.get("member_tables", {})
        if isinstance(member_tables, dict):
            for key, table_data in member_tables.items():
                fallback_code, fallback_name = _split_legacy_member_key(key)
                member = _make_member(table_data, fallback_code=fallback_code, fallback_name=fallback_name)

                if _member_identity(member) and _has_member_data(member):
                    members.append(member)

        # Old format: top-level section fields
        top_level_member = _make_member(section)
        if _member_identity(top_level_member) and _has_member_data(top_level_member):
            members.append(top_level_member)

        # Deduplicate by member_name, keeping last version
        deduped = {}
        for member in members:
            key = _member_identity(member)
            if key:
                deduped[key] = member

        return list(deduped.values())

    def _normalize_section_from_doc(section):
        """
        Converts old or new stored structure into clean structure:
        {
          pg_name: "...",
          members: [...]
        }
        """
        normalized = {
            "pg_name": current_pg_name,
            "members": [],
        }

        if not isinstance(section, dict):
            return normalized

        if isinstance(section.get("members"), list):
            for item in section.get("members", []):
                member = _make_member(item)
                if _member_identity(member) and _has_member_data(member):
                    normalized["members"].append(member)

        legacy_members = _members_from_payload_section(section)
        if legacy_members:
            by_key = {_member_identity(m): m for m in normalized["members"] if _member_identity(m)}
            for member in legacy_members:
                by_key[_member_identity(member)] = member
            normalized["members"] = list(by_key.values())

        return normalized

    def _merge_members(existing_section, incoming_members):
        existing_section = existing_section if isinstance(existing_section, dict) else {}
        existing_members = existing_section.get("members", [])
        if not isinstance(existing_members, list):
            existing_members = []

        merged = {}

        for item in existing_members:
            member = _make_member(item)
            key = _member_identity(member)
            if key and _has_member_data(member):
                merged[key] = member

        for item in incoming_members:
            member = _make_member(item)
            key = _member_identity(member)
            if key and _has_member_data(member):
                merged[key] = member

        return {
            "pg_name": current_pg_name,
            "members": list(merged.values()),
        }

    def _legacy_section_for_frontend(clean_section):
        """
        Temporary backward-compatible response for receipt_voucher.html.
        Member code removed. Member-wise data is keyed by member_name only.
        """
        clean_section = clean_section if isinstance(clean_section, dict) else {}
        members = clean_section.get("members", [])
        if not isinstance(members, list):
            members = []

        member_tables = {}
        first_member = {}

        for member in members:
            member = _make_member(member)
            if not _member_identity(member):
                continue

            key = member.get("member_name", "")
            member_tables[key] = {
                "member_id": member.get("member_id", ""),
                "member_code": member.get("member_code", ""),
                "member_name": member.get("member_name", ""),
                "entries": member.get("entries", []),
                "details": member.get("details", ""),
                "txn_date": member.get("txn_date", ""),
                "signatures": member.get("signatures", {"member": "", "udyog": ""}),
            }

            if not first_member:
                first_member = member

        return {
            "pg_name": current_pg_name,
            "txn_date": first_member.get("txn_date", "") if first_member else "",
            "member_name": first_member.get("member_name", "") if first_member else "",
            "details": first_member.get("details", "") if first_member else "",
            "signatures": first_member.get("signatures", {"member": "", "udyog": ""}) if first_member else {"member": "", "udyog": ""},
            "entries": first_member.get("entries", []) if first_member else [],
            "member_tables": member_tables,
        }

    base_q = {"pg_id": pg_oid}

    if request.method == "GET":
        if request.args.get("history") in ("1", "true", "yes"):
            history = _period_history(db, coll, base_q)
            # Receipt voucher history also needs the member and transaction
            # dates so the page can offer a member/date selector.
            docs_by_id = {
                str(doc.get("_id")): doc
                for doc in db[coll].find(
                    base_q,
                    {"year": 1, "month": 1, "pg_group_receipt": 1,
                     "group_member_receipt": 1, "pg_section": 1,
                     "member_section": 1}
                ).sort([("year", -1), ("month", -1), ("updated_at", -1)])
            }
            for item in history:
                doc = docs_by_id.get(str(item.get("_id")), {})
                date_records = []
                for section_key in ("pg_group_receipt", "group_member_receipt"):
                    section = doc.get(section_key)
                    if not isinstance(section, dict):
                        legacy_key = "pg_section" if section_key == "pg_group_receipt" else "member_section"
                        section = doc.get(legacy_key) or {}
                    section = _normalize_section_from_doc(section)
                    members = section.get("members", [])
                    for raw_member in members:
                        member = _make_member(raw_member)
                        if not _member_identity(member) or not _has_member_data(member):
                            continue
                        date_records.append({
                            "section": section_key,
                            "member_id": member.get("member_id", ""),
                            "member_code": member.get("member_code", ""),
                            "member_name": member.get("member_name", ""),
                            "txn_date": member.get("txn_date", ""),
                            "details": member.get("details", ""),
                            "entries": member.get("entries", []),
                            "signatures": member.get("signatures", {"member": "", "udyog": ""}),
                        })
                item["date_records"] = date_records
            return jsonify({"history": history})

        if year and month:
            doc = db[coll].find_one({**base_q, "year": year, "month": month})
        else:
            doc = db[coll].find_one(base_q, sort=[("updated_at", -1)])

        if not doc:
            clean_pg_group = {
                "pg_name": current_pg_name,
                "members": [],
            }
            clean_group_member = {
                "pg_name": current_pg_name,
                "members": [],
            }

            return jsonify({
                "pg_group_receipt": clean_pg_group,
                "group_member_receipt": clean_group_member,

                # backward-compatible response for current frontend
                "pg_section": _legacy_section_for_frontend(clean_pg_group),
                "member_section": _legacy_section_for_frontend(clean_group_member),

                "year": year,
                "month": month,
            })

        clean_pg_group = _normalize_section_from_doc(
            doc.get("pg_group_receipt") or doc.get("pg_section") or {}
        )

        clean_group_member = _normalize_section_from_doc(
            doc.get("group_member_receipt") or doc.get("member_section") or {}
        )

        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])

        doc["pg_group_receipt"] = clean_pg_group
        doc["group_member_receipt"] = clean_group_member

        # backward-compatible response for current frontend
        doc["pg_section"] = _legacy_section_for_frontend(clean_pg_group)
        doc["member_section"] = _legacy_section_for_frontend(clean_group_member)

        return jsonify(doc)

    role = getattr(g, "role", None) or session.get("role")
    if role != "PG_DATA_ENTRY":
        abort(403)

    payload = request.get_json(silent=True) or {}

    existing_doc = db[coll].find_one({**base_q, "year": year, "month": month}) or {}

    existing_pg_group = _normalize_section_from_doc(
        existing_doc.get("pg_group_receipt") or existing_doc.get("pg_section") or {}
    )

    existing_group_member = _normalize_section_from_doc(
        existing_doc.get("group_member_receipt") or existing_doc.get("member_section") or {}
    )

    incoming_pg_members = []
    incoming_member_members = []

    # New frontend keys
    incoming_pg_members.extend(_members_from_payload_section(payload.get("pg_group_receipt", {})))
    incoming_member_members.extend(_members_from_payload_section(payload.get("group_member_receipt", {})))

    # Old frontend keys, for current receipt_voucher.html compatibility
    incoming_pg_members.extend(_members_from_payload_section(payload.get("pg_section", {})))
    incoming_member_members.extend(_members_from_payload_section(payload.get("member_section", {})))

    clean_pg_group = _merge_members(existing_pg_group, incoming_pg_members)
    clean_group_member = _merge_members(existing_group_member, incoming_member_members)

    _upsert_pg_period_doc(
        db,
        collection=coll,
        pg_id=pg_id,
        year=year,
        month=month,
        payload={
            "pg_group_receipt": clean_pg_group,
            "group_member_receipt": clean_group_member,
            "meta": payload.get("meta", {}),
        },
        user=_current_user_dict()
    )

    # Remove old messy top-level fields from MongoDB after saving clean structure.
    db[coll].update_one(
        {**base_q, "year": year, "month": month},
        {
            "$unset": {
                "pg_section": "",
                "member_section": "",
            }
        }
    )

    return jsonify({
        "ok": True,
        "message": "Saved successfully",
        "pg_group_receipt_members": len(clean_pg_group.get("members", [])),
        "group_member_receipt_members": len(clean_group_member.get("members", [])),
    })


#changes by atlanta
# ---------- Receipt Voucher Member Options ----------
@pg_bp.route("/receipt-voucher-members/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def api_receipt_voucher_members(pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)

    members = list(
        db.pg_members.find(
            {
                "pg_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id'))),
                "$or": [
                    {"is_active": True},
                    {"is_active": {"$exists": False}}
                ]
            },
            {
                "_id": 1,
                "name": 1,
                "member_name": 1,
                "member_code": 1,
                "shg_code": 1,
            }
        ).sort("name", 1)
    )

    result = []
    for m in members:
        member_name = (
            m.get("name")
            or m.get("member_name")
            or ""
        )
        member_code = (
            m.get("member_code")
            or m.get("shg_code")
            or ""
        )

        result.append({
            "_id": str(m.get("_id")),
            "member_name": str(member_name),
            "member_code": str(member_code),
        })

    return jsonify({
        "ok": True,
        "members": result
    })


@pg_bp.route("/api/register/<name>/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg', pg_id_param="pg_id")
def api_generic_register(name, pg_id):
    db = current_app.mongo_db
    _load_pg_or_404(db, pg_id)
    year, month = _get_period_from_args()

    # Mobile clients may send the selected output-register period in JSON
    # instead of the query string. Normalize it before building Mongo queries.
    if name == "output":
        request_payload = request.get_json(silent=True) or {}
        if not isinstance(request_payload, dict):
            request_payload = {}
        request_period = request_payload.get("period") if isinstance(request_payload.get("period"), dict) else {}
        if year is None:
            raw_year = request_payload.get("year", request_period.get("year"))
            try:
                year = int(raw_year) if raw_year not in (None, "", "null") else None
            except (TypeError, ValueError):
                year = None
        if month is None:
            raw_month = request_payload.get("month", request_period.get("month"))
            try:
                month = int(raw_month) if raw_month not in (None, "", "null") else None
            except (TypeError, ValueError):
                month = None

    allowed = {
        "input": "pg_input_registers",
        "output": "pg_output_registers",
        "asset": "pg_asset_registers",
        "minutes": "pg_meeting_minutes",
        "member_ledger": "pg_member_ledgers",
    }

    if name not in allowed:
        return jsonify({"ok": False, "error": f"Invalid register name: {name}"}), 404

    coll = allowed[name]
    base_pg_id = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))

    def _norm_name(value):
        return " ".join(str(value or "").strip().split())

    def _name_key(value):
        return _norm_name(value).lower()

    def _producer_key(value):
        key = _name_key(value)
        safe = []

        for ch in key:
            if ch.isalnum():
                safe.append(ch)
            elif ch in (" ", "-", "_"):
                safe.append("_")

        key = "".join(safe).strip("_")

        while "__" in key:
            key = key.replace("__", "_")

        return key[:120] or "producer"
       

    def _safe_regex_exact(value):
        value = str(value or "")
        for ch in ["\\", ".", "+", "*", "?", "^", "$", "(", ")", "[", "]", "{", "}", "|"]:
            value = value.replace(ch, "\\" + ch)
        return f"^{value}$"

    def _serialize_doc(doc):
        if not doc:
            return doc
        doc["_id"] = str(doc["_id"])
        doc["pg_id"] = str(doc["pg_id"])
        return doc

    def _base_period_query():
        q = {"pg_id": base_pg_id}
        if name == "output" and base_pg_id is not None:
            # Older/mobile writes may retain pg_id and period values as
            # strings. Accept those alongside the canonical ObjectId/integer
            # values so the web register can find the same saved record.
            q["pg_id"] = {"$in": [base_pg_id, str(base_pg_id)]}
        if name == "output":
            # Mobile versions have stored period values at the document root,
            # inside data, or inside data.meta.
            period_clauses = []
            if year is not None:
                year_values = [int(year), str(int(year))]
                period_clauses.append({"$or": [
                    {"year": {"$in": year_values}},
                    {"data.year": {"$in": year_values}},
                    {"data.meta.periodYear": {"$in": year_values}},
                    {"period.year": {"$in": year_values}},
                ]})
            if month is not None:
                month_values = [int(month), str(int(month))]
                period_clauses.append({"$or": [
                    {"month": {"$in": month_values}},
                    {"data.month": {"$in": month_values}},
                    {"data.meta.periodMonth": {"$in": month_values}},
                    {"period.month": {"$in": month_values}},
                ]})
            if period_clauses:
                q["$and"] = period_clauses
        else:
            if year is not None:
                q["year"] = int(year)
            if month is not None:
                q["month"] = int(month)
        return q

    def _blank_pg_name():
        pg_doc = db.pgs.find_one({"_id": base_pg_id}, {"name": 1}) or {}
        return pg_doc.get("name", "")

    def _get_saved_names(register_name):
        """
        For input:
            root: input_name
            meta: data.meta.regInputName

        For output:
            root: output_name
            meta: data.meta.regOutputName
            fallback: data.produceName
        """
        q = _base_period_query()

        if register_name == "input":
            root_field = "input_name"
            meta_field = "data.meta.regInputName"
            projection = {
                "input_name": 1,
                "data.meta.regInputName": 1,
                "updated_at": 1,
                "created_at": 1,
            }
        elif register_name == "output":
            root_field = "output_name"
            meta_field = "data.meta.regOutputName"
            projection = {
                "output_name": 1,
                "outputName": 1,
                "produceName": 1,
                "produce_name": 1,
                "data.meta.regOutputName": 1,
                "data.produceName": 1,
                "data.produce_name": 1,
                "data.output_name": 1,
                "data.outputName": 1,
                "updated_at": 1,
                "created_at": 1,
            }
        else:
            return []

        docs = list(
            db[coll].find(q, projection).sort([("updated_at", -1), ("created_at", -1)])
        )

        seen = set()
        names = []

        for d in docs:
            data = d.get("data") or {}
            meta = data.get("meta") or {}

            if register_name == "input":
                val = d.get(root_field) or meta.get("regInputName") or ""
            else:
                val = (
                    d.get(root_field)
                    or d.get("outputName")
                    or d.get("produceName")
                    or d.get("produce_name")
                    or meta.get("regOutputName")
                    or data.get("produceName")
                    or data.get("produce_name")
                    or data.get("output_name")
                    or data.get("outputName")
                    or ""
                )

            val = _norm_name(val)
            if not val:
                continue

            k = val.lower()
            if k in seen:
                continue

            seen.add(k)
            names.append(val)

        return names

    def _get_selected_name_from_args(register_name):
        if register_name == "input":
            return _norm_name(
                request.args.get("input_name")
                or request.args.get("regInputName")
                or request.args.get("search")
                or ""
            )

        if register_name == "output":
            return _norm_name(
                request.args.get("output_name")
                or request.args.get("produce_name")
                or request.args.get("regOutputName")
                or request.args.get("search")
                or ""
            )

        return ""

    def _find_named_doc(register_name, selected_name):
        selected_key = _name_key(selected_name)
        base_q = _base_period_query()

        if not selected_key:
            return None

        if register_name == "input":
            return db[coll].find_one(
                {
                    **base_q,
                    "$or": [
                        {"input_key": selected_key},
                        {"input_name": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.meta.regInputName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                    ],
                },
                sort=[("updated_at", -1), ("created_at", -1)],
            )

        if register_name == "output":
            return db[coll].find_one(
                {
                    **base_q,
                    "$or": [
                        {"output_key": selected_key},
                        {"output_name": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"outputName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"produceName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"produce_name": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.meta.regOutputName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.produceName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.produce_name": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.output_name": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                        {"data.outputName": {"$regex": _safe_regex_exact(selected_name), "$options": "i"}},
                    ],
                },
                sort=[("updated_at", -1), ("created_at", -1)],
            )

        return None

    def _input_register_num(value):
        try:
            if value in (None, "", "null", "None"):
                return 0.0
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    def _normalize_input_stock_rows(raw_data):
        """
        Keep Input Register stock consistent for every client.

        st15 = cumulative remaining quantity (purchase qty - sale qty)
        st16 = optional stock rate (never silently copied from purchase rate)
        st17 = remaining quantity * stock rate only when a stock rate exists
        """
        data = dict(raw_data) if isinstance(raw_data, dict) else {}
        rows = data.get("rows")
        if not isinstance(rows, list):
            return data

        running_stock = 0.0
        normalized_rows = []

        for raw_row in rows:
            if not isinstance(raw_row, dict):
                continue

            row = dict(raw_row)
            running_stock = round(
                running_stock
                + _input_register_num(row.get("p4"))
                - _input_register_num(row.get("s11")),
                2,
            )
            row["st15"] = running_stock

            raw_stock_rate = row.get("st16")
            has_stock_rate = (
                raw_stock_rate is not None
                and str(raw_stock_rate).strip() != ""
            )

            if has_stock_rate:
                row["st17"] = round(
                    running_stock * _input_register_num(raw_stock_rate),
                    2,
                )
            else:
                row["st16"] = ""
                row["st17"] = ""

            normalized_rows.append(row)

        data["rows"] = normalized_rows
        return data

    def _blank_input_response(selected_name=""):
        selected_name = _norm_name(selected_name)
        selected_key = _name_key(selected_name)

        return {
            "ok": True,
            "is_new_input": bool(selected_name),
            "saved_input_names": _get_saved_names("input"),
            "year": year,
            "month": month,
            "input_name": selected_name,
            "input_key": selected_key,
            "unit_of_stocking": "",
            "data": {
                "pg_name": _blank_pg_name(),
                "meta": {
                    "regInputName": selected_name,
                    "regInputUnit": "",
                    "regUnit": "",
                },
                "rows": [],
                "purchaseRows": [],
                "saleRows": [],
                "stockRows": [],
            },
        }

    def _blank_output_response(selected_name=""):
        selected_name = _norm_name(selected_name)
        selected_key = _name_key(selected_name)

        return {
            "ok": True,
            "is_new_output": bool(selected_name),
            "saved_output_names": _get_saved_names("output"),
            "year": year,
            "month": month,
            "output_name": selected_name,
            "output_key": selected_key,
            "unit_of_stocking": "",
            "data": {
                "pg_name": _blank_pg_name(),
                "produceName": selected_name,
                "unitStock": "",
                "activeTab": "all",
                "meta": {
                    "regOutputName": selected_name,
                    "regOutputUnit": "",
                    "regUnit": "",
                },
                "rows": [],
            },
        }
    
    def _normalize_member_ledger_data(raw_data):
        data = raw_data if isinstance(raw_data, dict) else {}

        pg_name = data.get("pgName") or data.get("pg_name") or _blank_pg_name()
        active_tab = data.get("activeTab") if data.get("activeTab") in ("purchases", "sales") else "purchases"

        producers = data.get("producers")
        clean_producers = []

        if isinstance(producers, list):
            for idx, item in enumerate(producers):
                if not isinstance(item, dict):
                    continue

                producer_name = _norm_name(item.get("producerName") or item.get("producer_name") or "")
                stocking_unit = str(item.get("stockingUnit") or item.get("stocking_unit") or "").strip()

                purchases = item.get("purchases") if isinstance(item.get("purchases"), list) else []
                sales = item.get("sales") if isinstance(item.get("sales"), list) else []

                producer_id = str(item.get("producerId") or item.get("producer_id") or "").strip()
                if not producer_id:
                    base = _producer_key(producer_name) if producer_name else f"producer_{idx + 1}"
                    producer_id = base

                clean_producers.append({
                    "producerId": producer_id,
                    "producerName": producer_name,
                    "stockingUnit": stocking_unit,
                    "purchases": purchases,
                    "sales": sales,
                })

        # Backward compatibility for old single-producer saved data
        if not clean_producers:
            old_producer_name = _norm_name(data.get("producerName") or data.get("producer_name") or "")
            old_stocking_unit = str(data.get("stockingUnit") or data.get("stocking_unit") or "").strip()
            old_purchases = data.get("purchases") if isinstance(data.get("purchases"), list) else []
            old_sales = data.get("sales") if isinstance(data.get("sales"), list) else []

            if old_producer_name or old_stocking_unit or old_purchases or old_sales:
                clean_producers.append({
                    "producerId": _producer_key(old_producer_name) if old_producer_name else "producer_1",
                    "producerName": old_producer_name,
                    "stockingUnit": old_stocking_unit,
                    "purchases": old_purchases,
                    "sales": old_sales,
                })

        # Ensure unique producer IDs
        seen = {}
        unique_producers = []

        for item in clean_producers:
            base_id = _producer_key(item.get("producerId") or item.get("producerName") or "producer")
            final_id = base_id
            counter = 2

            while final_id in seen:
                final_id = f"{base_id}_{counter}"
                counter += 1

            seen[final_id] = True
            item["producerId"] = final_id
            unique_producers.append(item)

        active_producer_id = str(data.get("activeProducerId") or "").strip()

        if not active_producer_id and unique_producers:
            active_producer_id = unique_producers[0]["producerId"]

        if active_producer_id and not any(p.get("producerId") == active_producer_id for p in unique_producers):
            active_producer_id = unique_producers[0]["producerId"] if unique_producers else ""

        active_producer = None
        for producer in unique_producers:
            if producer.get("producerId") == active_producer_id:
                active_producer = producer
                break

        if active_producer is None:
            active_producer = {}

        return {
            "pgName": pg_name,
            "producerName": active_producer.get("producerName", ""),
            "stockingUnit": active_producer.get("stockingUnit", ""),
            "purchases": active_producer.get("purchases", []),
            "sales": active_producer.get("sales", []),
            "producers": unique_producers,
            "activeProducerId": active_producer_id,
            "activeTab": active_tab,
            "isDraft": bool(data.get("isDraft", False)),
            "savedAt": data.get("savedAt") or datetime.utcnow().isoformat(),
        }

    def _blank_member_ledger_response():
        return {
            "ok": True,
            "year": year,
            "month": month,
            "data": {
                "pgName": _blank_pg_name(),
                "producerName": "",
                "stockingUnit": "",
                "purchases": [],
                "sales": [],
                "producers": [],
                "activeProducerId": "",
                "activeTab": "purchases",
            },
        }

    # ============================================================
    # GET
    # ============================================================
    if request.method == "GET":

        # --------------------------------------------------------
        # INPUT REGISTER SPECIAL FLOW
        # Save/load separately by:
        # pg_id + year + month + input_key
        # --------------------------------------------------------
        if name == "input":
            selected_input_name = _get_selected_name_from_args("input")

            if selected_input_name:
                doc = _find_named_doc("input", selected_input_name)

                if not doc:
                    return jsonify(_blank_input_response(selected_input_name)), 200

                doc = _serialize_doc(doc)
                doc["data"] = _normalize_input_stock_rows(doc.get("data") or {})
                doc["ok"] = True
                doc["saved_input_names"] = _get_saved_names("input")
                doc["input_name"] = (
                    doc.get("input_name")
                    or (((doc.get("data") or {}).get("meta") or {}).get("regInputName"))
                    or selected_input_name
                )
                doc["input_key"] = doc.get("input_key") or _name_key(doc.get("input_name"))
                doc["unit_of_stocking"] = (
                    doc.get("unit_of_stocking")
                    or (((doc.get("data") or {}).get("meta") or {}).get("regInputUnit"))
                    or (((doc.get("data") or {}).get("meta") or {}).get("regUnit"))
                    or ""
                )
                return jsonify(doc), 200

            # No selected input: return blank + saved names only.
            return jsonify(_blank_input_response("")), 200

        # --------------------------------------------------------
        # OUTPUT REGISTER SPECIAL FLOW
        # Save/load separately by:
        # pg_id + year + month + output_key
        # Example:
        # /pg/api/register/output/<pg_id>?year=2026&month=5
        # /pg/api/register/output/<pg_id>?year=2026&month=5&output_name=Paddy
        # --------------------------------------------------------
        if name == "output":
            selected_output_name = _get_selected_name_from_args("output")

            # History should remain available, but unique by month.
            if request.args.get("history") in ("1", "true", "yes"):
                q = {"pg_id": {"$in": [base_pg_id, str(base_pg_id)]}}
                docs = list(
                    db[coll].find(
                        q,
                        {
                            "year": 1,
                            "month": 1,
                            "updated_at": 1,
                            "created_at": 1,
                        },
                    ).sort([("year", -1), ("month", -1), ("updated_at", -1)])
                )

                seen = set()
                history = []
                for d in docs:
                    y = d.get("year")
                    m = d.get("month")
                    if not y or not m:
                        continue
                    k = f"{y}-{m}"
                    if k in seen:
                        continue
                    seen.add(k)
                    history.append({
                        "year": y,
                        "month": m,
                        "updated_at": d.get("updated_at"),
                        "created_at": d.get("created_at"),
                    })

                return jsonify({"ok": True, "history": history}), 200

            if selected_output_name:
                doc = _find_named_doc("output", selected_output_name)

                if not doc:
                    return jsonify(_blank_output_response(selected_output_name)), 200

                doc = _serialize_doc(doc)
                data = doc.get("data") or {}
                if not isinstance(data, dict):
                    data = {}
                # Mobile builds have used both document-root and nested
                # payloads. Merge root fields even when data contains only
                # metadata, so the web does not discard saved rows.
                for key in (
                    "rows", "items", "entries", "records", "list", "output_rows",
                    "outputRows", "stock_rows", "stockRows", "saleRows",
                    "produceName", "produce_name", "output_name", "outputName",
                    "unitStock", "unit_of_stocking", "activeTab", "meta",
                ):
                    if key not in data and key in doc:
                        data[key] = doc[key]
                meta = data.get("meta") or {}

                doc["ok"] = True
                doc["saved_output_names"] = _get_saved_names("output")
                doc["output_name"] = (
                    doc.get("output_name")
                    or doc.get("outputName")
                    or doc.get("produceName")
                    or doc.get("produce_name")
                    or meta.get("regOutputName")
                    or data.get("produceName")
                    or data.get("produce_name")
                    or data.get("output_name")
                    or data.get("outputName")
                    or selected_output_name
                )
                doc["output_key"] = doc.get("output_key") or _name_key(doc.get("output_name"))
                doc["unit_of_stocking"] = (
                    doc.get("unit_of_stocking")
                    or meta.get("regOutputUnit")
                    or meta.get("regUnit")
                    or data.get("unitStock")
                    or data.get("unit_of_stocking")
                    or ""
                )

                if not isinstance(data.get("meta"), dict):
                    data["meta"] = {}
                data["meta"]["regOutputName"] = doc["output_name"]
                data["meta"]["regOutputUnit"] = doc["unit_of_stocking"]
                data["produceName"] = doc["output_name"]
                data["unitStock"] = doc["unit_of_stocking"]
                if not isinstance(data.get("rows"), list):
                    data["rows"] = next(
                        (
                            data.get(key)
                            for key in (
                                "items", "entries", "records", "list", "output_rows",
                                "outputRows", "stock_rows", "stockRows", "saleRows",
                            )
                            if isinstance(data.get(key), list)
                        ),
                        [],
                    )

                doc["data"] = data

                return jsonify(doc), 200

            # No selected produce: return blank + saved output names only.
            return jsonify(_blank_output_response("")), 200


        # --------------------------------------------------------
        # MEMBER LEDGER SPECIAL FLOW
        # Save/load producer-wise data separately inside one PG/month document:
        # data.producers[].producerName
        # data.producers[].stockingUnit
        # data.producers[].purchases
        # data.producers[].sales
        # --------------------------------------------------------
        if name == "member_ledger":
            base_q = {"pg_id": base_pg_id}

            if request.args.get("history") in ("1", "true", "yes"):
                return jsonify({"history": _period_history(db, coll, base_q)})

            doc_id = (request.args.get("doc_id") or "").strip()
            if doc_id:
                doc = db[coll].find_one({**base_q, "_id": safe_objectid(doc_id)})
                if not doc:
                    return jsonify({"ok": False, "error": "Record not found"}), 404

                doc = _serialize_doc(doc)
                doc["ok"] = True
                doc["data"] = _normalize_member_ledger_data(doc.get("data") or {})
                return jsonify(doc), 200

            doc = (
                db[coll].find_one({**base_q, "year": year, "month": month})
                if (year and month)
                else db[coll].find_one(base_q, sort=[("updated_at", -1)])
            )

            if not doc:
                return jsonify(_blank_member_ledger_response()), 200

            doc = _serialize_doc(doc)
            doc["ok"] = True
            doc["data"] = _normalize_member_ledger_data(doc.get("data") or {})
            return jsonify(doc), 200
        # --------------------------------------------------------
        # EXISTING GENERIC GET FLOW FOR OTHER REGISTERS
        # --------------------------------------------------------
        base_q = {"pg_id": base_pg_id}

        if request.args.get("history") in ("1", "true", "yes"):
            return jsonify({"history": _period_history(db, coll, base_q)})

        doc_id = (request.args.get("doc_id") or "").strip()
        if doc_id:
            doc = db[coll].find_one({**base_q, "_id": safe_objectid(doc_id)})
            if not doc:
                return jsonify({"ok": False, "error": "Record not found"}), 404
            return jsonify(_serialize_doc(doc)), 200

        doc = (
            db[coll].find_one({**base_q, "year": year, "month": month})
            if (year and month)
            else db[coll].find_one(base_q, sort=[("updated_at", -1)])
        )

        if not doc:
            return jsonify({
                "ok": True,
                "data": {"pg_name": _blank_pg_name()},
                "year": year,
                "month": month,
            }), 200

        return jsonify(_serialize_doc(doc)), 200

    # ============================================================
    # POST
    # ============================================================
    role = getattr(g, "role", None) or session.get("role")

    if role not in ("PG_DATA_ENTRY", "CADRE_CC"):
        return jsonify({"ok": False, "error": f"Forbidden for role: {role}"}), 403

    payload = request.get_json(silent=True) or {}

    try:
        # --------------------------------------------------------
        # INPUT REGISTER SPECIAL SAVE FLOW
        # Save Fish / Chicken / Seed separately using:
        # pg_id + year + month + input_key
        # --------------------------------------------------------
        if name == "input":
            data = payload.get("data", payload) or {}
            meta = data.get("meta") or {}

            input_name = _norm_name(
                payload.get("input_name")
                or payload.get("regInputName")
                or meta.get("regInputName")
                or data.get("regInputName")
                or ""
            )

            if not input_name:
                return jsonify({
                    "ok": False,
                    "error": "Name of Input is required before saving."
                }), 400

            input_key = _name_key(input_name)

            data.setdefault("meta", {})
            data["meta"]["regInputName"] = input_name

            unit_of_stocking = (
                payload.get("unit_of_stocking")
                or payload.get("regInputUnit")
                or data["meta"].get("regInputUnit")
                or data["meta"].get("regUnit")
                or data.get("regInputUnit")
                or ""
            )

            data["meta"]["regInputUnit"] = unit_of_stocking
            data["meta"]["regUnit"] = unit_of_stocking
            data = _normalize_input_stock_rows(data)

            now = datetime.utcnow()

            q = {
                "pg_id": base_pg_id,
                "input_key": input_key,
            }

            if year is not None:
                q["year"] = int(year)
            if month is not None:
                q["month"] = int(month)

            before = db[coll].find_one(q)

            doc_set = {
                "pg_id": base_pg_id,
                "year": int(year) if year is not None else None,
                "month": int(month) if month is not None else None,
                "input_name": input_name,
                "input_key": input_key,
                "unit_of_stocking": unit_of_stocking,
                "data": data,
                "updated_at": now,
            }

            if before:
                db[coll].update_one({"_id": before["_id"]}, {"$set": doc_set})
                saved_id = before["_id"]

                log_audit(
                    db,
                    action=f"{coll}:update",
                    collection=coll,
                    doc_id=before["_id"],
                    user=_current_user_dict(),
                    before=before,
                    after=doc_set,
                    meta={
                        "pg_id": pg_id,
                        "year": year,
                        "month": month,
                        "input_name": input_name,
                    },
                )
            else:
                doc_set["created_at"] = now
                res = db[coll].insert_one(doc_set)
                saved_id = res.inserted_id

                log_audit(
                    db,
                    action=f"{coll}:create",
                    collection=coll,
                    doc_id=saved_id,
                    user=_current_user_dict(),
                    after=doc_set,
                    meta={
                        "pg_id": pg_id,
                        "year": year,
                        "month": month,
                        "input_name": input_name,
                    },
                )

            return jsonify({
                "ok": True,
                "message": f"{input_name} saved successfully.",
                "id": str(saved_id),
                "input_name": input_name,
                "input_key": input_key,
                "saved_input_names": _get_saved_names("input"),
            }), 200

        # --------------------------------------------------------
        # OUTPUT REGISTER SPECIAL SAVE FLOW
        # Save Paddy / Fish / Vegetable separately using:
        # pg_id + year + month + output_key
        # --------------------------------------------------------
        if name == "output":
            data = payload.get("data", payload) or {}
            meta = data.get("meta") or {}

            output_name = _norm_name(
                payload.get("output_name")
                or payload.get("produce_name")
                or payload.get("regOutputName")
                or meta.get("regOutputName")
                or data.get("produceName")
                or data.get("regOutputName")
                or ""
            )

            if not output_name:
                return jsonify({
                    "ok": False,
                    "error": "Name of Produce is required before saving."
                }), 400

            output_key = _name_key(output_name)

            unit_of_stocking = (
                payload.get("unit_of_stocking")
                or payload.get("regOutputUnit")
                or meta.get("regOutputUnit")
                or meta.get("regUnit")
                or data.get("unitStock")
                or data.get("regOutputUnit")
                or ""
            )

            data.setdefault("meta", {})
            data["meta"]["regOutputName"] = output_name
            data["meta"]["regOutputUnit"] = unit_of_stocking
            data["meta"]["regUnit"] = unit_of_stocking
            data["produceName"] = output_name
            data["unitStock"] = unit_of_stocking
            data.setdefault("rows", [])

            now = datetime.utcnow()

            q = {**_base_period_query(), "output_key": output_key}

            before = db[coll].find_one(q)
            if not before:
                # Legacy/mobile records may have the produce name but no
                # output_key. Reuse that record so web Save updates it.
                before = _find_named_doc("output", output_name)

            doc_set = {
                "pg_id": base_pg_id,
                "year": int(year) if year is not None else None,
                "month": int(month) if month is not None else None,
                "output_name": output_name,
                "output_key": output_key,
                "unit_of_stocking": unit_of_stocking,
                "data": data,
                "updated_at": now,
            }

            if before:
                db[coll].update_one({"_id": before["_id"]}, {"$set": doc_set})
                saved_id = before["_id"]

                log_audit(
                    db,
                    action=f"{coll}:update",
                    collection=coll,
                    doc_id=before["_id"],
                    user=_current_user_dict(),
                    before=before,
                    after=doc_set,
                    meta={
                        "pg_id": pg_id,
                        "year": year,
                        "month": month,
                        "output_name": output_name,
                    },
                )
            else:
                doc_set["created_at"] = now
                res = db[coll].insert_one(doc_set)
                saved_id = res.inserted_id

                log_audit(
                    db,
                    action=f"{coll}:create",
                    collection=coll,
                    doc_id=saved_id,
                    user=_current_user_dict(),
                    after=doc_set,
                    meta={
                        "pg_id": pg_id,
                        "year": year,
                        "month": month,
                        "output_name": output_name,
                    },
                )

            return jsonify({
                "ok": True,
                "message": f"{output_name} saved successfully.",
                "id": str(saved_id),
                "output_name": output_name,
                "output_key": output_key,
                "saved_output_names": _get_saved_names("output"),
            }), 200


        # --------------------------------------------------------
        # MEMBER LEDGER SPECIAL SAVE FLOW
        # Keeps each producer's header + table rows separate.
        # --------------------------------------------------------
        if name == "member_ledger":
            raw_data = payload.get("data", payload) or {}
            data = _normalize_member_ledger_data(raw_data)

            now = datetime.utcnow()

            q = {"pg_id": base_pg_id}

            if year is not None:
                q["year"] = int(year)
            if month is not None:
                q["month"] = int(month)

            before = db[coll].find_one(q)

            doc_set = {
                "pg_id": base_pg_id,
                "year": int(year) if year is not None else None,
                "month": int(month) if month is not None else None,
                "producer_count": len(data.get("producers") or []),
                "producer_names": [
                    p.get("producerName")
                    for p in (data.get("producers") or [])
                    if p.get("producerName")
                ],
                "data": data,
                "updated_at": now,
            }

            if before:
                db[coll].update_one({"_id": before["_id"]}, {"$set": doc_set})
                saved_id = before["_id"]

                try:
                    log_audit(
                        db,
                        action=f"{coll}:update",
                        collection=coll,
                        doc_id=before["_id"],
                        user=_current_user_dict(),
                        before=before,
                        after=doc_set,
                        meta={
                            "pg_id": pg_id,
                            "year": year,
                            "month": month,
                            "register": "member_ledger",
                            "producer_count": doc_set["producer_count"],
                        },
                    )
                except Exception:
                    current_app.logger.exception("member_ledger audit update failed")
            else:
                doc_set["created_at"] = now
                res = db[coll].insert_one(doc_set)
                saved_id = res.inserted_id

                try:
                    log_audit(
                        db,
                        action=f"{coll}:create",
                        collection=coll,
                        doc_id=saved_id,
                        user=_current_user_dict(),
                        after=doc_set,
                        meta={
                            "pg_id": pg_id,
                            "year": year,
                            "month": month,
                            "register": "member_ledger",
                            "producer_count": doc_set["producer_count"],
                        },
                    )
                except Exception:
                    current_app.logger.exception("member_ledger audit create failed")

            return jsonify({
                "ok": True,
                "message": "Member Ledger saved successfully.",
                "id": str(saved_id),
                "producer_count": doc_set["producer_count"],
                "producer_names": doc_set["producer_names"],
                "data": data,
            }), 200
        # --------------------------------------------------------
        # EXISTING GENERIC SAVE FLOW FOR OTHER REGISTERS
        # --------------------------------------------------------
        _upsert_pg_period_doc(
            db,
            collection=coll,
            pg_id=pg_id,
            year=year,
            month=month,
            payload={
                "data": payload.get("data", payload),
            },
            user=_current_user_dict(),
        )

        return jsonify({"ok": True, "message": "Saved successfully"}), 200

    except Exception as e:
        current_app.logger.exception("api_generic_register save failed")
        return jsonify({"ok": False, "error": str(e)}), 500




@pg_bp.route("/api/members/<pg_id>", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_members_api(pg_id):
    db = current_app.mongo_db
    oid = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    if not oid:
        return jsonify({"ok": False, "error": "Invalid PG ID"}), 400

    pg = db.pgs.find_one({"_id": oid}, {"name": 1})
    if not pg:
        return jsonify({"ok": False, "error": "PG not found"}), 404

    members = list(
        db.pg_members.find(
            {
                "pg_id": oid,
                "$or": [{"is_active": True}, {"is_active": {"$exists": False}}],
            },
            {"name": 1, "member_name": 1, "member_code": 1, "code": 1, "spouse_name": 1, "member_id": 1},
        ).sort([("name", 1), ("member_name", 1)]).limit(2000)
    )

    master_ids = []
    for m in members:
        mid = m.get("member_id")
        if isinstance(mid, ObjectId):
            master_ids.append(mid)
        elif mid and ObjectId.is_valid(str(mid)):
            master_ids.append(ObjectId(str(mid)))

    master_by_id = {}
    if master_ids:
        for doc in db.shg_members_master.find(
            {"_id": {"$in": master_ids}},
            {
                "Member Name": 1,
                "Name": 1,
                "Member_Code": 1,
                "Member Code": 1,
                "SHG Member Code": 1,
                "Code": 1,
                "Father/Mother/Spouse Name": 1,
                "Spouse Name": 1,
                "Husband Name": 1,
                "Father Name": 1,
                "Mother Name": 1,
            },
        ):
            master_by_id[str(doc["_id"])] = doc

    def _pick(doc, *keys):
        if not doc:
            return ""
        for key in keys:
            val = doc.get(key)
            if val not in (None, "", []):
                return str(val).strip()
        return ""

    rows = []
    for m in members:
        mid = m.get("member_id")
        mid_str = str(mid) if mid else ""
        master = master_by_id.get(mid_str, {})
        rows.append({
            "_id": str(m.get("_id")),
            "name": ((m.get("name") or m.get("member_name") or _pick(master, "Member Name", "Name") or "Member").strip()),
            "member_code": ((m.get("member_code") or m.get("code") or _pick(master, "Member_Code", "Member Code", "SHG Member Code", "Code")).strip()),
            "spouse_name": ((m.get("spouse_name") or _pick(master, "Father/Mother/Spouse Name", "Spouse Name", "Husband Name", "Father Name", "Mother Name")).strip()),
        })

    return jsonify({
        "ok": True,
        "pg": {"_id": str(pg["_id"]), "name": pg.get("name", "")},
        "members": rows,
    }), 200


@pg_bp.route("/profile/<pg_id>/data", methods=["GET"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
def pg_profile_data(pg_id):
    db = current_app.mongo_db

    def _wants_json():
        if request.headers.get("Authorization"):
            return True
        if request.is_json:
            return True
        accept = request.headers.get("Accept", "")
        if "application/json" in accept.lower():
            return True
        return False

    def _deny(status_code, message):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), status_code
        abort(status_code, message)

    role = getattr(g, "role", None) or session.get("role")
    auth_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
    auth_clf_id = str(getattr(g, "clf_id", None) or session.get("clf_id") or "")
    auth_block_id = str(getattr(g, "block_id", None) or session.get("block_id") or "")
    auth_dist_id = str(getattr(g, "district_id", None) or session.get("district_id") or "")
    auth_state_id = str(getattr(g, "state_id", None) or session.get("state_id") or "")

    oid = safe_objectid(pg_id)
    if not oid:
        return _deny(400, "Invalid PG ID")

    pg = db.pgs.find_one({"_id": oid})
    if not pg:
        return _deny(404, "PG not found")

    if role == "PG_DATA_ENTRY":
        if not auth_pg_id or auth_pg_id != str(pg["_id"]):
            return _deny(403, "Forbidden")

    if auth_clf_id and str(pg.get("clf_id") or "") != auth_clf_id:
        return _deny(403, "Forbidden")

    if auth_block_id and str(pg.get("block_id") or "") != auth_block_id:
        return _deny(403, "Forbidden")

    if auth_dist_id and str(pg.get("district_id") or "") != auth_dist_id:
        return _deny(403, "Forbidden")

    if auth_state_id and str(pg.get("state_id") or "") != auth_state_id:
        return _deny(403, "Forbidden")

    clf_name = None
    pg_block_id = pg.get("block_id")

    if pg_block_id:
        block_admin = db.users.find_one({
            "role": {"$in": ["CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN"]},
            "block_id": pg_block_id
        })
        if block_admin:
            clf_name = (
                block_admin.get("username")
                or block_admin.get("name")
                or block_admin.get("full_name")
                or ""
            )

    response = {
        "ok": True,
        "data": {
            "pg_id": str(pg["_id"]),
            "pg_name": pg.get("name", ""),
            "activity": pg.get("sector", ""),
            "address": pg.get("address", ""),
            "village": pg.get("Village", ""),
            "block": pg.get("Block", ""),
            "district": pg.get("District", ""),
            "state": pg.get("State", ""),
            "formation_date": pg.get("formation_date", ""),
            "total_members": pg.get("total_members", 0),
            "clf_name": clf_name,
            "bank_details": {
                "bank_name": pg.get("bank_details", {}).get("bank_name", ""),
                "branch": pg.get("bank_details", {}).get("branch", ""),
                "account_number": pg.get("bank_details", {}).get("account_number", ""),
                "ifsc": pg.get("bank_details", {}).get("ifsc", ""),
            }
        }
    }

    return jsonify(response), 200

# ============================================================
# NEW: MEETING & GOVERNANCE TRACKING
# - Meeting register, attendance, resolutions
# ============================================================
# commited by atlanta 
@pg_bp.route("/meetings/<pg_id>", methods=["GET", "POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CLF_MANAGER", "CLF_ADMIN", "BLOCK_ADMIN", "DISTRICT_ADMIN", "ADMIN", "SUPER_ADMIN")
@require_unlocked_period(scope='pg')
def meeting_register(pg_id):
    db = current_app.mongo_db

    def _wants_json():
        return bool(
            request.headers.get("Authorization")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "").lower()
        )

    def _deny(message, status=400):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), status
        flash(message, "danger" if status >= 400 else "warning")
        return redirect(url_for("pg.pg_home"))

    # Prefer mobile JWT scope first, then session
    auth_pg_id = str(getattr(g, "pg_id", None) or session.get("pg_id") or "")
    role = getattr(g, "role", None) or session.get("role")

    if not pg_id or str(pg_id).lower() == "none":
        pg_id = auth_pg_id

    pg_obj_id = safe_objectid(pg_id) or safe_objectid(session.get("pg_id"))
    if not pg_obj_id:
        return _deny("Invalid PG ID.", 400)

    pg = db.pgs.find_one({"_id": pg_obj_id})
    if not pg:
        return _deny("PG not found.", 404)

    # Scope restriction for PG user
    if role == "PG_DATA_ENTRY" and auth_pg_id and auth_pg_id != str(pg_obj_id):
        return _deny("You cannot access this PG.", 403)

    # Only active members for attendance list
    member_docs = list(
        db.pg_members.find(
            {
                "pg_id": pg_obj_id,
                "$or": [
                    {"is_active": True},
                    {"is_active": {"$exists": False}}
                ]
            },
            {"name": 1, "member_name": 1}
        ).sort([("name", 1)]).limit(500)
    )


    # =========================================================
    # GET JSON → LIST / LOAD SAVED MEETING MINUTES
    # Used by Meeting Minutes Book frontend for dynamic multiple meetings.
    # Normal browser page rendering remains unchanged.
    # =========================================================
    if request.method == "GET" and _wants_json():
        def _json_safe(value):
            if isinstance(value, ObjectId):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, list):
                return [_json_safe(v) for v in value]
            if isinstance(value, dict):
                return {k: _json_safe(v) for k, v in value.items()}
            return value

        def _date_pretty(date_text):
            try:
                return datetime.strptime(str(date_text), "%Y-%m-%d").strftime("%d %b %Y")
            except Exception:
                return str(date_text or "")

        def _doc_meeting_date(doc):
            if not isinstance(doc, dict):
                return ""

            if doc.get("meeting_date"):
                return str(doc.get("meeting_date"))

            data = doc.get("data") or {}
            minutes = data.get("minutes") or {}

            date_iso = minutes.get("dateISO")
            if date_iso:
                return str(date_iso)[:10]

            date_pretty = minutes.get("datePretty")
            if date_pretty:
                try:
                    return datetime.strptime(str(date_pretty), "%d %b %Y").strftime("%Y-%m-%d")
                except Exception:
                    return ""

            return ""

        def _serialize_meeting_doc(doc):
            data = doc.get("data") or {}
            minutes = data.get("minutes") or {}
            members = data.get("members") or []

            meeting_date_value = _doc_meeting_date(doc)

            return {
                "_id": str(doc.get("_id") or ""),
                "pg_id": str(doc.get("pg_id") or ""),
                "meeting_date": meeting_date_value,
                "meeting_date_pretty": _date_pretty(meeting_date_value),
                "meeting_type": doc.get("meeting_type") or "general",
                "year": doc.get("year") or (int(meeting_date_value[:4]) if meeting_date_value else None),
                "month": doc.get("month") or (int(meeting_date_value[5:7]) if meeting_date_value else None),
                "agenda_count": len(minutes.get("agendaItems") or []),
                "deliberation_count": len(minutes.get("deliberations") or []),
                "decision_count": len(minutes.get("decisions") or []),
                "members_count": len(members) if isinstance(members, list) else 0,
                "data": _json_safe(data),
                "meta": _json_safe(doc.get("meta") or {}),
                "created_at": _json_safe(doc.get("created_at")),
                "updated_at": _json_safe(doc.get("updated_at")),
            }

        pg_id_values = [
    pg_obj_id,
    str(pg_obj_id),
]

        pg_scope = {
        "$or": [
        {"pg_id": {"$in": pg_id_values}},
        {"PG_ID": {"$in": pg_id_values}},
        {"active_pg_id": {"$in": pg_id_values}},
        {"producer_group_id": {"$in": pg_id_values}},
    ]
}

        requested_date = (request.args.get("meeting_date") or request.args.get("date") or "").strip()

        if requested_date:
            requested_pretty = _date_pretty(requested_date)

            # Prefer the canonical PG/date pair. The legacy fallback below
            # checks alternate schemas only when no exact record exists, so
            # an older duplicate cannot win an unordered find_one query.
            doc = db.pg_meeting_minutes.find_one(
                {
                    "pg_id": pg_obj_id,
                    "meeting_date": requested_date,
                },
                sort=[("updated_at", -1), ("created_at", -1)],
            )

            legacy_date_candidates = [
                {
                    "$or": [
                        {"meeting_date": requested_date},
                        {"date": requested_date},
                        {"entry_date": requested_date},
                    ]
                },
                {
                    "$or": [
                        {"meta.meetingDate": requested_date},
                        {"data.meta.meetingDate": requested_date},
                    ]
                },
                {"data.minutes.datePretty": requested_pretty},
                {"data.minutes.dateISO": {"$regex": f"^{re.escape(requested_date)}"}},
            ]

            if not doc:
                for date_candidate in legacy_date_candidates:
                    doc = db.pg_meeting_minutes.find_one(
                        {"$and": [pg_scope, date_candidate]},
                        sort=[("updated_at", -1), ("created_at", -1)],
                    )
                    if doc:
                        break

            if not doc:
                return jsonify({
                    "ok": False,
                    "error": "No saved meeting minutes found for the selected date.",
                    "meeting": None,
                }), 404

            return jsonify({
                "ok": True,
                "meeting": _serialize_meeting_doc(doc),
                "members": [
                    {
                        "_id": str(m.get("_id") or ""),
                        "name": m.get("name") or m.get("member_name") or "",
                    }
                    for m in member_docs
                ],
            }), 200
        
        print("MEETING MINUTES LIST DEBUG:", {
    "pg_id": str(pg_obj_id),
    "pg_scope": pg_scope,
    "total_found": db.pg_meeting_minutes.count_documents(pg_scope),
})

        docs = list(
            db.pg_meeting_minutes
            .find(pg_scope)
            .sort([
                ("meeting_date", -1),
                ("year", -1),
                ("month", -1),
                ("updated_at", -1),
                ("created_at", -1),
            ])
            .limit(300)
        )

        meetings = [_serialize_meeting_doc(doc) for doc in docs]

        meetings = sorted(
            meetings,
            key=lambda x: x.get("meeting_date") or "",
            reverse=True
        )

        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id") or ""),
                "name": pg.get("name") or pg.get("pg_name") or "",
            },
            "meetings": meetings,
            "members": [
                {
                    "_id": str(m.get("_id") or ""),
                    "name": m.get("name") or m.get("member_name") or "",
                }
                for m in member_docs
            ],
            "count": len(meetings),
        }), 200
    # =========================================================
    # POST → SAVE / UPDATE MEETING
    # =========================================================
    if request.method == "POST":
        if _wants_json():
            body = request.get_json(silent=True) or {}

            meeting_date = (body.get("meeting_date") or "").strip()
            meeting_type = (body.get("meeting_type") or "").strip().lower()
            agenda = body.get("agenda", None)
            resolution = body.get("resolution", None)
            deliberations = body.get("deliberations", None)
            att = body.get("attendance_ids", None)

            if att is not None and not isinstance(att, list):
                att = []
        else:
            meeting_date = (request.form.get("meeting_date") or "").strip()
            meeting_type = (request.form.get("meeting_type") or "").strip().lower()
            agenda = request.form.get("agenda", None)
            resolution = request.form.get("resolution", None)

            # optional deliberations support for web form
            deliberations_raw = request.form.get("deliberations")
            deliberations = deliberations_raw if deliberations_raw is not None else None

            form_att = request.form.getlist("attendance")
            att = form_att if form_att else None

        if not meeting_date:
            return _deny("Meeting date is required.", 400)

        try:
            datetime.strptime(meeting_date, "%Y-%m-%d")
        except ValueError:
            return _deny("Invalid meeting date format. Use YYYY-MM-DD.", 400)

        # 🔁 Detect if request is meeting minutes (app/web unified)
        is_minutes_payload = any([
            agenda is not None,
            resolution is not None,
            deliberations is not None
        ])

        # 🔁 Select correct collection
        collection = db.pg_meeting_minutes if is_minutes_payload else db.pg_meetings

        nested_data = body.get("data") if _wants_json() else None
        is_nested_minutes_payload = isinstance(nested_data, dict) and (
            isinstance(nested_data.get("minutes"), dict) or isinstance(nested_data.get("members"), list)
        )

        if is_nested_minutes_payload:
            now = datetime.utcnow()
            minutes_data = nested_data.get("minutes") or {}
            members_data = nested_data.get("members") or []

            existing_minutes_doc = db.pg_meeting_minutes.find_one({
                "pg_id": pg_obj_id,
                "meeting_date": meeting_date
            }) or {}

            existing_data = existing_minutes_doc.get("data") or {}
            existing_minutes = existing_data.get("minutes") or {}
            existing_members = existing_data.get("members") or []

            incoming_minutes_has_data = any([
                minutes_data.get("agendaItems"),
                minutes_data.get("deliberations"),
                minutes_data.get("decisions"),
            ])

            final_minutes = minutes_data if incoming_minutes_has_data else existing_minutes
            final_members = members_data if isinstance(members_data, list) and members_data else existing_members

            web_minutes_doc = {
                "pg_id": pg_obj_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type or "general",
                "year": int(body.get("year") or meeting_date[:4]),
                "month": int(body.get("month") or meeting_date[5:7]),
                "data": {
                    "minutes": {
                        "dateISO": final_minutes.get("dateISO") or f"{meeting_date}T00:00:00.000Z",
                        "datePretty": final_minutes.get("datePretty") or datetime.strptime(meeting_date, "%Y-%m-%d").strftime("%d %b %Y"),
                        "agendaItems": final_minutes.get("agendaItems") or [],
                        "deliberations": final_minutes.get("deliberations") or [],
                        "decisions": final_minutes.get("decisions") or [],
                    },
                    "members": final_members,
                },
                "meta": {
                    **(existing_minutes_doc.get("meta") or {}),
                    **(body.get("meta") or {}),
                    "periodYear": int(body.get("year") or meeting_date[:4]),
                    "periodMonth": int(body.get("month") or meeting_date[5:7]),
                    "savedAt": datetime.utcnow().isoformat() + "Z",
                },
                "updated_at": now,
            }

            date_pretty_key = datetime.strptime(meeting_date, "%Y-%m-%d").strftime("%d %b %Y")

            db.pg_meeting_minutes.update_one(
                {
                    "pg_id": pg_obj_id,
                    "$or": [
                        {"meeting_date": meeting_date},
                        {"data.minutes.datePretty": date_pretty_key},
                    ],
                },
                {
                    "$set": web_minutes_doc,
                    "$setOnInsert": {
                        "created_at": now,
                    }
                },
                upsert=True
            )

            saved_doc = db.pg_meeting_minutes.find_one({
                "pg_id": pg_obj_id,
                "$or": [
                    {"meeting_date": meeting_date},
                    {"data.minutes.datePretty": date_pretty_key},
                ]
            })

            if _wants_json():
                return jsonify({
                    "ok": True,
                    "message": "Meeting minutes saved successfully.",
                    "meeting": {
                        "_id": str(saved_doc.get("_id")) if saved_doc else "",
                        "meeting_date": meeting_date,
                        "year": int(body.get("year") or meeting_date[:4]),
                        "month": int(body.get("month") or meeting_date[5:7]),
                        "data": (saved_doc or web_minutes_doc).get("data", {}),
                        "meta": (saved_doc or web_minutes_doc).get("meta", {}),
                    }
                }), 200

            flash("Meeting saved successfully.", "success")
            return redirect(url_for("pg.meeting_register", pg_id=pg_id))

        existing_doc = collection.find_one({
            "pg_id": pg_obj_id,
            "meeting_date": meeting_date
        }) or {}

        # Preserve existing meeting_type if not sent
        final_meeting_type = meeting_type or existing_doc.get("meeting_type") or "general"

        # Preserve existing agenda/resolution if omitted
        final_agenda = existing_doc.get("agenda", "")
        if agenda is not None:
            final_agenda = str(agenda).strip()

        final_resolution = existing_doc.get("resolution", "")
        if resolution is not None:
            final_resolution = str(resolution).strip()

        # Preserve/add deliberations
        final_deliberations = existing_doc.get("deliberations", [])
        if deliberations is not None:
            if isinstance(deliberations, list):
                cleaned = []
                for item in deliberations:
                    if isinstance(item, dict):
                        cleaned_item = {
                            "id": str(item.get("id") or ""),
                            "content": str(item.get("content") or "").strip()
                        }
                        if cleaned_item["content"]:
                            cleaned.append(cleaned_item)
                    else:
                        text = str(item).strip()
                        if text:
                            cleaned.append({
                                "id": "",
                                "content": text
                            })
                final_deliberations = cleaned
            else:
                text = str(deliberations).strip()
                final_deliberations = [{"id": "", "content": text}] if text else []

        # Preserve existing attendance if omitted
        if att is None:
            final_att_ids = existing_doc.get("attendance", []) or []
        else:
            final_att_ids = []
            for mid in att:
                mid_str = str(mid).strip()
                if not mid_str:
                    continue
                if ObjectId.is_valid(mid_str):
                    final_att_ids.append(ObjectId(mid_str))
                else:
                    final_att_ids.append(mid_str)

        now = datetime.utcnow()

        update_data = {
            "pg_id": pg_obj_id,
            "meeting_date": meeting_date,
            "meeting_type": final_meeting_type,
            "agenda": final_agenda,
            "deliberations": final_deliberations,
            "attendance": final_att_ids,
            "attendance_count": len(final_att_ids),
            "resolution": final_resolution,
            "updated_at": now,
        }

        collection.update_one(
            {
                "pg_id": pg_obj_id,
                "meeting_date": meeting_date
            },
            {
                "$set": update_data,
                "$setOnInsert": {
                    "created_at": now
                }
            },
            upsert=True
        )

        saved_doc = collection.find_one({
            "pg_id": pg_obj_id,
            "meeting_date": meeting_date
        })

        if _wants_json():
            return jsonify({
                "ok": True,
                "message": "Meeting saved successfully.",
                "meeting": {
                    "_id": str(saved_doc["_id"]),
                    "meeting_date": saved_doc.get("meeting_date", ""),
                    "meeting_type": saved_doc.get("meeting_type", "general"),
                    "agenda": saved_doc.get("agenda", ""),
                    "deliberations": saved_doc.get("deliberations", []),
                    "resolution": saved_doc.get("resolution", ""),
                    "attendance_ids": [
                        str(x) for x in (saved_doc.get("attendance") or [])
                    ],
                    "attendance_count": int(saved_doc.get("attendance_count") or 0),
                }
            }), 200

        flash("Meeting saved successfully.", "success")
        return redirect(url_for("pg.meeting_register", pg_id=pg_id))

        # =========================================================
        # GET → FETCH FROM pg_meeting_minutes ONLY
        # Works for both WEB records and APP records
    # =========================================================
    selected_date = (request.args.get("meeting_date") or "").strip()

    def _parse_pretty_to_iso(value):
        value = str(value or "").strip()
        if not value:
            return ""
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except Exception:
                pass
        return ""

    def _date_pretty_from_iso(date_key):
        try:
            return datetime.strptime(date_key, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            return date_key

    query = {"pg_id": pg_obj_id}

    if selected_date:
        selected_pretty = _date_pretty_from_iso(selected_date)
        query["$or"] = [
            {"meeting_date": selected_date},
            {"data.minutes.datePretty": selected_pretty},
            {"data.minutes.dateISO": {"$regex": f"^{selected_date}"}},
        ]

    minutes_docs = list(
        db.pg_meeting_minutes.find(query).sort([
            ("year", -1),
            ("month", -1),
            ("updated_at", -1),
            ("created_at", -1),
        ])
    )

    def _serialize_minutes_doc(doc):
        data = doc.get("data") or {}
        minutes = data.get("minutes") or {}
        members = data.get("members") or []

        pretty = minutes.get("datePretty") or doc.get("datePretty") or ""
        date_key = (
            doc.get("meeting_date")
            or _parse_pretty_to_iso(pretty)
            or str(minutes.get("dateISO") or "")[:10]
        )

        agenda_items = minutes.get("agendaItems") or []
        deliberations = minutes.get("deliberations") or []
        decisions = minutes.get("decisions") or []

        return {
            "_id": str(doc.get("_id")),
            "meeting_date": date_key,
            "datePretty": pretty or _date_pretty_from_iso(date_key),
            "year": int(doc.get("year") or doc.get("meta", {}).get("periodYear") or date_key[:4] or 0),
            "month": int(doc.get("month") or doc.get("meta", {}).get("periodMonth") or date_key[5:7] or 0),

            "data": {
                "minutes": {
                    "dateISO": minutes.get("dateISO") or "",
                    "datePretty": pretty or _date_pretty_from_iso(date_key),
                    "agendaItems": agenda_items,
                    "deliberations": deliberations,
                    "decisions": decisions,
                },
                "members": members,
            },

            # compatibility for app form loading
            "agenda": "\n".join([str(x).strip() for x in agenda_items if str(x).strip()]),
            "deliberations": [
                {"id": str(i + 1), "content": str(x).strip()}
                for i, x in enumerate(deliberations)
                if str(x).strip()
            ],
            "resolution": "\n".join([str(x).strip() for x in decisions if str(x).strip()]),
            "members_present": members,
            "attendance_count": len(members),
            "source": "pg_meeting_minutes",
        }

    meetings = [_serialize_minutes_doc(doc) for doc in minutes_docs]

    if _wants_json():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg["_id"]),
                "name": pg.get("name", "")
            },
            "members": [
                {
                    "_id": str(m["_id"]),
                    "name": m.get("name") or m.get("member_name") or "Member"
                }
                for m in member_docs
            ],
            "meetings": meetings
        }), 200

# === CORE ENGINE INTEGRATION ACTIVE ===
# AuditLogger.log(action, user_id, entity, entity_id) available

# ============================================================
# CLF/BLOCK: Active PG context helpers
# - Lets CLF/BLOCK work on one PG at a time using the sidebar links
# ============================================================

@pg_bp.route('/set_active/<pg_id>')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def set_active_pg(pg_id):
    """Set the active PG context in session (for CLF/BLOCK sidebar actions)."""
    db = current_app.mongo_db
    pg_doc = db.pgs.find_one({"_id": (safe_objectid(pg_id) or safe_objectid(session.get('pg_id')))})
    if not pg_doc:
        flash('PG not found.', 'danger')
        return redirect(url_for('reports.hierarchy_dashboard'))

    role = session.get('role')

    # PG user can only set own PG
    if role == 'PG_DATA_ENTRY' and session.get('pg_id') != pg_id:
        flash('You cannot access this PG.', 'danger')
        return redirect(url_for('pg.pg_home'))

    if role == 'CADRE_CC' and str(pg_id) not in _assigned_pg_ids_for_session():
        flash('This PG is not assigned to your Cadre account.', 'danger')
        return redirect(url_for('reports.hierarchy_dashboard'))

    # Scope checks for other roles (same as pg_dashboard)
    if role not in ('PG_DATA_ENTRY', 'CADRE_CC'):
        if session.get('clf_id') and str(pg_doc.get('clf_id')) != session.get('clf_id'):
            flash('This PG is not under your CLF.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('block_id') and str(pg_doc.get('block_id')) != session.get('block_id'):
            flash('This PG is not under your Block.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('district_id') and str(pg_doc.get('district_id')) != session.get('district_id'):
            flash('This PG is not under your District.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))
        if session.get('state_id') and str(pg_doc.get('state_id')) != session.get('state_id'):
            flash('This PG is not under your State.', 'danger')
            return redirect(url_for('reports.hierarchy_dashboard'))

    session['active_pg_id'] = pg_id
    session['active_pg_name'] = pg_doc.get('name') or pg_doc.get('pg_name') or pg_doc.get('PG Name') or pg_id
    if role == 'CADRE_CC':
        session['pg_id'] = pg_id

    nxt = request.args.get('next')
    if nxt:
        return redirect(nxt)
    return redirect(url_for('pg.pg_view', pg_id=pg_id))


@pg_bp.route('/clear_active')
@login_required
@roles_required('PG_DATA_ENTRY','CADRE_CC','CLF_MANAGER','CLF_ADMIN','CLF_ADMIN','BLOCK_ADMIN','DISTRICT_ADMIN','ADMIN','SUPER_ADMIN')
def clear_active_pg():
    session.pop('active_pg_id', None)
    session.pop('active_pg_name', None)
    if session.get('role') == 'CADRE_CC':
        session.pop('pg_id', None)
    flash('Active PG cleared.', 'info')
    return redirect(url_for('reports.hierarchy_dashboard'))

# ============================================================
# PG Registration + Membership Validation Routes
# ============================================================



def _pg_registration_review_snapshot(pg):
    """
    Small safe snapshot of PG Registration fields used for Block Admin change highlighting.
    Do not store full PG document here.
    """
    pg = pg or {}
    office_bearers = pg.get("office_bearers") or {}
    bank_details = pg.get("bank_details") or {}
    aggregation_centre = pg.get("aggregation_centre") or {}

    def clean_value(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None:
            return ""
        return str(value).strip()

    members = []
    for m in (pg.get("members") or []):
        members.append({
            "member_id": clean_value(m.get("member_id")),
            "member_name": clean_value(m.get("member_name")),
            "shg_name": clean_value(m.get("shg_name")),
            "shg_code": clean_value(m.get("shg_code")),
            "role": clean_value(m.get("role") or "member").lower(),
        })

    members = sorted(
        members,
        key=lambda x: (
            x.get("member_name") or "",
            x.get("member_id") or "",
            x.get("role") or "",
        )
    )

    return {
        "pg_type": clean_value(pg.get("pg_type")),
        "sector": clean_value(pg.get("sector")),
        "formation_date": clean_value(pg.get("formation_date")),
        "contact_number": clean_value(office_bearers.get("contact_number")),
        "president_id": clean_value(office_bearers.get("president_id")),
        "secretary_id": clean_value(office_bearers.get("secretary_id")),
        "cashier_id": clean_value(office_bearers.get("cashier_id")),
        "bank_name": clean_value(bank_details.get("bank_name")),
        "branch": clean_value(bank_details.get("branch")),
        "account_number": clean_value(bank_details.get("account_number")),
        "ifsc": clean_value(bank_details.get("ifsc")),
        "agg_centre_name": clean_value(aggregation_centre.get("name")),
        "agg_centre_address": clean_value(aggregation_centre.get("address")),
        "selected_shg_keys": sorted([clean_value(x) for x in (pg.get("selected_shg_keys") or []) if clean_value(x)]),
        "total_members": clean_value(pg.get("total_members") or len(members)),
        "members": members,
    }


def _pg_registration_changed_fields(old_snapshot, new_snapshot):
    """
    Compare rejected snapshot with resubmitted snapshot.
    Returns display-ready changed field list.
    """
    old_snapshot = old_snapshot or {}
    new_snapshot = new_snapshot or {}

    field_labels = {
        "pg_type": "PG Type",
        "sector": "Sector",
        "formation_date": "Formation Date",
        "contact_number": "Contact Number",
        "president_id": "President",
        "secretary_id": "Secretary",
        "cashier_id": "Cashier",
        "bank_name": "Bank Name",
        "branch": "Branch",
        "account_number": "Account Number",
        "ifsc": "IFSC",
        "agg_centre_name": "Aggregation Centre",
        "agg_centre_address": "Aggregation Address",
        "selected_shg_keys": "Selected SHGs",
        "total_members": "Total Members",
        "members": "Selected Members",
    }

    changed = []

    def norm(value):
        if value is None:
            return ""
        if isinstance(value, list):
            return value
        return str(value).strip()

    def display_value(value):
        if value is None or value == "":
            return "-"
        if isinstance(value, list):
            if not value:
                return "-"
            if value and isinstance(value[0], dict):
                names = []
                for item in value:
                    name = item.get("member_name") or item.get("member_id") or ""
                    role = item.get("role") or "member"
                    if name:
                        names.append(f"{name} ({role})")
                return ", ".join(names) if names else "-"
            return ", ".join([str(x) for x in value])
        return str(value)

    for key, label in field_labels.items():
        old_value = norm(old_snapshot.get(key))
        new_value = norm(new_snapshot.get(key))

        if old_value != new_value:
            changed.append({
                "key": key,
                "label": label,
                "old": display_value(old_snapshot.get(key)),
                "new": display_value(new_snapshot.get(key)),
            })

    return changed




@pg_bp.route("/registration/<pg_id>/submit-validation", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC")
@require_unlocked_period(scope='pg')
def submit_pg_registration_validation(pg_id):
    db = current_app.mongo_db
    pg = _load_pg_for_validation(db, pg_id)

    if not pg:
        return _validation_json_or_redirect(
            False,
            "PG not found.",
            status=404,
            redirect_to=url_for("pg.pg_home")
        )

    # ----------------------------------------------------------
    # Safety check before submitting to Block Admin
    # Prevents blank/incomplete PG Registration from getting locked
    # ----------------------------------------------------------
    members = pg.get("members") or []

    if not members:
        return _validation_json_or_redirect(
            False,
            "Please save PG Registration with selected PG members before submitting for Block validation.",
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    bank_details = pg.get("bank_details") or {}
    office_bearers = pg.get("office_bearers") or {}
    aggregation_centre = pg.get("aggregation_centre") or {}

    required_checks = {
        "PG Type": pg.get("pg_type"),
        "Sector": pg.get("sector"),
        "Formation Date": pg.get("formation_date"),
        "Contact Number": office_bearers.get("contact_number"),
        "Bank Name": bank_details.get("bank_name"),
        "Branch": bank_details.get("branch"),
        "Account Number": bank_details.get("account_number"),
        "IFSC": bank_details.get("ifsc"),
        "Aggregation Centre Name": aggregation_centre.get("name"),
        "Aggregation Centre Address": aggregation_centre.get("address"),
    }

    missing = [
        field_name
        for field_name, value in required_checks.items()
        if not value or str(value).strip() == ""
    ]

    if missing:
        return _validation_json_or_redirect(
            False,
            "Please save all required PG Registration fields before submitting: " + ", ".join(missing),
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    # ----------------------------------------------------------
    # Office bearer validation
    # ----------------------------------------------------------
    president_id = office_bearers.get("president_id")
    secretary_id = office_bearers.get("secretary_id")
    cashier_id = office_bearers.get("cashier_id")

    if not president_id or not secretary_id or not cashier_id:
        return _validation_json_or_redirect(
            False,
            "Please save President, Secretary and Cashier before submitting for Block validation.",
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    saved_member_ids = set()
    for m in members:
        mid = m.get("member_id")
        if mid:
            saved_member_ids.add(str(mid))

    if (
        str(president_id) not in saved_member_ids
        or str(secretary_id) not in saved_member_ids
        or str(cashier_id) not in saved_member_ids
    ):
        return _validation_json_or_redirect(
            False,
            "President, Secretary and Cashier must be selected from saved PG members.",
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    if len({str(president_id), str(secretary_id), str(cashier_id)}) != 3:
        return _validation_json_or_redirect(
            False,
            "President, Secretary and Cashier must be different members.",
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    total_members = pg.get("total_members") or 0

    try:
        total_members = int(total_members)
    except Exception:
        total_members = 0

    if total_members <= 0 or total_members != len(members):
        return _validation_json_or_redirect(
            False,
            "Please save PG members properly before submitting for Block validation.",
            status=400,
            redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
            category="danger"
        )

    # ----------------------------------------------------------
    # Snapshot + changed fields tracking
    # ----------------------------------------------------------
    previous_validation_doc = _validation_doc(pg, "pg_registration")

    previous_snapshot = (
        previous_validation_doc.get("rejected_snapshot")
        or previous_validation_doc.get("submitted_snapshot")
        or {}
    )

    current_snapshot = _pg_registration_review_snapshot(pg)

    changed_fields = []
    if previous_snapshot:
        changed_fields = _pg_registration_changed_fields(previous_snapshot, current_snapshot)

    # ----------------------------------------------------------
    # Existing validation submit workflow
    # ----------------------------------------------------------
    ok, message, status = _submit_validation(db, pg, "pg_registration")

    if ok:
        db.pgs.update_one(
            {"_id": pg["_id"]},
            {
                "$set": {
                    "registration_validation.previous_snapshot": previous_snapshot,
                    "registration_validation.submitted_snapshot": current_snapshot,
                    "registration_validation.changed_fields": changed_fields,
                    "registration_validation.changed_field_keys": [
                        x.get("key")
                        for x in changed_fields
                        if x.get("key")
                    ],
                    "registration_validation.snapshot_updated_at": _validation_now(),
                }
            }
        )

    return _validation_json_or_redirect(
        ok,
        message,
        status=status,
        redirect_to=url_for("pg.pg_registration", pg_id=str(pg["_id"])),
        extra={
            "pg_id": str(pg["_id"]),
            "form_type": "pg_registration",
            "validation_status": _validation_status(
                current_app.mongo_db.pgs.find_one({"_id": pg["_id"]}) or pg,
                "pg_registration"
            ),
        },
        category="success" if ok else "danger"
    )


@pg_bp.route("/members/<pg_id>/submit-validation", methods=["POST"])
@login_required
@roles_required("PG_DATA_ENTRY", "CADRE_CC")
@require_unlocked_period(scope='pg')
def submit_member_registration_validation(pg_id):
    db = current_app.mongo_db
    pg = _load_pg_for_validation(db, pg_id)

    if not pg:
        return _validation_json_or_redirect(
            False,
            "PG not found.",
            status=404,
            redirect_to=url_for("pg.pg_home")
        )

    pg_obj_id = pg["_id"]

    # ----------------------------------------------------------
    # IMPORTANT:
    # Do not block validation submit using member_id matching here.
    # This route's job is to submit the Membership Registration
    # validation status to Block Admin.
    #
    # Earlier issue happened because this route tried to match:
    # pg.members.member_id -> pg_members.member_id
    # and returned before _submit_validation().
    # ----------------------------------------------------------

    selected_members = pg.get("members") or []

    if not selected_members:
        saved_member_count = db.pg_members.count_documents({
            "$or": [
                {"pg_id": pg_obj_id},
                {"pg_id": str(pg_obj_id)},
            ],
            "$or": [
                {"is_active": True},
                {"is_active": {"$exists": False}},
            ]
        })

        if saved_member_count <= 0:
            return _validation_json_or_redirect(
                False,
                "No PG members found. Please complete PG Registration member selection first.",
                status=400,
                redirect_to=url_for("pg.pg_members", pg_id=str(pg_obj_id)),
                category="danger"
            )

    ok, message, status = _submit_validation(db, pg, "member_registration")

    print("MEMBER VALIDATION SUBMIT RESULT:", {
        "ok": ok,
        "message": message,
        "status": status,
        "pg_id": str(pg["_id"]),
        "validation_status_after_submit": _validation_status(
            db.pgs.find_one({"_id": pg["_id"]}) or pg,
            "member_registration"
        ),
    })

    return _validation_json_or_redirect(
        ok,
        message,
        status=status,
        redirect_to=url_for("pg.pg_members", pg_id=str(pg["_id"])),
        extra={
            "pg_id": str(pg["_id"]),
            "form_type": "member_registration",
            "validation_status": _validation_status(
                db.pgs.find_one({"_id": pg["_id"]}) or pg,
                "member_registration"
            ),
        },
        category="success" if ok else "danger"
    )



@pg_bp.route("/validation/queue", methods=["GET"])
@login_required
@roles_required("BLOCK_ADMIN")
def validation_queue():
    """
    Block Admin validation queue.

    Shows PG Registration and Membership Registration forms submitted/resubmitted
    by PG logins under the logged-in Block Admin's block.
    """
    db = current_app.mongo_db
    block_id = getattr(g, "block_id", None) or session.get("block_id")

    if not block_id or not ObjectId.is_valid(str(block_id)):
        flash("Your account is not mapped to a valid block.", "danger")
        return redirect(url_for("reports.hierarchy_dashboard"))

    block_oid = ObjectId(str(block_id))

    block_doc = db.blocks.find_one({"_id": block_oid}, {"name": 1}) or {}
    block_name = block_doc.get("name") or ""

    block_scope_or = [
        {"block_id": block_oid},
        {"block_id": str(block_oid)},
    ]

    if block_name:
        block_scope_or.extend([
            {"Block": block_name},
            {"block": block_name},
            {"block_name": block_name},
        ])

    query = {
        "$and": [
            {
                "$or": block_scope_or
            },
            {
                "$or": [
                    {"registration_validation.status": {"$in": list(VALIDATION_PENDING_STATUSES)}},
                    {"member_registration_validation.status": {"$in": list(VALIDATION_PENDING_STATUSES)}},
                ]
            }
        ]
    }
        
    

    pgs = list(db.pgs.find(query).sort([
        ("registration_validation.submitted_at", -1),
        ("member_registration_validation.submitted_at", -1),
        ("updated_at", -1),
    ]))

    rows = []
    for pg in pgs:
        reg_doc = _validation_doc(pg, "pg_registration")
        mem_doc = _validation_doc(pg, "member_registration")

        if str(reg_doc.get("status") or "").lower() in VALIDATION_PENDING_STATUSES:
            rows.append({
                "pg": pg,
                "form_type": "pg_registration",
                "form_label": _validation_label("pg_registration"),
                "status": reg_doc.get("status") or "submitted",
                "submitted_at": reg_doc.get("submitted_at"),
                "remarks": reg_doc.get("remarks") or "",
            })

        if str(mem_doc.get("status") or "").lower() in VALIDATION_PENDING_STATUSES:
            rows.append({
                "pg": pg,
                "form_type": "member_registration",
                "form_label": _validation_label("member_registration"),
                "status": mem_doc.get("status") or "submitted",
                "submitted_at": mem_doc.get("submitted_at"),
                "remarks": mem_doc.get("remarks") or "",
            })

    if _wants_json_response():
        return jsonify({
            "ok": True,
            "rows": [
                {
                    "pg_id": str(row["pg"].get("_id")),
                    "pg_name": row["pg"].get("name") or row["pg"].get("pg_name") or "",
                    "form_type": row["form_type"],
                    "form_label": row["form_label"],
                    "status": row["status"],
                    "submitted_at": row["submitted_at"].isoformat() if isinstance(row["submitted_at"], datetime) else row["submitted_at"],
                    "review_url": url_for("pg.validation_review", form_type=row["form_type"], pg_id=str(row["pg"].get("_id"))),
                }
                for row in rows
            ],
            "count": len(rows),
        }), 200

    return render_template(
        "block/validation_queue.html",
        rows=rows,
        pending_count=len(rows),
    )


@pg_bp.route("/validation/<form_type>/<pg_id>/review", methods=["GET"])
@login_required
@roles_required("BLOCK_ADMIN")
def validation_review(form_type, pg_id):
    """
    Block Admin review page for PG Registration or Membership Registration.
    """
    db = current_app.mongo_db

    if form_type not in VALIDATION_FORM_TYPES:
        flash("Invalid validation form type.", "danger")
        return redirect(url_for("pg.validation_queue"))

    pg = _load_pg_for_validation(db, pg_id)

    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("pg.validation_queue"))

    if not _block_admin_can_validate_pg(pg):
        flash("This PG is not under your Block.", "danger")
        return redirect(url_for("pg.validation_queue"))

    validation = _validation_doc(pg, form_type)
    status = str(validation.get("status") or "draft").lower()

    changed_fields = []
    changed_field_keys = []

    if form_type == "pg_registration":
        changed_fields = validation.get("changed_fields") or []
        changed_field_keys = validation.get("changed_field_keys") or []

        if not isinstance(changed_fields, list):
            changed_fields = []

        if not isinstance(changed_field_keys, list):
            changed_field_keys = []

    members = []

    if form_type == "pg_registration":
        saved_pg_members = pg.get("members") or []
        pg_member_scope = {
            "$and": [
                {"$or": [{"pg_id": pg["_id"]}, {"pg_id": str(pg["_id"])}]},
                {"$or": [{"is_active": True}, {"is_active": {"$exists": False}}]},
            ]
        }
        registered_member_docs = list(db.pg_members.find(pg_member_scope))
        registered_by_id = {
            str(doc.get("member_id")): doc
            for doc in registered_member_docs
            if doc.get("member_id") is not None
        }

        # Keep embedded registration members and their office-bearer roles first,
        # then add active Membership Register members not present in that snapshot.
        review_member_sources = []
        seen_member_ids = set()
        for saved_member in saved_pg_members:
            saved_member = saved_member if isinstance(saved_member, dict) else {}
            member_id = saved_member.get("member_id")
            member_key = str(member_id) if member_id is not None else ""
            registered_doc = registered_by_id.get(member_key, {})
            review_member_sources.append((saved_member, registered_doc))
            if member_key:
                seen_member_ids.add(member_key)

        for registered_doc in registered_member_docs:
            member_id = registered_doc.get("member_id")
            member_key = str(member_id) if member_id is not None else ""
            if member_key and member_key not in seen_member_ids:
                review_member_sources.append(({
                    "member_id": member_id,
                    "member_name": registered_doc.get("name") or registered_doc.get("member_name") or "",
                    "shg_name": registered_doc.get("shg_name") or "",
                    "shg_code": registered_doc.get("shg_code") or "",
                    "role": registered_doc.get("role") or "member",
                }, registered_doc))
                seen_member_ids.add(member_key)

        master_member_ids = []
        for saved_member, registered_doc in review_member_sources:
            member_id = saved_member.get("member_id") or registered_doc.get("member_id")
            if member_id is not None and ObjectId.is_valid(str(member_id)):
                master_member_ids.append(ObjectId(str(member_id)))
        master_members_map = {}
        if master_member_ids:
            master_members = list(db.shg_members_master.find({"_id": {"$in": list(set(master_member_ids))}}))
            master_members_map = {
                str(master.get("_id")): master
                for master in master_members
                if master.get("_id")
            }

        role_label_map = {
            "president": "President",
            "secretary": "Secretary",
            "cashier": "Cashier",
            "member": "Member",
        }

        for saved_member, registered_doc in review_member_sources:
            member_id = saved_member.get("member_id") or registered_doc.get("member_id")
            member_key = str(member_id) if member_id is not None else ""
            master = master_members_map.get(member_key, {})
            role_value = str(
                saved_member.get("role") or registered_doc.get("role") or "member"
            ).strip().lower()
            designation_value = role_label_map.get(
                role_value,
                role_value.title() if role_value else "Member"
            )

            members.append({
                "_id": member_id,
                "member_id": member_id,
                "member_code": (saved_member.get("member_code") or registered_doc.get("member_code")
                                or master.get("Member Code") or master.get("member_code") or ""),
                "name": (
                    saved_member.get("member_name") or registered_doc.get("name")
                    or registered_doc.get("member_name") or master.get("Member Name")
                    or master.get("member_name") or master.get("name") or ""
                ),
                "member_name": (
                    saved_member.get("member_name") or registered_doc.get("name")
                    or registered_doc.get("member_name") or master.get("Member Name")
                    or master.get("member_name") or master.get("name") or ""
                ),
                "role": role_value,
                "designation": designation_value,
                "designation_in_pg": designation_value,
                "shg_name": (
                    saved_member.get("shg_name") or registered_doc.get("shg_name")
                    or master.get("SHG Name") or master.get("SHG_Name")
                    or master.get("shg_name") or ""
                ),
                "shg_code": (
                    saved_member.get("shg_code") or registered_doc.get("shg_code")
                    or master.get("SHG Code") or master.get("SHG_Code")
                    or master.get("shg_code") or ""
                ),
                "spouse_parent": (
                    registered_doc.get("spouse_name") or registered_doc.get("spouse_parent")
                    or master.get("Spouse/Parent Name") or master.get("Spouse Name")
                    or master.get("Father/Husband Name") or master.get("spouse_parent")
                    or master.get("spouse_name") or ""
                ),
                "category": (
                    registered_doc.get("category") or master.get("Category")
                    or master.get("Social Category") or master.get("category") or ""
                ),
                "contact": (
                    registered_doc.get("contact") or registered_doc.get("phone")
                    or master.get("Contact") or master.get("Contact Number")
                    or master.get("Mobile Number") or master.get("Phone")
                    or master.get("contact") or master.get("phone") or ""
                ),
                "bank": registered_doc.get("bank_name") or master.get("Bank Name") or master.get("bank_name") or "",
                "bank_name": registered_doc.get("bank_name") or master.get("Bank Name") or master.get("bank_name") or "",
                "branch": registered_doc.get("branch") or master.get("Branch") or master.get("branch") or "",
                "account": registered_doc.get("account_number") or master.get("Account Number") or master.get("account_number") or "",
                "account_number": registered_doc.get("account_number") or master.get("Account Number") or master.get("account_number") or "",
                "fee": registered_doc.get("membership_fee_paid") or master.get("Membership Fee") or master.get("membership_fee") or "",
                "membership_fee_paid": registered_doc.get("membership_fee_paid") or master.get("Membership Fee") or master.get("membership_fee") or "",
            })

    elif form_type == "member_registration":
        selected_pg_members = pg.get("members") or []

        role_map = {}
        order_map = {}

        for index, item in enumerate(selected_pg_members):
            mid = item.get("member_id")
            if not mid:
                continue

            mid_str = str(mid)
            role_map[mid_str] = str(item.get("role") or "member").strip().lower()
            order_map[mid_str] = index

        role_label_map = {
            "president": "President",
            "secretary": "Secretary",
            "cashier": "Cashier",
            "member": "Member",
        }

        member_docs = list(db.pg_members.find({
            "$and": [
                {
                    "$or": [
                        {"pg_id": pg["_id"]},
                        {"pg_id": str(pg["_id"])},
                    ]
                },
                {
                    "$or": [
                        {"is_active": True},
                        {"is_active": {"$exists": False}},
                    ]
                }
            ]
        }))

        def _member_sort_key(doc):
            mid = str(doc.get("member_id") or "")
            return order_map.get(mid, 9999)

        member_docs.sort(key=_member_sort_key)

        sector_name = str(pg.get("sector") or "").strip().lower()

        for doc in member_docs:
            mid = doc.get("member_id")
            mid_str = str(mid) if mid else ""

            role_value = str(
                doc.get("role")
                or role_map.get(mid_str)
                or "member"
            ).strip().lower()

            designation_value = role_label_map.get(
                role_value,
                role_value.title() if role_value else "Member"
            )

            crop_or_activity = ""

            if sector_name == "agri":
                crop_or_activity = doc.get("agri_crop") or ""
            elif sector_name == "ardd":
                activity = doc.get("ardd_activity") or ""
                unit = doc.get("ardd_unit") or ""
                crop_or_activity = f"{activity} - {unit}" if activity and unit else (activity or unit)
            elif sector_name == "fishery":
                crop_or_activity = doc.get("fishery_activity") or ""

            fee_value = doc.get("membership_fee_paid")
            if fee_value in (None, ""):
                fee_value = ""

            members.append({
                "_id": doc.get("_id"),
                "member_id": doc.get("member_id"),

                "name": doc.get("name") or doc.get("member_name") or "",
                "member_name": doc.get("name") or doc.get("member_name") or "",

                "role": role_value,
                "designation": designation_value,
                "designation_in_pg": designation_value,

                "spouse_parent": doc.get("spouse_name") or doc.get("spouse_parent") or "",
                "spouse_name": doc.get("spouse_name") or "",

                "category": doc.get("category") or "",
                "shg_name": doc.get("shg_name") or "",
                "shg_code": doc.get("shg_code") or "",

                "contact": doc.get("contact") or "",
                "bank": doc.get("bank_name") or "",
                "bank_name": doc.get("bank_name") or "",
                "branch": doc.get("branch") or "",
                "account": doc.get("account_number") or "",
                "account_number": doc.get("account_number") or "",
                "fee": fee_value,
                "membership_fee_paid": fee_value,

                "agri_crop": doc.get("agri_crop") or "",
                "agri_ffs_module": doc.get("agri_ffs_module") or "",

                "ardd_activity": doc.get("ardd_activity") or "",
                "ardd_unit": doc.get("ardd_unit") or "",

                "fishery_activity": doc.get("fishery_activity") or "",

                "crop_or_activity": crop_or_activity,
                "ffs_module": doc.get("agri_ffs_module") or "",
            })

    if _wants_json_response():
        return jsonify({
            "ok": True,
            "pg": {
                "_id": str(pg.get("_id")),
                "name": pg.get("name") or pg.get("pg_name") or "",
                "sector": pg.get("sector") or "",
                "status": pg.get("status") or "draft",
            },
            "form_type": form_type,
            "form_label": _validation_label(form_type),
            "validation": {
                **validation,
                "submitted_at": validation.get("submitted_at").isoformat() if isinstance(validation.get("submitted_at"), datetime) else validation.get("submitted_at"),
                "reviewed_at": validation.get("reviewed_at").isoformat() if isinstance(validation.get("reviewed_at"), datetime) else validation.get("reviewed_at"),
            },
            "status": status,
            "members_count": len(members),
            "changed_fields": changed_fields,
            "changed_field_keys": changed_field_keys,
            "changed_count": len(changed_fields),
        }), 200

    return render_template(
        "block/validation_review.html",
        pg=pg,
        form_type=form_type,
        form_label=_validation_label(form_type),
        validation=validation,
        validation_status=status,
        members=members,
        changed_fields=changed_fields,
        changed_field_keys=changed_field_keys,
        form_url=_validation_form_url(form_type, pg["_id"]),
    )






@pg_bp.route("/validation/<form_type>/<pg_id>/approve", methods=["POST"])
@login_required
@roles_required("BLOCK_ADMIN")
def validation_approve(form_type, pg_id):
    db = current_app.mongo_db

    if form_type not in VALIDATION_FORM_TYPES:
        return _validation_json_or_redirect(
            False,
            "Invalid validation form type.",
            status=400,
            redirect_to=url_for("pg.validation_queue")
        )

    pg = _load_pg_for_validation(db, pg_id)
    if not pg:
        return _validation_json_or_redirect(
            False,
            "PG not found.",
            status=404,
            redirect_to=url_for("pg.validation_queue")
        )

    remarks = request.form.get("remarks", "") if not request.is_json else (request.get_json(silent=True) or {}).get("remarks", "")
    ok, message, status = _review_validation(db, pg, form_type, "approve", remarks)

    return _validation_json_or_redirect(
        ok,
        message,
        status=status,
        redirect_to=url_for("pg.validation_queue"),
        extra={
            "pg_id": str(pg["_id"]),
            "form_type": form_type,
            "validation_status": _validation_status(
                current_app.mongo_db.pgs.find_one({"_id": pg["_id"]}) or pg,
                form_type
            ),
        },
        category="success" if ok else "danger"
    )


@pg_bp.route("/validation/<form_type>/<pg_id>/reject", methods=["POST"])
@login_required
@roles_required("BLOCK_ADMIN")
def validation_reject(form_type, pg_id):
    db = current_app.mongo_db

    if form_type not in VALIDATION_FORM_TYPES:
        return _validation_json_or_redirect(
            False,
            "Invalid validation form type.",
            status=400,
            redirect_to=url_for("pg.validation_queue")
        )

    pg = _load_pg_for_validation(db, pg_id)
    if not pg:
        return _validation_json_or_redirect(
            False,
            "PG not found.",
            status=404,
            redirect_to=url_for("pg.validation_queue")
        )

    if request.is_json:
        body = request.get_json(silent=True) or {}
        remarks = body.get("remarks", "")
    else:
        remarks = request.form.get("remarks", "")

    ok, message, status = _review_validation(db, pg, form_type, "reject", remarks)

    redirect_to = (
        url_for("pg.validation_review", form_type=form_type, pg_id=str(pg["_id"]))
        if not ok else url_for("pg.validation_queue")
    )

    return _validation_json_or_redirect(
        ok,
        message,
        status=status,
        redirect_to=redirect_to,
        extra={
            "pg_id": str(pg["_id"]),
            "form_type": form_type,
            "validation_status": _validation_status(
                current_app.mongo_db.pgs.find_one({"_id": pg["_id"]}) or pg,
                form_type
            ),
        },
        category="success" if ok else "danger"
    )
