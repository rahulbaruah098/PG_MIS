import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(25*1024*1024)))  # 25MB

    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/pg_mis")
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"



