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
from collections import defaultdict

# Fix for FuTu API logger path permission issues on macOS
if 'HOME' not in os.environ:
    os.environ['HOME'] = os.getcwd()
try:
    futu_log_dir = os.path.join(os.environ['HOME'], ".com.futunn.FutuOpenD/Log")
    os.makedirs(futu_log_dir, exist_ok=True)
    test_log_path = os.path.join(futu_log_dir, ".perm_test")
    with open(test_log_path, "w", encoding="utf-8") as f:
        f.write("ok")
    os.remove(test_log_path)
except Exception:
    os.environ['HOME'] = os.getcwd()

import schedule
from futu import *

from database.db import init_db, SessionLocal
from database.models import MarketType, Holding, AssetMonitor
from main import run_trading_bot, rollover_t1_holdings_task
from engine.time_utils import is_market_open, is_holiday
from data.futu_client import FUTU_HOST, FUTU_PORT

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

import asyncio
import time
from datetime import datetime

def job_a_share(force=False):
    """Runs intraday and pre-close for A-Share."""
    start_time = time.time()
    logger.info("=== A-Share Market Job Started ===")
    try:
        # Skip if it's a holiday (unless forced)
        if not force and is_holiday(MarketType.A_SHARE):
            logger.info("A-Share market is on holiday. Skipping analysis.")
            return

        asyncio.run(run_trading_bot(market_filter=[MarketType.A_SHARE], force=force))
    except Exception as e:
        logger.error("A-Share job failed: %s", e, exc_info=True)
    finally:
        elapsed = time.time() - start_time
        logger.info("=== A-Share Market Job Finished (Elapsed: %.1fs) ===", elapsed)

def job_hk_share(force=False):
    """Runs intraday and pre-close for HK-Share."""
    start_time = time.time()
    logger.info("=== HK-Share Market Job Started ===")
    try:
        # Skip if it's a holiday (unless forced)
        if not force and is_holiday(MarketType.HK_SHARE):
            logger.info("HK-Share market is on holiday. Skipping analysis.")
            return

        asyncio.run(run_trading_bot(market_filter=[MarketType.HK_SHARE], force=force))
    except Exception as e:
        logger.error("HK-Share job failed: %s", e, exc_info=True)
    finally:
        elapsed = time.time() - start_time
        logger.info("=== HK-Share Market Job Finished (Elapsed: %.1fs) ===", elapsed)

def job_rollover_t1():
    """Converts T+1 locked shares to sellable. Runs once per day."""
    start_time = time.time()
    logger.info("=== T+1 Rollover Job Started ===")
    try:
        # Skip if it's a holiday
        if is_holiday(MarketType.A_SHARE):
            logger.info("A-Share market is on holiday. Skipping T+1 rollover.")
            return

        asyncio.run(rollover_t1_holdings_task())
        logger.info("Successfully rolled over A-Share holdings.")
    except Exception as e:
        logger.error("Rollover failed: %s", e, exc_info=True)
    finally:
        elapsed = time.time() - start_time
        logger.info("=== T+1 Rollover Job Finished (Elapsed: %.1fs) ===", elapsed)


# ---------------------------------------------------------------------------
# Real-time Monitor Integration
# ---------------------------------------------------------------------------

class ATRStopLossHandler(StockQuoteHandlerBase):
    """
    Enhanced Handler for real-time tick/quote updates from FutuOpenD with caching and batch writing.
    1. Updates real-time prices to AssetMonitor table using batch writing.
    2. Checks incoming price against dynamic ATR stop-loss levels for held positions.
    3. Uses in-memory cache for AssetMonitor to reduce database queries.
    """
    def __init__(self, batch_interval=5.0):
        super().__init__()
        self.lock = threading.RLock()  # Protect shared resources from concurrent access
        self.holdings = {}
        self.active_assets = []

        # Cache for AssetMonitor data to reduce database queries
        self.asset_cache = {}  # {code: AssetMonitor object}

        # Batch writing system
        self.pending_updates = {}  # {code: (price, timestamp)}
        self.batch_interval = batch_interval  # seconds between batch writes
        self.last_batch_write = time.time()
        self.batch_write_event = threading.Event()

        # Performance monitoring
        self.update_count = 0
        self.batch_write_count = 0

        self.refresh_state()
        self.start_batch_writer()

    def refresh_state(self):
        """Reload holdings and monitored assets from database and update cache"""
        session = SessionLocal()
        try:
            # Refresh Holdings
            records = session.query(Holding).filter(Holding.quantity > 0).all()

            with self.lock:
                self.holdings = {h.code: h for h in records}

                # Refresh Active Assets and update cache
                assets = session.query(AssetMonitor).filter(AssetMonitor.is_active == 1).all()
                self.active_assets = [a.code for a in assets]

                # Update AssetMonitor cache
                self.asset_cache = {asset.code: asset for asset in assets}

            logger.info(f"[RealTime] Monitoring {len(self.active_assets)} active assets, checking {len(self.holdings)} for ATR stop-loss. Cache size: {len(self.asset_cache)}")
        except Exception as e:
            logger.error(f"[RealTime] Failed to refresh state: {e}")
        finally:
            session.close()

    def start_batch_writer(self):
        """Start background thread for batch writing to database"""
        def batch_writer_thread():
            logger.info(f"[Batch Writer] Started with interval: {self.batch_interval}s")
            while not self.batch_write_event.is_set():
                try:
                    # Wait for batch interval or event
                    self.batch_write_event.wait(self.batch_interval)

                    # Flush pending updates
                    if self.pending_updates:
                        self.flush_updates_to_db()
                except Exception as e:
                    logger.error(f"[Batch Writer] Error: {e}")

        thread = threading.Thread(target=batch_writer_thread, daemon=True)
        thread.start()
        logger.info("[Batch Writer] Background thread started")

    def flush_updates_to_db(self):
        """Flush pending updates to database in batch"""
        if not self.pending_updates:
            return

        start_time = time.time()
        updates_to_process = {}

        # Copy pending updates under lock
        with self.lock:
            updates_to_process = self.pending_updates.copy()
            self.pending_updates.clear()

        if not updates_to_process:
            return

        session = SessionLocal()
        try:
            codes_to_update = list(updates_to_process.keys())
            # 1. Update AssetMonitor in batch
            assets = session.query(AssetMonitor).filter(
                AssetMonitor.code.in_(codes_to_update)
            ).all()
            asset_dict = {asset.code: asset for asset in assets}

            # 2. Update Holding in batch for active positions
            from database.models import Holding
            holdings_db = session.query(Holding).filter(
                Holding.code.in_(codes_to_update),
                Holding.quantity > 0
            ).all()
            holding_dict = {h.code: h for h in holdings_db}

            updated_count = 0
            for code, (price, timestamp) in updates_to_process.items():
                # Update AssetMonitor
                if code in asset_dict:
                    asset_dict[code].last_price = price
                    asset_dict[code].last_updated = timestamp
                    updated_count += 1
                
                # Update Holding real-time price
                if code in holding_dict:
                    holding_db_row = holding_dict[code]
                    holding_db_row.last_price = price
                    # Also sync highest_price from memory to DB
                    with self.lock:
                        if code in self.holdings:
                            holding_db_row.highest_price = self.holdings[code].highest_price

                # Update memory cache for monitor
                with self.lock:
                    if code in self.asset_cache:
                        self.asset_cache[code].last_price = price
                        self.asset_cache[code].last_updated = timestamp

            # Single commit for all updates
            session.commit()
            self.batch_write_count += 1
            self.last_batch_write = time.time()

            elapsed = time.time() - start_time
            logger.info(f"[Batch Writer] Flushed {updated_count} updates in {elapsed:.3f}s")

        except Exception as e:
            logger.error(f"[Batch Writer] Database error: {e}")
            session.rollback()
            # Put failed updates back
            with self.lock:
                self.pending_updates.update(updates_to_process)
        finally:
            session.close()

    def queue_price_update(self, code, price, timestamp):
        """Queue a price update for batch writing"""
        with self.lock:
            self.pending_updates[code] = (price, timestamp)
            self.update_count += 1

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super(ATRStopLossHandler, self).on_recv_rsp(rsp_pb)
        if ret_code == RET_OK:
            try:
                # data is a DataFrame with real-time quotes
                updates = []
                current_time = datetime.now()

                for _, row in data.iterrows():
                    code = row['code']
                    current_price = row['last_price']
                    updates.append(f"{code}: {current_price}")

                    # 1. Queue price update for batch writing (no immediate DB write)
                    self.queue_price_update(code, current_price, current_time)

                    # 2. Check if we hold this stock for Stop Loss / Take Profit
                    with self.lock:
                        if code in self.holdings:
                            holding = self.holdings[code]
                            avg_cost = holding.avg_cost

                            # Update real-time price and highest price in memory
                            holding.last_price = current_price
                            if current_price > holding.highest_price:
                                holding.highest_price = current_price
                            
                            # Calculate real-time profit/loss, safeguard against div by zero
                            if avg_cost <= 0:
                                continue
                                
                            profit_pct = (current_price - avg_cost) / avg_cost
                            
                            # 1. Hard Stop-Loss check (e.g. -8%)
                            if profit_pct <= -0.08:
                                logger.warning(f"🚨 [REAL-TIME STOP LOSS] {code} triggers hard stop loss! Cost: {avg_cost}, Price: {current_price}, P&L: {profit_pct*100:.2f}%")
                                # Apply 0.2% slippage to simulate limit-order execution instead of ideal market order
                                exec_price = current_price * 0.998
                                self._trigger_sell(code, exec_price, "Real-time Hard Stop Loss (-8%)")

                            # 2. Hard Take-Profit check (e.g. +15%)
                            elif profit_pct >= 0.15:
                                logger.info(f"💰 [REAL-TIME TAKE PROFIT] {code} triggers hard take profit! Cost: {avg_cost}, Price: {current_price}, P&L: {profit_pct*100:.2f}%")
                                exec_price = current_price * 0.998
                                self._trigger_sell(code, exec_price, "Real-time Hard Take Profit (+15%)")

                # Print a brief summary of received prices
                if updates:
                    logger.debug(f"实时报价更新 -> " + " | ".join(updates))

            except Exception as e:
                logger.error(f"[RealTime] Error processing quote update: {e}")
        else:
            logger.error(f"[RealTime] Quote subscription error: {data}")

        return RET_OK, data

    def _trigger_sell(self, code, price, reason):
        """Execute a mock sell for the real-time trigger"""
        from engine.trade_lock import GlobalTradeLock
        with GlobalTradeLock._lock:  # Acquire the global lock to prevent main bot from conflicting
            logger.info(f"[RealTime Exec] Executing SELL for {code} at {price} due to {reason}")
            from engine.executor import OrderExecutor
            from database.models import TradeAction, UserWallet, Holding
            session = SessionLocal()
            try:
                # Still use self.lock for internal state protection
                with self.lock:
                    if code not in self.holdings:
                        return
                    holding_mem = self.holdings[code]
                    qty = holding_mem.sellable_quantity
                    user_id = holding_mem.user_id
                    
                    if qty <= 0:
                        logger.warning(f"[RealTime Exec] Cannot sell {code}, sellable quantity is 0 (T+1 locked?)")
                        del self.holdings[code]
                        return
                        
                    executor = OrderExecutor(db_session=session, futu_client=None, simulate=True)
                    trade_record = executor.execute_trade(
                        user_id=user_id,
                        code=code,
                        action=TradeAction.SELL,
                        price=price,
                        quantity=qty,
                        reason=reason
                    )
                    
                    if trade_record:
                        # --- ATOMIC WALLET UPDATE ---
                        from sqlalchemy import update
                        stmt = update(UserWallet).where(
                            UserWallet.user_id == user_id,
                            UserWallet.market_type == holding_mem.market_type
                        ).values(balance=UserWallet.balance + (qty * price))
                        session.execute(stmt)
                        
                        holding_db = session.query(Holding).filter(Holding.id == holding_mem.id).first()
                        if holding_db:
                            holding_db.quantity -= qty
                            holding_db.sellable_quantity -= qty
                            if holding_db.quantity <= 0.001:
                                holding_db.quantity = 0.0
                                holding_db.avg_cost = 0.0
                                holding_db.sellable_quantity = 0.0
                                holding_db.tranches_count = 0
                            else:
                                holding_db.tranches_count = max(1, holding_db.tranches_count - 1)
                                
                        session.commit()
                        logger.info(f"[RealTime Exec] DB Updated. Sold {qty} shares of {code}.")
                    
                    del self.holdings[code]
            except Exception as e:
                session.rollback()
                logger.error(f"[RealTime Exec] DB Error: {e}")
            finally:
                session.close()

    def get_performance_stats(self):
        """Get performance statistics for monitoring"""
        with self.lock:
            return {
                'total_updates': self.update_count,
                'batch_writes': self.batch_write_count,
                'pending_updates': len(self.pending_updates),
                'cache_size': len(self.asset_cache),
                'holdings_monitored': len(self.holdings),
                'active_assets': len(self.active_assets)
            }


def start_realtime_monitor_thread(batch_interval=5.0):
    """
    Starts the independent high-frequency thread for real-time ATR stop-loss monitoring.
    Runs as a daemon thread alongside the scheduler.
    Args:
        batch_interval: Interval in seconds for batch writing to database (default: 5.0)
    """
    def monitor_thread():
        logger.info("[RealTime] Initializing Real-Time Market Monitor Thread with batch writing...")

        # Use the same host/port as defined in data/futu_client.py
        try:
            quote_ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            logger.info("[RealTime] Connected to FutuOpenD Quote Context for real-time monitoring.")
        except Exception as e:
            logger.error(f"[RealTime] Failed to connect to FutuOpenD: {e}")
            return

        # Register handler with batch writing support
        handler = ATRStopLossHandler(batch_interval=batch_interval)
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

                # Log performance stats periodically
                stats = handler.get_performance_stats()
                logger.info(f"[RealTime Stats] Updates: {stats['total_updates']}, "
                           f"Batch writes: {stats['batch_writes']}, "
                           f"Pending: {stats['pending_updates']}, "
                           f"Cache: {stats['cache_size']}")

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
            # Flush any remaining updates before shutdown
            logger.info("[RealTime] Flushing remaining updates...")
            handler.flush_updates_to_db()
            quote_ctx.close()

    # Start as daemon thread so it exits when main thread exits
    thread = threading.Thread(target=monitor_thread, daemon=True)
    thread.start()
    logger.info("[RealTime] Real-time monitor thread started successfully.")
    return thread

# ---------------------------------------------------------------------------
# API Server entry point
# ---------------------------------------------------------------------------
def start_api_server_thread():
    """Starts the FastAPI Web Interface in a background thread."""
    def run_server():
        import uvicorn
        from api_server import app
        logger.info("[API Server] Starting FastAPI on 0.0.0.0:8069")
        os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
        
        # Kill any existing process on port 8069 to prevent Address in Use error
        import subprocess
        try:
            output = subprocess.check_output("lsof -t -i:8069", shell=True)
            pids = output.decode("utf-8").strip().split()
            for pid in pids:
                if pid:
                    subprocess.run(f"kill -9 {pid}", shell=True)
                    logger.info(f"[API Server] Killed existing process {pid} holding port 8069")
        except subprocess.CalledProcessError:
            pass  # No process found

        try:
            uvicorn.run(app, host="0.0.0.0", port=8069, log_level="warning")
        except Exception as e:
            logger.error(f"[API Server] Error starting API Server: {e}")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread

# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def start_scheduler(batch_interval=5.0):
    """
    Start the trading scheduler with real-time monitoring.
    Args:
        batch_interval: Interval in seconds for batch writing to database (default: 5.0)
                      Lower values = more real-time but higher DB load
                      Higher values = better performance but slight delay
    """
    init_db()   # create any missing tables once at startup

    # Start real-time monitoring thread with batch writing
    logger.info("=" * 60)
    logger.info("Starting enhanced scheduler with real-time monitoring and batch writing...")
    logger.info(f"Batch write interval: {batch_interval}s")
    logger.info("=" * 60)
    realtime_thread = start_realtime_monitor_thread(batch_interval=batch_interval)
    
    # Start API
    api_thread = start_api_server_thread()

    # T+1 Rollover session: Before A/HK market opens (08:55)
    schedule.every().day.at("08:55").do(job_rollover_t1)

    # A-Share session: 09:30-11:30, 13:00-15:00
    # Staggered schedule to avoid conflicts with HK-Share
    # 10:00 - Intraday analysis (morning)
    # 11:20 - Pre-close preparation (morning)
    # 14:00 - Afternoon opening
    # 14:40 - Pre-close preparation (afternoon)
    schedule.every().day.at("10:00").do(job_a_share)
    schedule.every().day.at("11:20").do(job_a_share)
    schedule.every().day.at("14:00").do(job_a_share)
    schedule.every().day.at("14:40").do(job_a_share)

    # HK-Share session: 09:30-12:00, 13:00-16:00
    # Staggered schedule to avoid conflicts with A-Share
    # 10:30 - Intraday analysis (morning)
    # 11:30 - Pre-close preparation (morning)
    # 14:30 - Afternoon analysis
    # 15:30 - Pre-close preparation (afternoon)
    schedule.every().day.at("10:30").do(job_hk_share)
    schedule.every().day.at("11:30").do(job_hk_share)
    schedule.every().day.at("14:30").do(job_hk_share)
    schedule.every().day.at("15:30").do(job_hk_share)

    logger.info("=" * 60)
    logger.info("Scheduler configured:")
    logger.info("  - Real-time monitoring: ACTIVE (background thread)")
    logger.info("  - API Server: ACTIVE (127.0.0.1:8069 background thread)")
    logger.info("  - A-Share analysis: 10:00, 11:20, 14:00, 14:40")
    logger.info("  - HK-Share analysis: 10:30, 11:30, 14:30, 15:30")
    logger.info("  - T+1 rollover: 08:55")
    logger.info("  - Holiday checking: ENABLED (skips trading on holidays)")
    logger.info("  - Execution time monitoring: ENABLED")
    logger.info("=" * 60)
    logger.info("Press Ctrl+C to stop.")

    # Check command line arguments for force-run mode
    force_run = False
    if len(sys.argv) > 1 and sys.argv[1] == '--force':
        force_run = True

    # Run initial startup analysis
    if force_run:
        logger.info("Force-run mode enabled. Bypassing market open checks for initial analysis...")
        logger.info("Running initial A-Share analysis...")
        job_a_share(force=True)
        logger.info("Running initial HK-Share analysis...")
        job_hk_share(force=True)
    else:
        logger.info("Checking if market is open for initial startup analysis...")
        if is_market_open(MarketType.A_SHARE):
            logger.info("A-Share market is open. Running initial analysis...")
            job_a_share()
        else:
            logger.info("A-Share market is CLOSED. Skipping initial analysis.")

        if is_market_open(MarketType.HK_SHARE):
            logger.info("HK-Share market is open. Running initial analysis...")
            job_hk_share()
        else:
            logger.info("HK-Share market is CLOSED. Skipping initial analysis.")

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
