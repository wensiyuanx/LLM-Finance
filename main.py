import os
import logging
import warnings
from datetime import datetime, timedelta
from database.db import SessionLocal, init_db

# Suppress matplotlib font warnings
warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib.font_manager')
from database.models import KLineData, TradeAction, SignalRecord, UserWallet, Holding, MarketType, AssetMonitor
from data.futu_client import FutuClient
from strategy.logic import generate_signals, generate_grid_trend_signals
from engine.executor import OrderExecutor
from scripts.visualizer import generate_kline_chart
from engine.time_utils import is_market_open
import pandas as pd
import asyncio
from typing import Dict, List
from config import get_config, refresh_config
from sqlalchemy import select, delete, func

# Fix for FuTu API requiring HOME environment variable
if 'HOME' not in os.environ:
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


async def save_signal_to_db(db, user_id: int, code: str, action, reason: str, close_price: float):
    db.add(SignalRecord(
        user_id=user_id, code=code,
        action=action, reason=reason, close_price=close_price
    ))
    await db.commit()


async def get_wallet(db, user_id: int, market_type):
    stmt = select(UserWallet).filter(
        UserWallet.user_id == user_id,
        UserWallet.market_type == market_type
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_wallet(db, wallet, delta: float):
    wallet.balance = round(wallet.balance + delta, 4)
    await db.commit()


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
    await db.commit()


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
    await db.commit()


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

async def fetch_and_compute(futu, asset, start_date_fetch, end_date):
    """Async K-line fetching and indicator calculation."""
    df_day = None
    df_60m = None
    if futu:
        from futu import KLType
        from strategy.indicators import calculate_indicators
        
        # Concurrent fetch for both timeframes
        df_day, df_60m = await asyncio.gather(
            asyncio.to_thread(futu.get_historical_klines, asset.code, start_date=start_date_fetch, end_date=end_date, ktype=KLType.K_DAY),
            asyncio.to_thread(futu.get_historical_klines, asset.code, start_date=start_date_fetch, end_date=end_date, ktype=KLType.K_60M)
        )
        
        if df_day is not None:
            df_day = format_futu_df(df_day)
        if df_60m is not None:
            df_60m = format_futu_df(df_60m)
            
        if df_day is not None and not df_day.empty and df_60m is not None and not df_60m.empty:
            # Pre-calculate indicators
            df_60m = calculate_indicators(df_60m)
            df_day = calculate_indicators(df_day)
    
    return asset.code, df_day, df_60m

async def rollover_t1_holdings_task():
    """Independent task to rollover T+1 holdings in the morning."""
    async with AsyncSessionLocal() as session:
        try:
            logger.info("Executing morning T+1 holdings rollover...")
            await rollover_t1_holdings(session)
        finally:
            await session.close()


async def run_trading_bot(market_filter=None):
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
                
                pm = PortfolioManager(
                    db_session=db_session, 
                    current_cash=wallet.balance, 
                    total_value=total_value, 
                    max_position_pct=POSITION_SIZE_FRAC,
                    max_total_exposure_pct=max_exposure
                )
                signals_context = []
                asset_stats = {} 
                
                logger.info(f"Fetching data concurrently for {len(assets)} assets...")
                
                # Prepare fetch tasks
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
                    
                    fetch_coroutines.append(fetch_and_compute(futu, a, start_date_fetch, end_date))
                    
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

                    if klines_day is None or klines_day.empty or klines_60m is None or klines_60m.empty:
                        logger.warning(f"No complete K-line data fetched for {code}.")
                        continue
                    
                    # Save data to DB (Async)
                    await save_klines_to_db(db_session, user_id, code, klines_day, timeframe='1d')
                    await save_klines_to_db(db_session, user_id, code, klines_60m, timeframe='60m')

                    try:
                        await asyncio.to_thread(generate_kline_chart, klines_60m.copy(), code)
                    except Exception as viz_e:
                        logger.error(f"Failed to generate chart for {code}: {viz_e}")

                    # Load persistent holding
                    holding = await get_holding(db_session, user_id, code, market_type)
                    current_price = float(klines_60m['close'].iloc[-1])
                    
                    if holding.quantity > 0:
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

                    action, reason, score, is_trend_entry = StrategyRouter.get_strategy_signals(
                        asset, klines_60m, klines_day, holding, is_pre_close
                    )
                    latest_close = float(klines_60m['close'].iloc[-1])
                    logger.info("[%s] Signal: %s — %s (Sellable: %s, Score: %.1f)", code, action.name, reason, holding.sellable_quantity, score)
                    await save_signal_to_db(db_session, user_id, code, action, reason, latest_close)
                    
                    asset_stats[code] = {'holding': holding}
                    
                    if market_type == MarketType.A_SHARE and action == TradeAction.BUY and now_time.hour < 14:
                        logger.warning("[%s] Suppressed BUY signal due to A-Share morning T+1 risk.", code)
                        continue

                    if action in [TradeAction.BUY, TradeAction.SELL]:
                        is_etf_asset = bool(getattr(asset, 'is_etf', False))
                        signals_context.append({
                            'code': code, 'market_type': market_type, 'action': action,
                            'price': latest_close, 'sellable_qty': holding.sellable_quantity,
                            'reason': reason, 'score': score, 'is_trend_entry': is_trend_entry,
                            'is_etf': is_etf_asset, 'tranches_count': holding.tranches_count,
                            'current_holding_val': holding.quantity * latest_close
                        })

                # Portfolio Manager resolution
                executable_orders = pm.evaluate_signals(signals_context)
                
                # Execute evaluated orders
                for order in executable_orders:
                    code = order['code']
                    action = order['action']
                    qty = order['quantity']
                    price = order['price'] * 1.002 if action == TradeAction.BUY else order['price'] * 0.998
                    reason = order['reason']
                    holding = asset_stats[code]['holding']
                    is_t1 = (market_type == MarketType.A_SHARE)
                    
                    if not is_market_open(market_type):
                        logger.warning("[%s] Market is CLOSED. Skipping Execution.", code)
                        continue

                    if action == TradeAction.SELL:
                        trade_record = await asyncio.to_thread(executor.execute_trade, user_id, code, action, price=price, quantity=qty, reason=reason)
                        if trade_record:
                            await update_wallet(db_session, wallet, qty * price)
                            await update_holding_sell(db_session, holding, qty)
                            logger.info(f"已卖出 {qty} 股 {code} @ {price:.3f}")
                            
                    elif action == TradeAction.BUY:
                        trade_record = await asyncio.to_thread(executor.execute_trade, user_id, code, action, price=price, quantity=qty, reason=reason)
                        if trade_record:
                            await update_wallet(db_session, wallet, -(qty * price))
                            await update_holding_buy(db_session, holding, qty, price, is_t1=is_t1, is_trend=order.get('is_trend_entry', False))
                            logger.info(f"已买入 {qty} 股 {code} @ {price:.3f}")

        except Exception as e:
            await db_session.rollback()
            logger.error("Trading bot error: %s", e, exc_info=True)
        finally:
            futu.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s")
    asyncio.run(run_trading_bot())
