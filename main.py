import logging
from datetime import datetime, timedelta
from database.db import SessionLocal, init_db
from database.models import KLineData, TradeAction, SignalRecord, UserWallet, Holding, MarketType, AssetMonitor
from data.futu_client import FutuClient
from data.yf_client import YFinanceClient
from strategy.logic import generate_signals, generate_etf_signals
from engine.executor import OrderExecutor
from scripts.visualizer import generate_kline_chart
import pandas as pd
import concurrent.futures
from typing import Dict
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data-window constants
# ---------------------------------------------------------------------------
# 200-day SMA needs 200 trading days (~280 calendar days).
# 60-day LSTM window adds another 60 trading days (~85 calendar days).
# We pull 550 calendar days to guarantee enough trading days after weekends
# and holidays, with a comfortable buffer.
DATA_WINDOW_DAYS = 550

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


def save_klines_to_db(db, user_id: int, code: str, df: pd.DataFrame, timeframe: str = '1d'):
    """Bulk upsert K-line rows: one query to find existing timestamps, then batch insert new ones."""
    if df is None or df.empty:
        return
    has_turnover = 'turnover' in df.columns
    timestamps = df.index.tolist()
    existing = {
        r.time_key for r in db.query(KLineData.time_key).filter(
            KLineData.user_id == user_id,
            KLineData.code == code,
            KLineData.timeframe == timeframe,
            KLineData.time_key.in_(timestamps)
        ).all()
    }
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
        db.bulk_save_objects(new_rows)
        db.commit()
        logger.debug("Saved %d new %s K-line rows for %s", len(new_rows), timeframe, code)

    # --- Sliding Window Pruning ---
    # Keep only the latest 360 records for this specific code/user/timeframe
    try:
        # Find the threshold timestamp (the 360th record)
        recent_klines = (
            db.query(KLineData.time_key)
            .filter(KLineData.user_id == user_id, KLineData.code == code, KLineData.timeframe == timeframe)
            .order_by(KLineData.time_key.desc())
            .offset(359) # The 360th row
            .limit(1)
            .first()
        )

        if recent_klines:
            threshold_time = recent_klines.time_key
            # Delete any record older than the 360th record
            db.query(KLineData).filter(
                KLineData.user_id == user_id,
                KLineData.code == code,
                KLineData.timeframe == timeframe,
                KLineData.time_key < threshold_time
            ).delete()
            db.commit()
            logger.debug("Pruned %s K-line data for %s (kept latest 360)", timeframe, code)
    except Exception as e:
        db.rollback()
        logger.error("Error during %s K-line pruning for %s: %s", timeframe, code, e)


def save_signal_to_db(db, user_id: int, code: str, action, reason: str, close_price: float):
    db.add(SignalRecord(
        user_id=user_id, code=code,
        action=action, reason=reason, close_price=close_price
    ))
    db.commit()


def get_wallet(db, user_id: int, market_type):
    return db.query(UserWallet).filter(
        UserWallet.user_id == user_id,
        UserWallet.market_type == market_type
    ).first()


def update_wallet(db, wallet, delta: float):
    wallet.balance = round(wallet.balance + delta, 4)
    db.commit()


def get_holding(db, user_id: int, code: str, market_type) -> Holding:
    """Return the persistent Holding row; create a flat one if it doesn't exist yet."""
    holding = db.query(Holding).filter(
        Holding.user_id == user_id,
        Holding.code == code
    ).first()
    if holding is None:
        holding = Holding(
            user_id=user_id, code=code,
            quantity=0.0, avg_cost=0.0,
            market_type=market_type
        )
        db.add(holding)
        db.commit()
        db.refresh(holding)
    return holding


def update_holding_buy(db, holding: Holding, qty: float, price: float, is_t1: bool = True):
    """Weighted-average cost update on buy. T+1 means bought shares are NOT sellable today."""
    total_cost = (holding.quantity * holding.avg_cost) + (qty * price)
    holding.quantity = round(holding.quantity + qty, 6)
    holding.avg_cost = round(total_cost / holding.quantity, 6)
    # If not T+1 (like HK), instantly sellable. Otherwise, delayed to next day.
    if not is_t1:
        holding.sellable_quantity = round(holding.sellable_quantity + qty, 6)
    db.commit()


def update_holding_sell(db, holding: Holding, qty: float):
    """Reduce position on sell."""
    holding.quantity = round(holding.quantity - qty, 6)
    holding.sellable_quantity = round(holding.sellable_quantity - qty, 6)
    if holding.quantity <= 0.001:
        holding.quantity = 0.0
        holding.avg_cost = 0.0
        holding.sellable_quantity = 0.0
    db.commit()

def rollover_t1_holdings(db_session, user_id=None):
    """Called at start of day: all quantity becomes sellable_quantity."""
    from database.models import Holding
    query = db_session.query(Holding)
    if user_id: query = query.filter(Holding.user_id == user_id)
    holdings = query.all()
    for h in holdings:
        h.sellable_quantity = h.quantity
    db_session.commit()



# ---------------------------------------------------------------------------
# Main bot
# ---------------------------------------------------------------------------


from engine.portfolio import PortfolioManager

class PortfolioManagerOverride(PortfolioManager):
    pass # Removed the buggy re-implementation that shadows engine.portfolio.PortfolioManager

def rollover_t1_holdings_task():
    """Independent task to rollover T+1 holdings in the morning."""
    session = SessionLocal()
    try:
        logger.info("Executing morning T+1 holdings rollover...")
        rollover_t1_holdings(session)
    finally:
        session.close()

def run_trading_bot(market_filter=None):
    """
    market_filter: optional list of MarketType to limit which markets are processed.
    e.g. [MarketType.A_SHARE, MarketType.HK_SHARE] for the Asia session job,
         [MarketType.US_SHARE] for the US session job, or None to run all markets.
    """
    init_db()
    logger.info("Trading bot started%s.",
                f" | filter: {[m.value for m in market_filter]}" if market_filter else "")

    futu = FutuClient()
    futu_connected = futu.connect()
    if not futu_connected:
        print("Warning: Futu OpenAPI not connected. A-Share / HK-Share fetching will fail.")

    yf_client = YFinanceClient()
    db_session = SessionLocal()
    executor = OrderExecutor(db_session=db_session, futu_client=futu, simulate=True)

    try:
        # Large enough window so SMA_200 and the 60-day LSTM window are fully warmed up
        start_date = (datetime.now() - timedelta(days=DATA_WINDOW_DAYS)).strftime("%Y-%m-%d")
        end_date   = datetime.now().strftime("%Y-%m-%d")

        assets_query = db_session.query(AssetMonitor).filter(AssetMonitor.is_active == 1)
        if market_filter:
            assets_query = assets_query.filter(AssetMonitor.market_type.in_(market_filter))
        assets_query = assets_query.filter(AssetMonitor.market_type != MarketType.US_SHARE)
        active_assets = assets_query.all()
        
        if not active_assets:
            logger.warning("No active A/HK assets found for this run.")
            return

        from collections import defaultdict
        user_market_assets = defaultdict(list)
        for asset in active_assets:
            user_market_assets[(asset.user_id, asset.market_type)].append(asset)
            
        # Async Data Fetch Helper
        def fetch_asset_data(asset):
            df_day = None
            df_60m = None
            if futu_connected:
                from futu import KLType
                # Fetch daily data
                df_day = futu.get_historical_klines(asset.code, start_date=start_date, end_date=end_date, ktype=KLType.K_DAY)
                # Fetch 60M data
                df_60m = futu.get_historical_klines(asset.code, start_date=start_date, end_date=end_date, ktype=KLType.K_60M)
                
                if df_day is not None:
                    df_day = format_futu_df(df_day)
                if df_60m is not None:
                    df_60m = format_futu_df(df_60m)
            
            # Fallback to yfinance if Futu fails
            if df_day is None or df_day.empty:
                logger.info(f"Futu failed for {asset.code}, trying yfinance...")
                try:
                    yf_code = asset.code.replace('SZ.', '').replace('SH.', '') + '.SZ' if 'SZ.' in asset.code else asset.code
                    df_day = yf_client.get_historical_klines(yf_code, start_date=start_date, end_date=end_date, interval="1d")
                    df_60m = yf_client.get_historical_klines(yf_code, start_date=start_date, end_date=end_date, interval="1h")
                except Exception as e:
                    logger.error(f"YFinance fallback failed for {asset.code}: {e}")

            return asset.code, df_day, df_60m

        for (user_id, market_type), assets in user_market_assets.items():
            wallet = get_wallet(db_session, user_id, market_type)
            if not wallet:
                continue
                
            pm = PortfolioManager(db_session=db_session, current_cash=wallet.balance, max_position_pct=POSITION_SIZE_FRAC)
            signals_context = []
            asset_stats = {} 
            
            logger.info(f"Fetching data concurrently for User {user_id} - {market_type.name}...")
            # Fetch data sequentially instead of concurrently to prevent MySQL threading issues
            code_to_df = {}
            for a in assets:
                code, df_day, df_60m = fetch_asset_data(a)
                code_to_df[code] = {'1d': df_day, '60m': df_60m}

            for asset in assets:
                code = asset.code
                data_dict = code_to_df.get(code)
                if not data_dict:
                    continue
                    
                klines_day = data_dict.get('1d')
                klines_60m = data_dict.get('60m')

                if klines_day is None or klines_day.empty or klines_60m is None or klines_60m.empty:
                    logger.warning(f"No complete K-line data fetched for {code}.")
                    continue
                    
                # Save data to DB 
                save_klines_to_db(db_session, user_id, code, klines_day, timeframe='1d')
                save_klines_to_db(db_session, user_id, code, klines_60m, timeframe='60m')

                try:
                    from strategy.indicators import calculate_indicators
                    # Visualizer can just chart the hourly data for now
                    chart_df = calculate_indicators(klines_60m.copy())
                    generate_kline_chart(chart_df, code)
                except Exception as viz_e:
                    logger.error(f"Failed to generate chart for {code}: {viz_e}")

                # Load persistent holding
                holding = get_holding(db_session, user_id, code, market_type)
                
                # Check if asset is ETF and route to correct strategy
                if getattr(asset, 'is_etf', False):
                    logger.info(f"[{code}] Routing to ETF Strategy")
                    # Note: We now track tranches accurately in the DB via holding.tranches_count
                    action, reason, score = generate_etf_signals(
                        klines_60m, current_position=holding.quantity,
                        avg_cost=holding.avg_cost, tranches_count=holding.tranches_count
                    )
                else:
                    # Pass both timeframes to strategy
                    action, reason, score = generate_signals(
                        klines_60m, df_day=klines_day, current_position=holding.quantity,
                        avg_cost=holding.avg_cost, code=code
                    )
                latest_close = float(klines_60m['close'].iloc[-1])
                logger.info("[%s] Signal: %s — %s (Sellable: %s, Score: %.1f)", code, action.name, reason, holding.sellable_quantity, score)
                save_signal_to_db(db_session, user_id, code, action, reason, latest_close)
                
                asset_stats[code] = {'holding': holding}
                
                # A-Share T+1 Morning Protection
                current_hour = datetime.now().hour
                if market_type == MarketType.A_SHARE and action == TradeAction.BUY and current_hour < 14:
                    logger.warning("[%s] Suppressed BUY signal due to A-Share morning T+1 risk.", code)
                    continue

                if action in [TradeAction.BUY, TradeAction.SELL]:
                    signals_context.append({
                        'code': code,
                        'market_type': market_type,
                        'action': action,
                        'price': latest_close,
                        'sellable_qty': holding.sellable_quantity,
                        'reason': reason,
                        'score': score
                    })

            # Portfolio Manager resolution
            executable_orders = pm.evaluate_signals(signals_context)
            
            # Execute actual evaluated orders
            for order in executable_orders:
                code = order['code']
                action = order['action']
                qty = order['quantity']
                price = order['price']
                reason = order['reason']
                holding = asset_stats[code]['holding']
                
                # Determine T+1 applicability (True for A-Share, False for HK)
                is_t1 = (market_type == MarketType.A_SHARE)
                
                if action == TradeAction.SELL:
                    trade_record = executor.execute_trade(
                        user_id, code, action,
                        price=price, quantity=qty, reason=reason
                    )
                    if trade_record:
                        proceeds = qty * price
                        update_wallet(db_session, wallet, proceeds)
                        update_holding_sell(db_session, holding, qty)
                        logger.info(f"已卖出 {qty} 股 {code} @ {price:.3f}, 回款 {proceeds:.2f} {wallet.currency}")
                        
                elif action == TradeAction.BUY:
                    trade_cost = qty * price
                    trade_record = executor.execute_trade(
                        user_id, code, action,
                        price=price, quantity=qty, reason=reason
                    )
                    if trade_record:
                        update_wallet(db_session, wallet, -trade_cost)
                        update_holding_buy(db_session, holding, qty, price, is_t1=is_t1)
                        logger.info(f"已买入 {qty} 股 {code} @ {price:.3f}, 花费 {trade_cost:.2f} {wallet.currency}")

    except Exception as e:
        db_session.rollback()
        logger.error("Error during K-line fetching or trading: %s", e)



    except Exception as e:
        logger.error("Trading bot error: %s", e, exc_info=True)
    finally:
        futu.close()
        db_session.close()

if __name__ == "__main__":
    run_trading_bot()
