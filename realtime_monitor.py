import os
import time
import logging
import threading
from datetime import datetime

# Fix for FuTu API requiring HOME environment variable
if 'HOME' not in os.environ:
    os.environ['HOME'] = os.getcwd()

from futu import *
from database.db import SessionLocal
from database.models import Holding, AssetMonitor

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class ATRStopLossHandler(StockQuoteHandlerBase):
    """
    Handler for real-time tick/quote updates from FutuOpenD.
    1. Updates real-time prices to AssetMonitor table.
    2. Checks incoming price against dynamic ATR stop-loss levels for held positions.
    """
    def __init__(self, db_session_unused):
        super().__init__()
        # We no longer save the session passed in the init because 
        # this handler runs in async background threads.
        self.lock = threading.RLock() # Protect shared resources from concurrent access
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

def start_realtime_monitor(codes_to_monitor=None):
    """
    Starts the independent high-frequency thread for real-time ATR stop-loss monitoring.
    """
    logger.info("Initializing Real-Time Market Monitor...")
    
    # Use the same host/port as defined in data/futu_client.py
    # Since we are in mock mode, ensure FutuOpenD is running
    try:
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        logger.info("Connected to FutuOpenD Quote Context for real-time monitoring.")
    except Exception as e:
        logger.error(f"Failed to connect to FutuOpenD: {e}")
        return

    # Setup database session for initial load (not used in async callbacks anymore)
    session = SessionLocal()
    
    # Register handler
    handler = ATRStopLossHandler(session)
    quote_ctx.set_handler(handler)
    
    # Start asynchronous push
    quote_ctx.start()
    
    # Get active assets to monitor (both holdings and watchlist)
    codes = handler.active_assets
    if codes_to_monitor:
        codes.extend(codes_to_monitor)
        codes = list(set(codes)) # deduplicate
        
    if not codes:
        logger.info("No active assets to monitor. Still running in background...")
    else:
        logger.info(f"Subscribing to real-time quotes for: {codes}")
        ret, data = quote_ctx.subscribe(codes, [SubType.QUOTE], subscribe_push=True)
        if ret != RET_OK:
            logger.error(f"Failed to subscribe to quotes: {data}")

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
                    logger.info(f"Updated real-time subscription list: {codes}")
                
    except KeyboardInterrupt:
        logger.info("Stopping real-time monitor...")
    finally:
        quote_ctx.close()
        session.close()

if __name__ == "__main__":
    start_realtime_monitor()
