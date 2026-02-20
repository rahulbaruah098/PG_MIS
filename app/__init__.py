from flask import Flask
from pymongo import MongoClient, ASCENDING
from .config import Config

mongo_client = None

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

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
    db.pg_documents.create_index([("pg_id", ASCENDING), ("doc_type", ASCENDING), ("uploaded_at", ASCENDING)])

    # Duplicate prevention (best-effort)
    db.pgs.create_index([("pg_name", ASCENDING), ("block_id", ASCENDING)], unique=True)

    # SHG Master (TRESP import)
    # Master fields are stored using human-readable column names.
    # These indexes keep cascading dropdown queries fast even at ~1L+ member rows.
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING), ("Village", ASCENDING)])
    db.shg_master.create_index([("SHG Code", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING)])
    db.shg_master.create_index([("State", ASCENDING), ("District", ASCENDING), ("Block", ASCENDING), ("Gram Panchayat", ASCENDING), ("Village", ASCENDING), ("SHG Code", ASCENDING)])

    db.shg_members_master.create_index([("SHG Code", ASCENDING), ("Member Code", ASCENDING)])
