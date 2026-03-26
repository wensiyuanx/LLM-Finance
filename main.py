import os
import logging
import warnings
from datetime import datetime, timedelta
from database.db import SessionLocal, AsyncSessionLocal, init_db

# Suppress matplotlib font warnings
warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib.font_manager')
from database.models import KLineData, TradeAction, SignalRecord, UserWallet, Holding, MarketType, AssetMonitor
from data.futu_client import FutuClient
from strategy.logic import generate_signals, generate_grid_trend_signals
from engine.executor import OrderExecutor
from scripts.visualizer import generate_kline_chart
from engine.time_utils import is_market_open
from engine.regime import RegimeDetector
from engine.ml_predictor import ml_predictor
import pandas as pd
import asyncio
from typing import Dict, List
from config import get_config, refresh_config
from sqlalchemy import select, delete, func

# Fix for FuTu API logger path permission issues on macOS / Linux
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

logger = logging.getLogger(__name__)

config = get_config()

# ---------------------------------------------------------------------------
# Data-window constants
# ---------------------------------------------------------------------------
# 200-day SMA needs 200 trading days (~280 calendar days).
# 60-day LSTM window adds another 60 trading days (~85 calendar days).
# We pull 550 calendar days to guarantee enough trading days after weekends
# and holidays, with a comfortable buffer.
DATA_WINDOW_DAYS = config["global"]["data_window_days"]
INCREMENTAL_OVERLAP = config["global"]["incremental_fetch_overlap_days"]

# Fraction of available wallet balance used per BUY order.
# 0.25 = 25% max position per signal — basic fixed-fraction position sizing.
POSITION_SIZE_FRAC = 0.25


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def format_futu_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise Futu K-line dataframe index to DatetimeIndex."""
    if df is not None and 'time_key' in df.columns:
        df['time_key'] = pd.to_datetime(df['time_key'])
        df.set_index('time_key', inplace=True)
    return df


async def save_klines_to_db(db, user_id: int, code: str, df: pd.DataFrame, timeframe: str = '1d'):
    """Bulk upsert K-line rows: one query to find existing timestamps, then batch insert new ones."""
    if df is None or df.empty:
        return
    has_turnover = 'turnover' in df.columns
    timestamps = df.index.tolist()
    
    # Query existing timestamps
    stmt = select(KLineData.time_key).filter(
        KLineData.user_id == user_id,
        KLineData.code == code,
        KLineData.timeframe == timeframe,
        KLineData.time_key.in_(timestamps)
    )
    result = await db.execute(stmt)
    existing = {r[0] for r in result.all()}
    
    new_rows = []
    for index, row in df.iterrows():
        if index not in existing:
            turnover = (float(row['turnover']) if has_turnover
                        else float(row['volume']) * float((row['high'] + row['low']) / 2))
            new_rows.append(KLineData(
                user_id=user_id, code=code, time_key=index, timeframe=timeframe,
                open_price=row['open'], close_price=row['close'],
                high_price=row['high'], low_price=row['low'],
                volume=row['volume'], turnover=turnover
            ))
    if new_rows:
        db.add_all(new_rows)
        await db.commit()
        logger.debug("Saved %d new %s K-line rows for %s", len(new_rows), timeframe, code)

    # --- Sliding Window Pruning ---
    try:
        max_records = 5000 if timeframe == '60m' else 1500
        # Find the threshold timestamp
        recent_klines_stmt = (
            select(KLineData.time_key)
            .filter(KLineData.user_id == user_id, KLineData.code == code, KLineData.timeframe == timeframe)
            .order_by(KLineData.time_key.desc())
            .offset(max_records - 1)
            .limit(1)
        )
        recent_klines_res = await db.execute(recent_klines_stmt)
        recent_klines = recent_klines_res.first()

        if recent_klines:
            threshold_time = recent_klines[0]
            # Delete any record older than the threshold record
            del_stmt = delete(KLineData).filter(
                KLineData.user_id == user_id,
                KLineData.code == code,
                KLineData.timeframe == timeframe,
                KLineData.time_key < threshold_time
            )
            await db.execute(del_stmt)
            await db.commit()
            logger.debug(f"Pruned {timeframe} K-line data for {code} (kept latest {max_records})")
    except Exception as e:
        await db.rollback()
        logger.error("Error during %s K-line pruning for %s: %s", timeframe, code, e)


async def save_signal_to_db(db, user_id: int, code: str, action, reason: str, close_price: float, current_price: float = None):
    db.add(SignalRecord(
        user_id=user_id, code=code,
        action=action, reason=reason, close_price=close_price, current_price=current_price
    ))
    await db.commit()


async def get_wallet(db, user_id: int, market_type):
    stmt = select(UserWallet).filter(
        UserWallet.user_id == user_id,
        UserWallet.market_type == market_type
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_wallet(db, user_id: int, market_type, delta: float, realized_pnl: float = 0.0):
    """Atomic wallet update to prevent race conditions."""
    from sqlalchemy import update
    stmt = update(UserWallet).where(
        UserWallet.user_id == user_id,
        UserWallet.market_type == market_type
    ).values(
        balance=UserWallet.balance + delta,
        total_pnl=UserWallet.total_pnl + realized_pnl
    )
    await db.execute(stmt)
    # commit is handled by the caller to ensure atomicity


async def get_holding(db, user_id: int, code: str, market_type) -> Holding:
    """Return the persistent Holding row; create a flat one if it doesn't exist yet."""
    stmt = select(Holding).filter(
        Holding.user_id == user_id,
        Holding.code == code
    )
    result = await db.execute(stmt)
    holding = result.scalar_one_or_none()
    
    if holding is None:
        holding = Holding(
            user_id=user_id, code=code,
            quantity=0.0, avg_cost=0.0,
            market_type=market_type
        )
        db.add(holding)
        await db.commit()
        await db.refresh(holding)
    return holding


async def load_klines_from_db(db, user_id: int, code: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """Load latest K-line data from DB for indicator calculation."""
    stmt = select(KLineData).filter(
        KLineData.user_id == user_id,
        KLineData.code == code,
        KLineData.timeframe == timeframe
    ).order_by(KLineData.time_key.desc()).limit(limit)
    
    res = await db.execute(stmt)
    records = res.scalars().all()
    
    if not records:
        return pd.DataFrame()
        
    # Reverse to chronological order
    records = list(reversed(records))
    
    data = []
    for r in records:
        data.append({
            'time_key': r.time_key,
            'open': r.open_price,
            'high': r.high_price,
            'low': r.low_price,
            'close': r.close_price,
            'volume': r.volume,
            'turnover': r.turnover
        })
    df = pd.DataFrame(data)
    df.set_index('time_key', inplace=True)
    return df


async def update_holding_buy(db, holding: Holding, qty: float, price: float, is_t1: bool = True, is_trend: bool = False):
    """Weighted-average cost update on buy. T+1 means bought shares are NOT sellable today."""
    total_cost = (holding.quantity * holding.avg_cost) + (qty * price)
    holding.quantity = round(holding.quantity + qty, 6)
    holding.avg_cost = round(total_cost / holding.quantity, 6)
    if not is_t1:
        holding.sellable_quantity = round(holding.sellable_quantity + qty, 6)
    
    if is_trend:
        holding.is_trend = 1
    
    holding.highest_price = max(holding.highest_price, price)
    holding.tranches_count += 1
    # commit is handled by the caller to ensure atomicity


async def update_holding_sell(db, holding: Holding, qty: float):
    """Reduce position on sell."""
    holding.quantity = round(holding.quantity - qty, 6)
    holding.sellable_quantity = round(holding.sellable_quantity - qty, 6)
    if holding.quantity <= 0.001:
        holding.quantity = 0.0
        holding.avg_cost = 0.0
        holding.sellable_quantity = 0.0
        holding.tranches_count = 0
        holding.is_trend = 0
    else:
        holding.tranches_count = max(1, holding.tranches_count - 1)
    # commit is handled by the caller to ensure atomicity


async def rollover_t1_holdings(db_session, user_id=None):
    """Called at start of day: all quantity becomes sellable_quantity."""
    stmt = select(Holding)
    if user_id:
        stmt = stmt.filter(Holding.user_id == user_id)
    result = await db_session.execute(stmt)
    holdings = result.scalars().all()
    for h in holdings:
        h.sellable_quantity = h.quantity
    await db_session.commit()



# ---------------------------------------------------------------------------
# Main bot
# ---------------------------------------------------------------------------


from engine.portfolio import PortfolioManager

class StrategyRouter:
    """Registry pattern for routing assets to appropriate strategies"""
    
    @staticmethod
    def get_strategy_signals(asset, klines_60m, klines_day, holding, is_pre_close):
        code = asset.code
        is_etf_asset = getattr(asset, 'is_etf', False)
        is_leveraged = getattr(asset, 'is_leveraged', False)
        
        if is_etf_asset:
            if is_leveraged:
                logger.info(f"[{code}] Routing to Leveraged ETF Momentum Strategy")
                from strategy.lev_etf_logic import generate_leveraged_etf_signals
                return generate_leveraged_etf_signals(
                    klines_60m, df_day=klines_day, current_position=holding.quantity,
                    avg_cost=holding.avg_cost, highest_price=holding.highest_price,
                    tranches_count=holding.tranches_count, last_buy_price=holding.avg_cost,
                    is_pre_close=is_pre_close
                )
            else:
                logger.info(f"[{code}] Routing to Aggressive ETF Strategy (Grid+Trend)")
                from strategy.logic import generate_grid_trend_signals
                return generate_grid_trend_signals(
                    klines_60m, current_position=holding.quantity,
                    avg_cost=holding.avg_cost, tranches_count=holding.tranches_count,
                    is_trend_position=bool(holding.is_trend),
                    highest_price=holding.highest_price,
                    is_pre_close=is_pre_close
                )
        else:
            logger.info(f"[{code}] Routing to Standard Stock Strategy (MTF)")
            from strategy.logic import generate_signals
            return generate_signals(
                klines_60m, df_day=klines_day, current_position=holding.quantity,
                avg_cost=holding.avg_cost, code=code,
                is_trend_position=bool(holding.is_trend),
                highest_price=holding.highest_price,
                is_pre_close=is_pre_close
            )

import time

class RateLimiter:
    """Token bucket rate limiter for FutuOpenD API limits (e.g., 60 requests / 30 seconds)"""
    def __init__(self, rate: int, per: float):
        self.rate = rate
        self.per = per
        self.allowance = float(rate)
        self.last_check = time.monotonic()
        self._locks = {}

    async def wait(self):
        loop = asyncio.get_running_loop()
        if loop not in self._locks:
            self._locks[loop] = asyncio.Lock()
            
        async with self._locks[loop]:
            while True:
                current = time.monotonic()
                elapsed = current - self.last_check
                self.last_check = current
                self.allowance += elapsed * (self.rate / self.per)
                
                if self.allowance > self.rate:
                    self.allowance = self.rate
                    
                if self.allowance >= 1.0:
                    self.allowance -= 1.0
                    return
                    
                # Wait until at least 1 token is generated
                wait_time = (1.0 - self.allowance) * (self.per / self.rate)
                await asyncio.sleep(wait_time)

# Futu API Rate Limit: 60 requests / 30 seconds
# We use a strict 1 request per 0.6 seconds to completely eliminate bursting and prevent hitting the rate limit even after script restarts.
futu_rate_limiter = RateLimiter(rate=1, per=0.6)

async def _rate_limited_fetch(futu, code, start_date, end_date, ktype):
    """Wrapper to apply rate limiting before the API call"""
    await futu_rate_limiter.wait()
    
    # Add a retry mechanism specifically for rate limits
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return await asyncio.to_thread(futu.get_historical_klines, code, start_date=start_date, end_date=end_date, ktype=ktype)
        except Exception as e:
            if "获取历史K线频率太高" in str(e) or "30秒最多60次" in str(e):
                if attempt < max_retries - 1:
                    logger.warning(f"Rate limit hit for {code}, sleeping 10s and retrying...")
                    await asyncio.sleep(10)
                    continue
            raise e

async def fetch_and_compute(futu, asset, start_date_fetch, end_date, api_semaphore):
    """Async K-line fetching and indicator calculation."""
    df_day = None
    df_60m = None
    if futu:
        from futu import KLType
        
        async with api_semaphore:
            # Concurrent fetch for both timeframes.
            df_day, df_60m = await asyncio.gather(
                _rate_limited_fetch(futu, asset.code, start_date_fetch, end_date, KLType.K_DAY),
                _rate_limited_fetch(futu, asset.code, start_date_fetch, end_date, KLType.K_60M)
            )
        
        if df_day is not None:
            df_day = format_futu_df(df_day)
        if df_60m is not None:
            df_60m = format_futu_df(df_60m)
            
    return asset.code, df_day, df_60m

async def rollover_t1_holdings_task():
    """Independent task to rollover T+1 holdings in the morning."""
    async with AsyncSessionLocal() as session:
        try:
            logger.info("Executing morning T+1 holdings rollover...")
            await rollover_t1_holdings(session)
        finally:
            await session.close()


async def run_trading_bot(market_filter=None, force=False):
    """
    Main asynchronous trading bot loop.
    """
    init_db()
    # Hot-reload config at the start of each run
    config = refresh_config()
    logger.info("Trading bot started%s (Config Hot-Reloaded).",
                f" | filter: {[m.value for m in market_filter]}" if market_filter else "")

    futu = FutuClient()
    futu_connected = await asyncio.to_thread(futu.connect)
    if not futu_connected:
        logger.error("Futu OpenAPI not connected. K-Line fetching will fail.")

    async with AsyncSessionLocal() as db_session:
        executor = OrderExecutor(db_session=db_session, futu_client=futu, simulate=True)

        try:
            # Constants from hot-reloaded config
            DATA_WINDOW_DAYS = config["global"]["data_window_days"]
            INCREMENTAL_OVERLAP = config["global"]["incremental_fetch_overlap_days"]
            POSITION_SIZE_FRAC = config["strategies"].get("global_max_pos", 0.25)
            
            start_date = (datetime.now() - timedelta(days=DATA_WINDOW_DAYS)).strftime("%Y-%m-%d")
            end_date   = datetime.now().strftime("%Y-%m-%d")

            stmt = select(AssetMonitor).filter(AssetMonitor.is_active == 1)
            if market_filter:
                stmt = stmt.filter(AssetMonitor.market_type.in_(market_filter))
            result = await db_session.execute(stmt)
            active_assets = result.scalars().all()
            
            if not active_assets:
                logger.warning("No active A/HK assets found for this run.")
                return

            from collections import defaultdict
            user_market_assets = defaultdict(list)
            for asset in active_assets:
                user_market_assets[(asset.user_id, asset.market_type)].append(asset)
                
            for (user_id, market_type), assets in user_market_assets.items():
                wallet = await get_wallet(db_session, user_id, market_type)
                if not wallet:
                    logger.warning(f"No wallet found for User {user_id} - {market_type.name}. Skipping {len(assets)} assets.")
                    continue
                    
                stmt_h = select(Holding).filter(
                    Holding.user_id == user_id, 
                    Holding.market_type == market_type
                )
                res_h = await db_session.execute(stmt_h)
                holdings = res_h.scalars().all()
                holdings_value = sum(h.quantity * h.avg_cost for h in holdings)
                total_value = wallet.balance + holdings_value
                
                # Global exposure limit: 90% (Keep 10% Cash Reserve)
                max_exposure = 0.90
                logger.info("Portfolio Status | Value: %.2f | Cash: %.2f | Exposure: %.1f%% | Limit: %.1f%%",
                            total_value, wallet.balance, (holdings_value/total_value if total_value > 0 else 0)*100, max_exposure*100)
                
                # Initialize PortfolioManager with config directly
                pm = await PortfolioManager.create(db_session, market_type.name, config)
                
                # Update total_assets in wallet after PortfolioManager loaded latest state
                wallet.total_assets = pm.total_value
                await db_session.commit()
                
                # Fetch Market Regime ONCE per market type
                await futu_rate_limiter.wait()
                market_regime = await asyncio.to_thread(RegimeDetector.get_market_regime, market_type.name, futu)
                
                signals_context = []
                asset_stats = {} 
                
                logger.info(f"Fetching data concurrently for {len(assets)} assets...")
                
                # Create a semaphore bound to the current event loop
                current_api_semaphore = asyncio.Semaphore(5)
                
                # Prepare concurrent fetch tasks
                fetch_coroutines = []
                for a in assets:
                    latest_db_time_stmt = select(func.max(KLineData.time_key)).filter(
                        KLineData.code == a.code, KLineData.timeframe == '60m'
                    )
                    latest_db_time_res = await db_session.execute(latest_db_time_stmt)
                    latest_db_time = latest_db_time_res.scalar()

                    if latest_db_time:
                        latest_dt = pd.to_datetime(latest_db_time)
                        start_date_fetch = (latest_dt - timedelta(days=INCREMENTAL_OVERLAP)).strftime("%Y-%m-%d")
                    else:
                        start_date_fetch = start_date
                    
                    fetch_coroutines.append(fetch_and_compute(futu, a, start_date_fetch, end_date, current_api_semaphore))
                    
                # Execute concurrently
                fetch_results = await asyncio.gather(*fetch_coroutines, return_exceptions=True)
                code_to_df = {}
                for res in fetch_results:
                    if isinstance(res, Exception):
                        logger.error(f"Concurrent fetch task failed: {res}")
                        continue
                    res_code, res_df_day, res_df_60m = res
                    code_to_df[res_code] = {'1d': res_df_day, '60m': res_df_60m}

                for asset in assets:
                    code = asset.code
                    data_dict = code_to_df.get(code)
                    if not data_dict: continue
                        
                    klines_day = data_dict.get('1d')
                    klines_60m = data_dict.get('60m')

                    # Save data to DB (Async) - only if we fetched new data
                    if klines_day is not None and not klines_day.empty:
                        await save_klines_to_db(db_session, user_id, code, klines_day, timeframe='1d')
                    if klines_60m is not None and not klines_60m.empty:
                        await save_klines_to_db(db_session, user_id, code, klines_60m, timeframe='60m')

                    # Load full window from DB to calculate indicators properly
                    from strategy.indicators import calculate_indicators
                    full_klines_day = await load_klines_from_db(db_session, user_id, code, '1d', limit=250)
                    full_klines_60m = await load_klines_from_db(db_session, user_id, code, '60m', limit=300)
                    
                    if full_klines_day.empty or full_klines_60m.empty:
                        logger.warning(f"No sufficient historical data in DB for {code}.")
                        continue
                        
                    # Calculate indicators
                    klines_60m_with_ind = calculate_indicators(full_klines_60m)
                    klines_day_with_ind = calculate_indicators(full_klines_day)

                    try:
                        await asyncio.to_thread(generate_kline_chart, klines_60m_with_ind.copy(), code)
                    except Exception as viz_e:
                        logger.error(f"Failed to generate chart for {code}: {viz_e}")

                    # Load persistent holding
                    holding = await get_holding(db_session, user_id, code, market_type)
                    current_price = float(klines_60m_with_ind['close'].iloc[-1])
                    
                    if holding.quantity > 0:
                        holding.last_price = current_price # Update current price for UI/analysis
                        if holding.highest_price == 0:
                            if current_price > holding.avg_cost:
                                holding.highest_price = current_price
                        elif current_price > holding.highest_price:
                            holding.highest_price = current_price
                        await db_session.commit()

                    now_time = datetime.now()
                    is_pre_close = False
                    if market_type == MarketType.A_SHARE and now_time.hour == 14 and now_time.minute >= 40:
                        is_pre_close = True
                    elif market_type == MarketType.HK_SHARE and now_time.hour == 15 and now_time.minute >= 40:
                        is_pre_close = True
                        
                    # 1. Detect Market Regime
                    # Regime is now fetched once per market type before the loop
                    
                    # 2. Get ML Prediction Probability
                    ml_prob = 0.5
                    try:
                        # Periodically retrain model if needed, but for now just predict
                        # If model doesn't exist, we could train it here, but training in the main loop might be slow.
                        # For now, we rely on the predictor. It will return 0.5 if no model is found.
                        ml_prob = ml_predictor.predict_prob(klines_60m_with_ind, code)
                    except Exception as e:
                        logger.warning(f"[{code}] ML Prediction failed: {e}")

                    action, reason, score, is_trend_entry = StrategyRouter.get_strategy_signals(
                        asset, klines_60m_with_ind, klines_day_with_ind, holding, is_pre_close
                    )
                    
                    # 3. Adjust Score and Action based on ML and Regime
                    if action == TradeAction.BUY:
                        # Regime Filter
                        if market_regime == "STRONG_BEAR":
                            logger.info(f"[{code}] Market is in STRONG_BEAR. Suppressing BUY signal.")
                            action = TradeAction.HOLD
                            reason = "大盘主跌浪，系统风控拦截买入"
                        else:
                            # Enhance score with ML prediction
                            # ml_prob > 0.5 means positive expectation
                            ml_score_boost = (ml_prob - 0.5) * 100
                            score += ml_score_boost
                            reason += f" [ML胜率: {ml_prob:.2f}]"
                            
                            # If ML strongly predicts a drop, veto the buy
                            if ml_prob < 0.35:
                                logger.info(f"[{code}] ML predicts high drop probability ({ml_prob:.2f}). Suppressing BUY.")
                                action = TradeAction.HOLD
                                reason = f"AI预测下跌概率高，拦截买入 (胜率 {ml_prob:.2f})"
                                
                    elif action == TradeAction.SELL and market_regime == "STRONG_BULL":
                        # In a strong bull market, relax take-profit criteria slightly ( handled within strategies usually, but we log it here)
                        reason += " [强势牛市护航]"
                    
                    # Handle Trend Promotion (Signal returns HOLD but is_trend_entry is True)
                    if action == TradeAction.HOLD and is_trend_entry and holding.quantity > 0 and holding.is_trend == 0:
                        logger.info(f"[{code}] Promoting position to TREND mode: {reason}")
                        holding.is_trend = 1
                        await db_session.commit()
                        continue # Already processed the promotion, no actual order needed

                    latest_close = float(klines_60m_with_ind['close'].iloc[-1])
                    current_asset_price = None
                    try:
                        if getattr(asset, "last_price", None) is not None:
                            current_asset_price = float(asset.last_price)
                    except Exception:
                        current_asset_price = None
                    if current_asset_price is None:
                        current_asset_price = latest_close
                    logger.info("[%s] Signal: %s — %s (Sellable: %s, Score: %.1f)", code, action.name, reason, holding.sellable_quantity, score)
                    await save_signal_to_db(db_session, user_id, code, action, reason, latest_close, current_price=current_asset_price)
                    
                    asset_stats[code] = {'holding': holding}
                    
                    if market_type == MarketType.A_SHARE and action == TradeAction.BUY and now_time.hour < 14 and not force:
                        logger.warning("[%s] Suppressed BUY signal due to A-Share morning T+1 risk.", code)
                        continue

                    if action in [TradeAction.BUY, TradeAction.SELL]:
                        is_etf_asset = bool(getattr(asset, 'is_etf', False))
                        signals_context.append({
                            'code': code, 'market_type': market_type, 'action': action,
                            'price': latest_close, 'sellable_qty': holding.sellable_quantity,
                            'reason': reason, 'score': score, 'is_trend_entry': is_trend_entry,
                            'is_etf': is_etf_asset, 'tranches_count': holding.tranches_count,
                            'current_holding_val': holding.quantity * latest_close,
                            'board_lot': getattr(asset, 'board_lot', 100)
                        })

                # Portfolio Manager resolution
                if not signals_context:
                    logger.info("本轮无交易信号触发。")

                # Evaluate signals OUTSIDE the execution lock to minimize lock holding time
                # Portfolio Manager load state needs its own brief protection if necessary,
                # but evaluation itself is just math.
                await pm._load_account_state()
                for ctx in signals_context:
                    h = await get_holding(db_session, user_id, ctx['code'], market_type)
                    await db_session.refresh(h)
                    ctx['sellable_qty'] = h.sellable_quantity
                    ctx['tranches_count'] = h.tranches_count
                    ctx['current_holding_val'] = h.quantity * ctx['price']
                    asset_stats[ctx['code']]['holding'] = h

                executable_orders = await pm.evaluate_signals(signals_context)
                
                # Execute evaluated orders
                for order in executable_orders:
                    code = order['code']
                    action = order['action']
                    qty = order['quantity']
                    reason = order['reason']
                    holding = asset_stats[code]['holding']
                    is_t1 = (market_type == MarketType.A_SHARE)
                    
                    if not is_market_open(market_type) and not force:
                        logger.warning("[%s] Market is CLOSED. Skipping Execution.", code)
                        continue

                    # --- Dynamic Order Book Pricing ---
                    # Fetch real-time Ask 1 / Bid 1 instead of relying on stale K-line close price
                    quote = futu.get_realtime_quote(code)
                    execution_price = order['price'] # Fallback to K-line price
                    
                    if quote:
                        try:
                            if action == TradeAction.BUY:
                                ask_price = float(quote.get('ask_price', 0))
                                if ask_price > 0:
                                    # Use Ask 1 for buying, add slight slippage buffer
                                    execution_price = ask_price * 1.001
                                    logger.info(f"[{code}] OrderBook Pricing: Using Ask 1 ({ask_price}) for BUY")
                                else:
                                    execution_price = order['price'] * 1.002
                                    
                            elif action == TradeAction.SELL:
                                bid_price = float(quote.get('bid_price', 0))
                                if bid_price > 0:
                                    # Use Bid 1 for selling, subtract slight slippage buffer
                                    execution_price = bid_price * 0.999
                                    logger.info(f"[{code}] OrderBook Pricing: Using Bid 1 ({bid_price}) for SELL")
                                else:
                                    execution_price = order['price'] * 0.998
                        except Exception as e:
                            logger.error(f"[{code}] Failed to parse realtime quote for pricing: {e}")
                            execution_price = order['price'] * (1.002 if action == TradeAction.BUY else 0.998)
                    else:
                        # Fallback if snapshot fails
                        execution_price = order['price'] * (1.002 if action == TradeAction.BUY else 0.998)

                    from engine.trade_lock import GlobalTradeLock
                    with GlobalTradeLock._lock: # Protect ONLY the actual DB/Execution part
                        if action == TradeAction.SELL:
                            trade_record = await executor.execute_trade(user_id, code, action, price=execution_price, quantity=qty, reason=reason, avg_cost=holding.avg_cost)
                            if trade_record:
                                await update_wallet(db_session, user_id, market_type, qty * execution_price, realized_pnl=trade_record.realized_pnl)
                                await update_holding_sell(db_session, holding, qty)
                                await db_session.commit()
                                logger.info(f"已卖出 {qty} 股 {code} @ {execution_price:.3f}, 实现盈亏: {trade_record.realized_pnl:.2f}")
                                
                        elif action == TradeAction.BUY:
                            trade_record = await executor.execute_trade(user_id, code, action, price=execution_price, quantity=qty, reason=reason)
                            if trade_record:
                                await update_wallet(db_session, user_id, market_type, -(qty * execution_price))
                                await update_holding_buy(db_session, holding, qty, execution_price, is_t1=is_t1, is_trend=order.get('is_trend_entry', False))
                                await db_session.commit()
                                logger.info(f"已买入 {qty} 股 {code} @ {execution_price:.3f}")

        except Exception as e:
            await db_session.rollback()
            logger.error("Trading bot error: %s", e, exc_info=True)
        finally:
            futu.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s")
    asyncio.run(run_trading_bot())
