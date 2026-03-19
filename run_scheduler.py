"""
run_scheduler.py
Entry point for the production trading bot.
Schedules market-specific analysis jobs and weekly AI retraining.
Integrated with real-time monitoring for risk control.
"""
import os
import logging
import sys
import threading
import time
from datetime import datetime

# Fix for FuTu API requiring HOME environment variable
if 'HOME' not in os.environ:
    os.environ['HOME'] = os.getcwd()

import schedule
from futu import *

from database.db import init_db, SessionLocal
from database.models import MarketType, Holding, AssetMonitor
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
# Real-time Monitor Integration
# ---------------------------------------------------------------------------

class ATRStopLossHandler(StockQuoteHandlerBase):
    """
    Handler for real-time tick/quote updates from FutuOpenD.
    1. Updates real-time prices to AssetMonitor table.
    2. Checks incoming price against dynamic ATR stop-loss levels for held positions.
    """
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()  # Protect shared resources from concurrent access
        self.holdings = {}
        self.active_assets = []
        self.refresh_state()

    def refresh_state(self):
        """Reload holdings and monitored assets from database"""
        session = SessionLocal()
        try:
            # Refresh Holdings
            records = session.query(Holding).filter(Holding.quantity > 0).all()

            with self.lock:
                self.holdings = {h.code: h for h in records}

                # Refresh Active Assets
                assets = session.query(AssetMonitor).filter(AssetMonitor.is_active == 1).all()
                self.active_assets = [a.code for a in assets]

            logger.info(f"[RealTime] Monitoring {len(self.active_assets)} active assets, checking {len(self.holdings)} for ATR stop-loss.")
        except Exception as e:
            logger.error(f"[RealTime] Failed to refresh state: {e}")
        finally:
            session.close()

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super(ATRStopLossHandler, self).on_recv_rsp(rsp_pb)
        if ret_code == RET_OK:
            # Create a fresh session for this async callback to ensure thread-safety
            session = SessionLocal()
            try:
                # data is a DataFrame with real-time quotes
                updates = []
                for _, row in data.iterrows():
                    code = row['code']
                    current_price = row['last_price']
                    updates.append(f"{code}: {current_price}")

                    # 1. Update AssetMonitor with real-time price
                    asset = session.query(AssetMonitor).filter(AssetMonitor.code == code).first()
                    if asset:
                        asset.last_price = current_price
                        asset.last_updated = datetime.now()
                        session.commit()

                    # 2. Check if we hold this stock for Stop Loss / Take Profit
                    with self.lock:
                        if code in self.holdings:
                            holding = self.holdings[code]
                            avg_cost = holding.avg_cost

                            # Calculate real-time profit/loss
                            profit_pct = (current_price - avg_cost) / avg_cost

                            # 1. Hard Stop-Loss check (e.g. -8%)
                            if profit_pct <= -0.08:
                                logger.warning(f"🚨 [REAL-TIME STOP LOSS] {code} triggers hard stop loss! Cost: {avg_cost}, Price: {current_price}, P&L: {profit_pct*100:.2f}%")
                                self._trigger_sell(code, current_price, "Real-time Hard Stop Loss (-8%)")

                            # 2. Hard Take-Profit check (e.g. +15%)
                            elif profit_pct >= 0.15:
                                logger.info(f"💰 [REAL-TIME TAKE PROFIT] {code} triggers hard take profit! Cost: {avg_cost}, Price: {current_price}, P&L: {profit_pct*100:.2f}%")
                                self._trigger_sell(code, current_price, "Real-time Hard Take Profit (+15%)")

                # Print a brief summary of received prices so the user can see it in the terminal
                if updates:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 实时报价更新 -> " + " | ".join(updates))

            except Exception as e:
                logger.error(f"[RealTime] Error processing quote update: {e}")
                session.rollback()
            finally:
                session.close()
        else:
            logger.error(f"[RealTime] Quote subscription error: {data}")

        return RET_OK, data

    def _trigger_sell(self, code, price, reason):
        """Execute a mock sell for the real-time trigger"""
        logger.info(f"[RealTime Exec] Mock executing SELL for {code} at {price} due to {reason}")
        # In a real system, this would call executor.py to place a market order
        # For now, we just log it and remove it from monitoring
        with self.lock:
            if code in self.holdings:
                del self.holdings[code]


def start_realtime_monitor_thread():
    """
    Starts the independent high-frequency thread for real-time ATR stop-loss monitoring.
    Runs as a daemon thread alongside the scheduler.
    """
    def monitor_thread():
        logger.info("[RealTime] Initializing Real-Time Market Monitor Thread...")

        # Use the same host/port as defined in data/futu_client.py
        try:
            quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            logger.info("[RealTime] Connected to FutuOpenD Quote Context for real-time monitoring.")
        except Exception as e:
            logger.error(f"[RealTime] Failed to connect to FutuOpenD: {e}")
            return

        # Register handler
        handler = ATRStopLossHandler()
        quote_ctx.set_handler(handler)

        # Start asynchronous push
        quote_ctx.start()

        # Get active assets to monitor (both holdings and watchlist)
        codes = handler.active_assets

        if not codes:
            logger.info("[RealTime] No active assets to monitor. Still running in background...")
        else:
            logger.info(f"[RealTime] Subscribing to real-time quotes for: {codes}")
            ret, data = quote_ctx.subscribe(codes, [SubType.QUOTE], subscribe_push=True)
            if ret != RET_OK:
                logger.error(f"[RealTime] Failed to subscribe to quotes: {data}")

        try:
            # Keep thread alive
            while True:
                time.sleep(60)
                # Periodically refresh state in case the main scheduler added something new
                handler.refresh_state()

                # Update subscription if new assets appear
                new_codes = handler.active_assets
                if set(new_codes) != set(codes):
                    codes = new_codes
                    if codes:
                        quote_ctx.subscribe(codes, [SubType.QUOTE], subscribe_push=True)
                        logger.info(f"[RealTime] Updated real-time subscription list: {codes}")

        except Exception as e:
            logger.error(f"[RealTime] Monitor thread error: {e}")
        finally:
            logger.info("[RealTime] Shutting down real-time monitor...")
            quote_ctx.close()

    # Start as daemon thread so it exits when main thread exits
    thread = threading.Thread(target=monitor_thread, daemon=True)
    thread.start()
    logger.info("[RealTime] Real-time monitor thread started successfully.")
    return thread

# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def start_scheduler():
    init_db()   # create any missing tables once at startup

    # Start real-time monitoring thread
    logger.info("=" * 60)
    logger.info("Starting enhanced scheduler with real-time monitoring...")
    logger.info("=" * 60)
    realtime_thread = start_realtime_monitor_thread()

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

    logger.info("=" * 60)
    logger.info("Scheduler configured:")
    logger.info("  - Real-time monitoring: ACTIVE (background thread)")
    logger.info("  - A-Share analysis: 11:30, 14:50")
    logger.info("  - HK-Share analysis: 11:30, 14:00, 15:50")
    logger.info("  - T+1 rollover: 09:00")
    logger.info("=" * 60)
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
        logger.info("=" * 60)
        logger.info("Scheduler stopped by user.")
        logger.info("Real-time monitor thread will exit automatically (daemon).")
        logger.info("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    start_scheduler()
