from database.db import engine
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def update_schema():
    with engine.connect() as conn:
        try:
            logger.info("Adding last_price to holdings table...")
            conn.execute(text("ALTER TABLE holdings ADD COLUMN last_price FLOAT DEFAULT 0.0 AFTER is_trend"))
            conn.commit()
            logger.info("Successfully added last_price.")
        except Exception as e:
            conn.rollback()
            logger.warning(f"Failed to add last_price (it might already exist): {e}")

        try:
            logger.info("Adding board_lot to asset_monitor table...")
            conn.execute(text("ALTER TABLE asset_monitor ADD COLUMN board_lot INT DEFAULT 100 AFTER last_updated"))
            conn.commit()
            logger.info("Successfully added board_lot.")
        except Exception as e:
            conn.rollback()
            logger.warning(f"Failed to add board_lot (it might already exist): {e}")

if __name__ == "__main__":
    update_schema()
