from flask import Flask, app
from pymongo import MongoClient, ASCENDING
from .config import Config
from flask_cors import CORS
from bson import ObjectId
from flask import session

mongo_client = None

def create_app():
    app = Flask(__name__)
    CORS(app, supports_credentials=True)
    app.config.from_object(Config)
    app.config["JWT_SECRET_KEY"] = "change-this-to-strong-secret-key"
    app.config["JWT_EXPIRATION_SECONDS"] = 86400  # 1 day
    global mongo_client
    mongo_client = MongoClient(app.config["MONGO_URI"])
    app.mongo_db = mongo_client.get_default_database()

    init_indexes(app.mongo_db)

    # Blueprints
    from .auth.routes import auth_bp
    from .master_data.routes import master_data_bp
    from .pg.routes import pg_bp
    from .finance.routes import finance_bp
    from .business.routes import business_bp
    from .reports.routes import reports_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(master_data_bp, url_prefix="/master")
    app.register_blueprint(pg_bp, url_prefix="/pg")
    app.register_blueprint(finance_bp, url_prefix="/finance")
    app.register_blueprint(business_bp, url_prefix="/business")
    app.register_blueprint(reports_bp, url_prefix="/reports")

    @app.route("/")
    def index():
        from flask import redirect, url_for
        return redirect(url_for("auth.login"))

    # ---- Role label helper for templates ----
    from .constants import ROLE_LABELS

    @app.context_processor
    def _inject_role_helpers():
        def role_label(role):
            return ROLE_LABELS.get(role, role or "")
        return {"role_label": role_label, "ROLE_LABELS": ROLE_LABELS}

    # ---- PG/CLF context for PG login (auto-prefill in templates) ----
    @app.context_processor
    def _inject_pg_context():
        """Provide current PG + CLF info for templates.

        Requirement:
        - In PG login, every template should show pre-filled PG name and CLF name.
        """
        try:
            role = session.get("role")
            pg_id = session.get("pg_id")
            if role == "PG_DATA_ENTRY" and pg_id and ObjectId.is_valid(pg_id):
                pg = app.mongo_db.pgs.find_one({"_id": ObjectId(pg_id)}) or {}
                clf = None
                try:
                    clf_id = pg.get("clf_id")
                    if clf_id:
                        clf = app.mongo_db.clfs.find_one({"_id": clf_id}) or None
                except Exception:
                    clf = None

                return {
                    "current_pg": pg,
                    "current_pg_name": pg.get("name") or "",
                    "current_clf": clf,
                    "current_clf_name": (clf or {}).get("name") if clf else "",
                }
        except Exception:
            pass
        return {"current_pg": None, "current_pg_name": "", "current_clf": None, "current_clf_name": ""}

    return app


def init_indexes(db):
    # users
    db.users.create_index("username", unique=True)
    db.users.create_index("role")
    db.users.create_index([("state_id", ASCENDING), ("district_id", ASCENDING)])

    # geo collections
    db.states.create_index("code", unique=True)
    db.districts.create_index([("state_id", ASCENDING), ("name", ASCENDING)])
    db.blocks.create_index([("district_id", ASCENDING), ("name", ASCENDING)])
    db.clfs.create_index([("block_id", ASCENDING), ("name", ASCENDING)])
    db.pgs.create_index([("clf_id", ASCENDING), ("name", ASCENDING)])

    # PG-related
    db.pg_members.create_index("pg_id")
    db.pg_funds.create_index("pg_id")
    db.pg_loans.create_index("pg_id")
    db.pg_member_loans.create_index([("pg_id", ASCENDING), ("member_id", ASCENDING)])
    db.pg_business_monthly.create_index([("pg_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)], unique=True)
    db.pg_market_transactions.create_index([("pg_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)], unique=True)
    db.pg_stocks_monthly.create_index([("pg_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)], unique=True)
    db.pg_income_expenditure.create_index([("pg_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)])
    db.mpr_snapshots.create_index([("level", ASCENDING), ("ref_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)])
    db.audit_logs.create_index([("collection", ASCENDING), ("doc_id", ASCENDING)])

    # workflow / counters / notifications / change requests / documents
    db.counters.create_index("seq")
    db.change_requests.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    db.change_requests.create_index([("collection", ASCENDING), ("doc_id", ASCENDING)])
    db.notifications.create_index([("to_user_id", ASCENDING), ("is_read", ASCENDING), ("ts", ASCENDING)])
    db.notifications.create_index([("to_role", ASCENDING), ("is_read", ASCENDING), ("ts", ASCENDING)])
    # Period locks (month freeze)
    db.period_locks.create_index([("scope", ASCENDING), ("ref_id", ASCENDING), ("year", ASCENDING), ("month", ASCENDING)], unique=True)

    # Loan lifecycle (accounts + repayments)
    db.pg_loan_accounts.create_index([("pg_id", ASCENDING), ("status", ASCENDING), ("loan_no", ASCENDING)])
    db.pg_loan_repayments.create_index([("loan_id", ASCENDING), ("paid_at", ASCENDING)])
    db.pg_member_loan_accounts.create_index([("pg_id", ASCENDING), ("member_id", ASCENDING), ("status", ASCENDING)])
    db.pg_member_loan_repayments.create_index([("loan_id", ASCENDING), ("paid_at", ASCENDING)])

    # Grants / utilization
    db.pg_grants.create_index([("pg_id", ASCENDING), ("category", ASCENDING), ("release_date", ASCENDING)])
    db.pg_grant_utilizations.create_index([("grant_id", ASCENDING), ("utilized_at", ASCENDING)])

    # Grant heads master (for strict head validation in utilization)
    try:
        db.grant_heads.create_index([("name", ASCENDING)], unique=True)
        if db.grant_heads.count_documents({}) == 0:
            db.grant_heads.insert_many([
                {"name": "Infrastructure"},
                {"name": "Equipment"},
                {"name": "Training"},
                {"name": "Raw Materials"},
                {"name": "Packaging"},
                {"name": "Marketing"},
                {"name": "Transportation"},
                {"name": "Operations"},
                {"name": "Others"},
            ], ordered=False)
    except Exception:
        pass

    # Inventory
    db.pg_stock_movements.create_index([("pg_id", ASCENDING), ("commodity", ASCENDING), ("ts", ASCENDING)])

    # Governance + plans + gradation
    db.pg_meetings.create_index([("pg_id", ASCENDING), ("meeting_date", ASCENDING)])
    db.pg_business_plans.create_index([("pg_id", ASCENDING), ("year", ASCENDING)], unique=True)
    db.pg_gradation_snapshots.create_index([("pg_id", ASCENDING), ("year", ASCENDING), ("quarter", ASCENDING)], unique=True)

    db.pg_documents.create_index([("pg_id", ASCENDING), ("doc_type", ASCENDING), ("uploaded_at", ASCENDING)])

    # Duplicate prevention (best-effort)
    # Enforce unique PG name within a block (ignore missing/empty names)
    db.pgs.create_index(
    [("name", ASCENDING), ("block_id", ASCENDING)],
    unique=True
)   

    # SHG Master (TRESP import)
    # Master fields are stored using human-readable column names.
    # These indexes keep cascading dropdown queries fast even at ~1L+ member rows.
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING), ("Village", ASCENDING)])
    db.shg_master.create_index([("SHG Code", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING), ("Village", ASCENDING), ("SHG Code", ASCENDING)])

    db.shg_members_master.create_index([("SHG Code", ASCENDING), ("Member Code", ASCENDING)])
