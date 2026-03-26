from sqlalchemy import text
from database.db import engine

def migrate():
    print("Migrating database...")
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE trade_records ADD COLUMN realized_pnl FLOAT DEFAULT 0.0"))
            print("Added realized_pnl to trade_records")
        except Exception as e:
            print(f"realized_pnl might already exist: {e}")
            
        try:
            conn.execute(text("ALTER TABLE trade_records ADD COLUMN pnl_pct FLOAT DEFAULT 0.0"))
            print("Added pnl_pct to trade_records")
        except Exception as e:
            print(f"pnl_pct might already exist: {e}")
            
        try:
            conn.execute(text("ALTER TABLE user_wallets ADD COLUMN total_assets FLOAT DEFAULT 0.0"))
            print("Added total_assets to user_wallets")
        except Exception as e:
            print(f"total_assets might already exist: {e}")
            
        try:
            conn.execute(text("ALTER TABLE user_wallets ADD COLUMN total_pnl FLOAT DEFAULT 0.0"))
            print("Added total_pnl to user_wallets")
        except Exception as e:
            print(f"total_pnl might already exist: {e}")

        try:
            conn.execute(text("UPDATE user_wallets SET total_assets = balance WHERE total_assets = 0.0 OR total_assets IS NULL"))
        except Exception as e:
            pass
            
        conn.commit()
    print("Migration complete!")

if __name__ == '__main__':
    migrate()
