import os
import sys

# Add the parent directory to sys.path to allow importing from database module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import engine, Base, SessionLocal
from database.models import KLineData, TradeRecord, AssetMonitor, MarketType, UserWallet

def seed_assets(db):
    try:
        from sqlalchemy import text
        # Check if is_etf column exists, if not, add it
        db.execute(text("ALTER TABLE asset_monitor ADD COLUMN is_etf BOOLEAN DEFAULT FALSE"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        # Add tranches_count tracking columns
        db.execute(text("ALTER TABLE holdings ADD COLUMN tranches_count INTEGER DEFAULT 0"))
        db.commit()
    except Exception:
        db.rollback()

    default_assets = [
        {"code": "SZ.159915", "market": MarketType.A_SHARE, "is_etf": True},
        {"code": "HK.00700", "market": MarketType.HK_SHARE, "is_etf": False},
        {"code": "SH.510300", "market": MarketType.A_SHARE, "is_etf": True},
    ]
    
    for asset in default_assets:
        exists = db.query(AssetMonitor).filter(AssetMonitor.code == asset["code"]).first()
        if not exists:
            new_asset = AssetMonitor(
                code=asset["code"], 
                market_type=asset["market"],
                is_etf=asset.get("is_etf", False)
            )
            db.add(new_asset)
    db.commit()
    print("Database seeded with default assets.")

def seed_wallets(db):
    """Seed default mock wallets for user 1 with market-specific balances."""
    default_wallets = [
        {"market": MarketType.A_SHARE,  "balance": 10000.0, "currency": "CNY"},
        {"market": MarketType.HK_SHARE, "balance": 20000.0, "currency": "HKD"},
        {"market": MarketType.US_SHARE, "balance": 500.0,   "currency": "USD"},
    ]
    for w in default_wallets:
        exists = db.query(UserWallet).filter(
            UserWallet.user_id == 1,
            UserWallet.market_type == w["market"]
        ).first()
        if not exists:
            db.add(UserWallet(user_id=1, market_type=w["market"], balance=w["balance"], currency=w["currency"]))
    db.commit()
    print("Database seeded with default wallet balances.")

def main():
    try:
        print("Creating tables in database...")
        Base.metadata.create_all(bind=engine)
        print("Database tables created successfully!")
        
        # Seed default data
        db = SessionLocal()
        seed_assets(db)
        seed_wallets(db)
        db.close()
        
    except Exception as e:
        print(f"Error creating database tables: {e}")
        print("Please ensure MySQL is running and the database exists and credentials in .env are correct.")

if __name__ == "__main__":
    main()
