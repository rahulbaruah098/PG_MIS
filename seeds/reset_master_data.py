"""Reset (delete) SHG master collections in MongoDB.

Usage:
  python seeds/reset_master_data.py

This will DELETE:
  - shg_master
  - shg_members_master
"""
import os
from dotenv import load_dotenv
from pymongo import MongoClient

def main():
    load_dotenv()
    uri=os.getenv("MONGO_URI")
    dbname=os.getenv("MONGO_DB","pg_mis")
    if not uri:
        raise RuntimeError("MONGO_URI not set")
    client=MongoClient(uri)
    db=client[dbname]
    a=db.shg_master.delete_many({})
    b=db.shg_members_master.delete_many({})
    print(f"Deleted shg_master: {a.deleted_count}")
    print(f"Deleted shg_members_master: {b.deleted_count}")

if __name__=="__main__":
    main()
