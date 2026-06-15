from datetime import datetime
from bson import ObjectId
from flask import (
    current_app,
    render_template,
    session,
    redirect,
    url_for,
    flash,
    jsonify,
    request,
)

from . import clf_bp


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _to_object_id(value):
    """
    Safely convert a value to ObjectId.
    Returns None if invalid.
    """
    try:
        if value and ObjectId.is_valid(str(value)):
            return ObjectId(str(value))
    except Exception:
        pass
    return None


def _current_user():
    """
    Build current logged-in user context from session.

    This keeps the CLF module independent and safe even before
    auth/rbac files are fully updated for CLF_ADMIN.
    """
    user_id = (
        session.get("user_id")
        or session.get("_user_id")
        or session.get("uid")
    )

    user = None
    user_oid = _to_object_id(user_id)

    if user_oid:
        user = current_app.mongo_db.users.find_one({"_id": user_oid}) or None

    if not user and user_id:
        user = current_app.mongo_db.users.find_one({"_id": str(user_id)}) or None

    if not user:
        user = {}

    role = (
        user.get("role")
        or session.get("role")
        or session.get("user_role")
        or ""
    )

    return {
        "id": str(user.get("_id") or user_id or ""),
        "doc": user,
        "role": role,
        "state_id": user.get("state_id") or session.get("state_id"),
        "district_id": user.get("district_id") or session.get("district_id"),
        "block_id": user.get("block_id") or session.get("block_id"),
        "clf_id": user.get("clf_id") or session.get("clf_id"),
        "assigned_pg_ids": user.get("assigned_pg_ids") or session.get("assigned_pg_ids") or [],
        "name": user.get("name") or session.get("name") or session.get("username") or "",
        "username": user.get("username") or session.get("username") or "",
    }


def _require_clf_admin():
    """
    Restrict CLF routes to CLF_ADMIN only.
    """
    user = _current_user()

    if user["role"] != "CLF_ADMIN":
        flash("You are not authorized to access the CLF module.", "danger")
        return None, redirect(url_for("auth.login"))

    if not user.get("clf_id"):
        flash("CLF is not mapped with this login. Please contact Block Admin.", "danger")
        return None, redirect(url_for("auth.login"))

    return user, None


def _id_match_filter(field_name, value):
    """
    Build Mongo filter that works with both ObjectId and string ids.
    This is important because older records may store ids differently.
    """
    oid = _to_object_id(value)

    if oid:
        return {
            "$or": [
                {field_name: oid},
                {field_name: str(value)},
            ]
        }

    return {field_name: str(value)}


def _get_clf_doc(clf_id):
    """
    Fetch CLF document using ObjectId/string fallback.
    """
    clf_oid = _to_object_id(clf_id)

    if clf_oid:
        clf = current_app.mongo_db.clfs.find_one({"_id": clf_oid})
        if clf:
            return clf

    return current_app.mongo_db.clfs.find_one({"_id": str(clf_id)}) or {}


def _get_assigned_pgs(user):
    """
    Source of truth:
    1. Prefer PGs mapped by clf_id.
    2. Also include PGs listed in user's assigned_pg_ids as fallback.
    3. Deduplicate by _id.

    CLF cannot see PGs outside mapped/assigned scope.
    """
    db = current_app.mongo_db
    clf_id = user.get("clf_id")
    assigned_pg_ids = user.get("assigned_pg_ids") or []

    filters = []

    if clf_id:
        filters.append(_id_match_filter("clf_id", clf_id))

    pg_or_filters = []

    for pg_id in assigned_pg_ids:
        pg_oid = _to_object_id(pg_id)
        if pg_oid:
            pg_or_filters.append({"_id": pg_oid})
        pg_or_filters.append({"_id": str(pg_id)})

    if pg_or_filters:
        filters.append({"$or": pg_or_filters})

    if not filters:
        return []

    if len(filters) == 1:
        query = filters[0]
    else:
        query = {"$or": filters}

    pgs = list(db.pgs.find(query).sort("name", 1))

    # Deduplicate safely
    seen = set()
    unique_pgs = []

    for pg in pgs:
        pg_key = str(pg.get("_id"))
        if pg_key not in seen:
            seen.add(pg_key)
            unique_pgs.append(pg)

    return unique_pgs


def _pg_ids_for_query(pgs):
    """
    Return PG ids in both ObjectId and string forms for old/new records.
    """
    values = []

    for pg in pgs:
        pg_id = pg.get("_id")
        if pg_id:
            values.append(pg_id)
            values.append(str(pg_id))

    return values


def _sum_number(value):
    """
    Safe float conversion for money/amount fields.
    """
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _calculate_cashflow(pg_ids):
    """
    CLF cashflow summary from available PG-level finance collections.

    This is intentionally read-only.
    It uses best-effort aggregation across existing collections without
    breaking older schemas.
    """
    if not pg_ids:
        return {
            "cash_in": 0.0,
            "cash_out": 0.0,
            "balance": 0.0,
            "grant_received": 0.0,
            "grant_utilized": 0.0,
            "loan_principal": 0.0,
            "loan_repaid": 0.0,
            "member_loan_principal": 0.0,
            "member_loan_repaid": 0.0,
        }

    db = current_app.mongo_db

    cash_in = 0.0
    cash_out = 0.0
    grant_received = 0.0
    grant_utilized = 0.0
    loan_principal = 0.0
    loan_repaid = 0.0
    member_loan_principal = 0.0
    member_loan_repaid = 0.0

    # Grants received
    for grant in db.pg_grants.find({"pg_id": {"$in": pg_ids}}):
        amount = (
            grant.get("amount")
            or grant.get("received_amount")
            or grant.get("release_amount")
            or grant.get("grant_amount")
            or 0
        )
        grant_received += _sum_number(amount)

    # Grant utilization
    for util in db.pg_grant_utilizations.find({"pg_id": {"$in": pg_ids}}):
        amount = (
            util.get("amount")
            or util.get("utilized_amount")
            or util.get("utilization_amount")
            or 0
        )
        grant_utilized += _sum_number(amount)

    # PG loan accounts
    for loan in db.pg_loan_accounts.find({"pg_id": {"$in": pg_ids}}):
        principal = (
            loan.get("principal")
            or loan.get("principal_amount")
            or loan.get("sanctioned_amount")
            or loan.get("amount")
            or 0
        )
        repaid = (
            loan.get("principal_repaid")
            or loan.get("total_repaid")
            or loan.get("repaid_amount")
            or 0
        )
        loan_principal += _sum_number(principal)
        loan_repaid += _sum_number(repaid)

    # Member loan accounts
    for loan in db.pg_member_loan_accounts.find({"pg_id": {"$in": pg_ids}}):
        principal = (
            loan.get("principal")
            or loan.get("principal_amount")
            or loan.get("loan_amount")
            or loan.get("amount")
            or 0
        )
        repaid = (
            loan.get("principal_repaid")
            or loan.get("total_repaid")
            or loan.get("repaid_amount")
            or 0
        )
        member_loan_principal += _sum_number(principal)
        member_loan_repaid += _sum_number(repaid)

    cash_in = grant_received + loan_principal + member_loan_principal
    cash_out = grant_utilized + loan_repaid + member_loan_repaid
    balance = cash_in - cash_out

    return {
        "cash_in": round(cash_in, 2),
        "cash_out": round(cash_out, 2),
        "balance": round(balance, 2),
        "grant_received": round(grant_received, 2),
        "grant_utilized": round(grant_utilized, 2),
        "loan_principal": round(loan_principal, 2),
        "loan_repaid": round(loan_repaid, 2),
        "member_loan_principal": round(member_loan_principal, 2),
        "member_loan_repaid": round(member_loan_repaid, 2),
    }


def _registration_status_counts(pgs):
    """
    Count PG registration and member registration validation status.
    """
    result = {
        "pg_registration": {
            "draft": 0,
            "submitted": 0,
            "approved": 0,
            "rejected": 0,
            "resubmitted": 0,
            "unknown": 0,
        },
        "member_registration": {
            "draft": 0,
            "submitted": 0,
            "approved": 0,
            "rejected": 0,
            "resubmitted": 0,
            "unknown": 0,
        },
    }

    allowed = {"draft", "submitted", "approved", "rejected", "resubmitted"}

    for pg in pgs:
        pg_status = (
            (pg.get("registration_validation") or {}).get("status")
            or pg.get("registration_validation_status")
            or "draft"
        )

        member_status = (
            (pg.get("member_registration_validation") or {}).get("status")
            or pg.get("member_registration_validation_status")
            or "draft"
        )

        pg_status = str(pg_status).lower()
        member_status = str(member_status).lower()

        if pg_status not in allowed:
            pg_status = "unknown"

        if member_status not in allowed:
            member_status = "unknown"

        result["pg_registration"][pg_status] += 1
        result["member_registration"][member_status] += 1

    return result


def _member_count_for_pgs(pg_ids):
    """
    Count active members under assigned PGs.
    """
    if not pg_ids:
        return 0

    query = {
        "pg_id": {"$in": pg_ids}
    }

    return current_app.mongo_db.pg_members.count_documents(query)

def _safe_name(doc, fallback="-"):
    """
    Safely return display name from master documents.
    """
    if not doc:
        return fallback

    return (
        doc.get("name")
        or doc.get("Name")
        or doc.get("title")
        or doc.get("code")
        or fallback
    )


def _get_master_doc(collection_name, value):
    """
    Fetch state/district/block master document with ObjectId/string fallback.
    """
    if not value:
        return None

    db = current_app.mongo_db
    oid = _to_object_id(value)

    if oid:
        doc = db[collection_name].find_one({"_id": oid})
        if doc:
            return doc

    return db[collection_name].find_one({"_id": str(value)})


def _get_clf_location_details(clf):
    """
    Resolve mapped State, District and Block details for CLF profile.
    """
    state = _get_master_doc("states", clf.get("state_id"))
    district = _get_master_doc("districts", clf.get("district_id"))
    block = _get_master_doc("blocks", clf.get("block_id"))

    return {
        "state": state,
        "district": district,
        "block": block,
        "state_name": _safe_name(state),
        "district_name": _safe_name(district),
        "block_name": _safe_name(block),
    }


def _get_clf_mapping_summary(clf, pgs):
    """
    Calculate village count and PG count after PGs are mapped to CLF.
    """
    villages = set()

    for pg in pgs:
        village_name = (
            pg.get("Village")
            or pg.get("village")
            or pg.get("village_name")
            or pg.get("Village Name")
            or ""
        )

        village_name = str(village_name).strip()

        if village_name:
            villages.add(village_name.lower())

    return {
        "pg_count": len(pgs),
        "village_covered_count": len(villages),
        "village_names": sorted(villages),
    }
# ------------------------------------------------------------
# CLF Routes
# ------------------------------------------------------------

@clf_bp.route("/")
def index():
    return redirect(url_for("clf.dashboard"))


@clf_bp.route("/dashboard")
def dashboard():
    """
    CLF Admin dashboard.

    CLF can only view its assigned/mapped PGs.
    CLF cannot create PG.
    """
    user, response = _require_clf_admin()
    if response:
        return response

    clf = _get_clf_doc(user.get("clf_id"))
    pgs = _get_assigned_pgs(user)
    pg_ids = _pg_ids_for_query(pgs)

    total_pgs = len(pgs)
    total_members = _member_count_for_pgs(pg_ids)
    cashflow = _calculate_cashflow(pg_ids)
    validation_counts = _registration_status_counts(pgs)

    recent_pgs = pgs[:8]

    context = {
        "user": user,
        "clf": clf,
        "pgs": pgs,
        "recent_pgs": recent_pgs,
        "total_pgs": total_pgs,
        "total_members": total_members,
        "cashflow": cashflow,
        "validation_counts": validation_counts,
        "now": datetime.utcnow(),
    }

    if request.args.get("format") == "json":
        return jsonify({
            "success": True,
            "clf": {
                "id": str(clf.get("_id", "")),
                "name": clf.get("name", ""),
            },
            "total_pgs": total_pgs,
            "total_members": total_members,
            "cashflow": cashflow,
            "validation_counts": validation_counts,
            "pgs": [
                {
                    "id": str(pg.get("_id")),
                    "name": pg.get("name") or pg.get("pg_name") or "",
                    "district_id": str(pg.get("district_id", "")),
                    "block_id": str(pg.get("block_id", "")),
                    "clf_id": str(pg.get("clf_id", "")),
                }
                for pg in pgs
            ],
        })

    return render_template("clf/dashboard.html", **context)


@clf_bp.route("/profile")
def profile():
    """
    CLF Admin profile page.

    Shows the CLF details entered by Block Admin while creating CLF:
    - mapped State/District/Block
    - VC name/location
    - President details
    - Secretary details
    - registration details
    - CLF type
    - EC member count
    - calculated village count after PG mapping
    - calculated PG count after PG mapping
    """
    user, response = _require_clf_admin()
    if response:
        return response

    clf = _get_clf_doc(user.get("clf_id"))
    pgs = _get_assigned_pgs(user)

    location_details = _get_clf_location_details(clf)
    mapping_summary = _get_clf_mapping_summary(clf, pgs)

    if request.args.get("format") == "json":
        return jsonify({
            "success": True,
            "clf": {
                "id": str(clf.get("_id", "")),
                "name": clf.get("name") or clf.get("clf_name") or "",
                "state": location_details["state_name"],
                "district": location_details["district_name"],
                "block": location_details["block_name"],
                "vc_name_location": clf.get("vc_name_location") or "",
                "president_name": clf.get("president_name") or "",
                "president_contact": clf.get("president_contact") or "",
                "secretary_name": clf.get("secretary_name") or "",
                "secretary_contact": clf.get("secretary_contact") or "",
                "is_registered": clf.get("is_registered") or "",
                "registration_date": clf.get("registration_date_raw") or "",
                "clf_type": clf.get("clf_type") or "",
                "ec_members_count": clf.get("ec_members_count") or 0,
                "village_covered_count": mapping_summary["village_covered_count"],
                "pg_count": mapping_summary["pg_count"],
            },
        })

    return render_template(
        "clf/profile.html",
        user=user,
        clf=clf,
        pgs=pgs,
        location_details=location_details,
        mapping_summary=mapping_summary,
    )

@clf_bp.route("/assigned-pgs")
def assigned_pgs():
    """
    List all PGs assigned/mapped to the logged-in CLF.
    """
    user, response = _require_clf_admin()
    if response:
        return response

    clf = _get_clf_doc(user.get("clf_id"))
    pgs = _get_assigned_pgs(user)

    rows = []

    for pg in pgs:
        pg_id_values = _pg_ids_for_query([pg])
        member_count = _member_count_for_pgs(pg_id_values)

        rows.append({
            "pg": pg,
            "member_count": member_count,
            "registration_status": (
                (pg.get("registration_validation") or {}).get("status")
                or pg.get("registration_validation_status")
                or "draft"
            ),
            "member_registration_status": (
                (pg.get("member_registration_validation") or {}).get("status")
                or pg.get("member_registration_validation_status")
                or "draft"
            ),
        })

    if request.args.get("format") == "json":
        return jsonify({
            "success": True,
            "clf": {
                "id": str(clf.get("_id", "")),
                "name": clf.get("name", ""),
            },
            "pgs": [
                {
                    "id": str(row["pg"].get("_id")),
                    "name": row["pg"].get("name") or row["pg"].get("pg_name") or "",
                    "member_count": row["member_count"],
                    "registration_status": row["registration_status"],
                    "member_registration_status": row["member_registration_status"],
                }
                for row in rows
            ],
        })

    return render_template(
        "clf/assigned_pgs.html",
        user=user,
        clf=clf,
        rows=rows,
        total_pgs=len(rows),
    )


@clf_bp.route("/pg/<pg_id>/view")
def view_pg(pg_id):
    """
    CLF can view only PGs mapped/assigned to its CLF.
    This route is read-only.
    """
    user, response = _require_clf_admin()
    if response:
        return response

    pgs = _get_assigned_pgs(user)
    allowed_pg_ids = {str(pg.get("_id")) for pg in pgs}

    if str(pg_id) not in allowed_pg_ids:
        flash("You are not authorized to view this PG.", "danger")
        return redirect(url_for("clf.assigned_pgs"))

    pg_oid = _to_object_id(pg_id)
    pg = None

    if pg_oid:
        pg = current_app.mongo_db.pgs.find_one({"_id": pg_oid})

    if not pg:
        pg = current_app.mongo_db.pgs.find_one({"_id": str(pg_id)})

    if not pg:
        flash("PG not found.", "danger")
        return redirect(url_for("clf.assigned_pgs"))

    pg_id_values = _pg_ids_for_query([pg])
    members = list(current_app.mongo_db.pg_members.find({"pg_id": {"$in": pg_id_values}}).sort("name", 1))
    cashflow = _calculate_cashflow(pg_id_values)

    if request.args.get("format") == "json":
        return jsonify({
            "success": True,
            "pg": {
                "id": str(pg.get("_id")),
                "name": pg.get("name") or pg.get("pg_name") or "",
                "registration_validation": pg.get("registration_validation") or {},
                "member_registration_validation": pg.get("member_registration_validation") or {},
            },
            "members_count": len(members),
            "cashflow": cashflow,
        })

    return render_template(
        "clf/pg_view.html",
        user=user,
        pg=pg,
        members=members,
        cashflow=cashflow,
    )


@clf_bp.route("/cashflow")
def cashflow():
    """
    Read-only CLF cashflow summary.
    """
    user, response = _require_clf_admin()
    if response:
        return response

    clf = _get_clf_doc(user.get("clf_id"))
    pgs = _get_assigned_pgs(user)
    pg_ids = _pg_ids_for_query(pgs)
    cashflow_summary = _calculate_cashflow(pg_ids)

    if request.args.get("format") == "json":
        return jsonify({
            "success": True,
            "clf": {
                "id": str(clf.get("_id", "")),
                "name": clf.get("name", ""),
            },
            "cashflow": cashflow_summary,
        })

    return render_template(
        "clf/cashflow.html",
        user=user,
        clf=clf,
        pgs=pgs,
        cashflow=cashflow_summary,
    )