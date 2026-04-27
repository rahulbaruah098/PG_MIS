import os
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime
from bson import ObjectId
from app.utils import hash_password

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/pg_mis")

client = MongoClient(MONGO_URI)
db = client.get_default_database()

def main():
    db.users.delete_many({})
    db.states.delete_many({})
    db.districts.delete_many({})
    db.blocks.delete_many({})
    db.clfs.delete_many({})
    db.pgs.delete_many({})
    db.pg_members.delete_many({})

    state_id = db.states.insert_one({"code": "MZ", "name": "Mizoram", "created_at": datetime.utcnow()}).inserted_id
    district_id = db.districts.insert_one({"name": "Kolasib", "state_id": state_id, "created_at": datetime.utcnow()}).inserted_id
    block_id = db.blocks.insert_one({"name": "Kolasib Block", "district_id": district_id, "created_at": datetime.utcnow()}).inserted_id

    db.users.insert_one({
        "username": "superadmin",
        "password_hash": hash_password("Super@123"),
        "role": "SUPER_ADMIN",
        "state_id": None,
        "district_id": None,
        "block_id": None,
        "clf_id": None,
        "pg_id": None,
        "status": "active",
        "created_at": datetime.utcnow(),
        "last_login": None,
    })

    # db.users.insert_one({
    #     "username": "mizoram_admin",
    #     "password_hash": hash_password("Admin@123"),
    #     "role": "ADMIN",
    #     "state_id": state_id,
    #     "district_id": None,
    #     "block_id": None,
    #     "clf_id": None,
    #     "pg_id": None,
    #     "status": "active",
    #     "created_at": datetime.utcnow(),
    #     "last_login": None,
    # })

    # db.users.insert_one({
    #     "username": "kolasib_district",
    #     "password_hash": hash_password("District@123"),
    #     "role": "DISTRICT_ADMIN",
    #     "state_id": state_id,
    #     "district_id": district_id,
    #     "block_id": None,
    #     "clf_id": None,
    #     "pg_id": None,
    #     "status": "active",
    #     "created_at": datetime.utcnow(),
    #     "last_login": None,
    # })

    print("Seed data inserted.")
    print("Login as SUPER_ADMIN:")
    print("  username: superadmin, password: Super@123")

if __name__ == "__main__":
    main()
