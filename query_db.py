from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv
import pandas as pd

load_dotenv()
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    print("--- TRADE RECORDS ---")
    trades = pd.read_sql("SELECT * FROM trade_records WHERE code='HK.01138' ORDER BY created_at DESC LIMIT 5", conn)
    print(trades)
    
    print("\n--- SIGNAL RECORDS ---")
    signals = pd.read_sql("SELECT * FROM signal_records WHERE code='HK.01138' ORDER BY created_at DESC LIMIT 5", conn)
    print(signals)
