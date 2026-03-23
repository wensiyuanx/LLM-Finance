import sys
import os
import logging
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from database.db import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_migration():
    try:
        with engine.connect() as conn:
            # Check if column exists
            result = conn.execute(text("SHOW COLUMNS FROM asset_monitor LIKE 'is_leveraged'"))
            if not result.fetchone():
                logger.info("Adding 'is_leveraged' column to asset_monitor table...")
                conn.execute(text("ALTER TABLE asset_monitor ADD COLUMN is_leveraged INTEGER DEFAULT 0"))
                logger.info("Column added successfully.")
                
                # Mark HK.07226 as leveraged as we know it is one
                conn.execute(text("UPDATE asset_monitor SET is_leveraged = 1 WHERE code = 'HK.07226'"))
                logger.info("Marked HK.07226 as leveraged.")
                
                conn.commit()
            else:
                logger.info("Column 'is_leveraged' already exists.")

            result = conn.execute(text("SHOW COLUMNS FROM signal_records LIKE 'current_price'"))
            if not result.fetchone():
                logger.info("Adding 'current_price' column to signal_records table...")
                conn.execute(text("ALTER TABLE signal_records ADD COLUMN current_price FLOAT NULL"))
                logger.info("Column added successfully.")
                conn.commit()
            else:
                logger.info("Column 'current_price' already exists.")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

if __name__ == "__main__":
    run_migration()
