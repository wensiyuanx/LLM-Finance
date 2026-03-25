import asyncio
import logging
from sqlalchemy import select
from database.db import SessionLocal
from database.models import AssetMonitor
from engine.ml_predictor import ml_predictor
from data.futu_client import FutuClient
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def main():
    futu_client = FutuClient()
    if not futu_client.connect():
        logger.error("Failed to connect to FutuOpenD")
        return
        
    session = SessionLocal()
    assets = session.query(AssetMonitor).filter(AssetMonitor.is_active == 1).all()
    
    logger.info(f"Starting ML model training for {len(assets)} active assets...")
    
    for asset in assets:
        try:
            logger.info(f"Fetching data and training for {asset.code}...")
            
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
            
            df = futu_client.get_historical_klines(asset.code, start_date=start_date, end_date=end_date, ktype='K_60M')
            
            if df is not None and len(df) > 100:
                success = ml_predictor.train_model(df, asset.code)
                if success:
                    logger.info(f"Successfully trained model for {asset.code}")
            else:
                logger.warning(f"Not enough data for {asset.code} (got {len(df) if df is not None else 0} rows)")
            
            # Rate limiting
            time.sleep(1)
        except Exception as e:
            logger.error(f"Failed to train model for {asset.code}: {e}")
            
    futu_client.close()
    session.close()
    logger.info("Finished all ML training.")

if __name__ == "__main__":
    asyncio.run(main())
