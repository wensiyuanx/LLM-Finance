"""
run_scheduler.py
Entry point for the production trading bot.
Schedules market-specific analysis jobs and weekly AI retraining.
"""
import logging
import sys
import threading
import time

import schedule

from database.db import init_db
from database.models import MarketType
from main import run_trading_bot, rollover_t1_holdings_task

# ---------------------------------------------------------------------------
# Logging — write to stdout AND a persistent log file
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def job_a_share():
    """Runs intraday and pre-close for A-Share."""
    logger.info("=== A-Share Market Job Started ===")
    try:
        run_trading_bot(market_filter=[MarketType.A_SHARE])
    except Exception as e:
        logger.error("A-Share job failed: %s", e, exc_info=True)
    logger.info("=== A-Share Market Job Finished ===")

def job_hk_share():
    """Runs intraday and pre-close for HK-Share."""
    logger.info("=== HK-Share Market Job Started ===")
    try:
        run_trading_bot(market_filter=[MarketType.HK_SHARE])
    except Exception as e:
        logger.error("HK-Share job failed: %s", e, exc_info=True)
    logger.info("=== HK-Share Market Job Finished ===")

def job_rollover_t1():
    """Converts T+1 locked shares to sellable. Runs once per day."""
    logger.info("=== T+1 Rollover Job Started ===")
    try:
        rollover_t1_holdings_task()
        logger.info("Successfully rolled over A-Share holdings.")
    except Exception as e:
        logger.error("Rollover failed: %s", e, exc_info=True)

# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def start_scheduler():
    init_db()   # create any missing tables once at startup

    # T+1 Rollover session: Before A/HK market opens
    schedule.every().day.at("09:00").do(job_rollover_t1)

    # A-Share session: 09:30-11:30, 13:00-15:00
    # Run intraday (11:30) and pre-close (14:50) for MTF hourly/daily strategy
    schedule.every().day.at("11:30").do(job_a_share)
    schedule.every().day.at("14:50").do(job_a_share) # Pre-close execution

    # HK-Share session: 09:30-12:00, 13:00-16:00
    # Run intraday (11:30, 14:00) and pre-close (15:50)
    schedule.every().day.at("11:30").do(job_hk_share)
    schedule.every().day.at("14:00").do(job_hk_share)
    schedule.every().day.at("15:50").do(job_hk_share) # Pre-close execution

    logger.info("Scheduler configured — A-Share: 11:30, 14:50 | HK-Share: 11:30, 14:00, 15:50 (MTF Intraday)")
    logger.info("Press Ctrl+C to stop.")

    # Run initial startup analysis
    logger.info("Running initial startup analysis for A & HK shares...")
    job_a_share()
    job_hk_share()

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    start_scheduler()
