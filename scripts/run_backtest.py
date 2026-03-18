
import sys
import os
import argparse
import pandas as pd
import logging
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import engine, SessionLocal
from database.models import MarketType
from data.futu_client import FutuClient
from main import save_klines_to_db, format_futu_df

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def fetch_and_save_data(code, days=550):
    """
    Checks if data exists in DB. If not, fetches from Futu/YFinance and saves it.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    logger.info(f"Checking data for {code} from {start_date} to {end_date}...")
    
    # Check DB first
    query_check = f"SELECT count(*) as count FROM kline_data WHERE code='{code}' AND timeframe='60m'"
    try:
        df_check = pd.read_sql_query(query_check, engine)
        count = df_check['count'].iloc[0]
        if count > 100:
            logger.info(f"Found {count} records in DB. Skipping fetch.")
            return True
    except Exception as e:
        logger.warning(f"DB check failed: {e}")

    # Connect to data providers
    futu = FutuClient()
    connected = futu.connect()
    
    df_day = None
    df_60m = None
    
    # Try Futu first
    if connected:
        from futu import KLType
        logger.info("Fetching from Futu...")
        df_day = futu.get_historical_klines(code, start_date, end_date, ktype=KLType.K_DAY)
        df_60m = futu.get_historical_klines(code, start_date, end_date, ktype=KLType.K_60M)
        
        if df_day is not None: df_day = format_futu_df(df_day)
        if df_60m is not None: df_60m = format_futu_df(df_60m)
        futu.close()
    
    if df_day is None or df_day.empty or df_60m is None or df_60m.empty:
        logger.error("Failed to fetch data from all sources.")
        return False
        
    # Save to DB
    session = SessionLocal()
    try:
        # User ID 1 is default admin
        save_klines_to_db(session, 1, code, df_day, timeframe='1d')
        save_klines_to_db(session, 1, code, df_60m, timeframe='60m')
        logger.info(f"Successfully saved data for {code} to database.")
        return True
    except Exception as e:
        logger.error(f"Failed to save to DB: {e}")
        return False
    finally:
        session.close()

def run_backtest(code, cash=100000.0):
    """
    Invokes the Backtrader strategy with the specified code.
    """
    import backtrader as bt
    from scripts.backtest.backtrader_strategy import MultiTimeframeStrategy
    
    cerebro = bt.Cerebro()
    cerebro.addstrategy(MultiTimeframeStrategy)
    
    logger.info(f"Loading data for {code} from database...")
    
    query_60m = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='60m' ORDER BY time_key ASC"
    df_60m = pd.read_sql_query(query_60m, engine)
    
    query_1d = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='1d' ORDER BY time_key ASC"
    df_1d = pd.read_sql_query(query_1d, engine)
    
    if df_60m.empty or df_1d.empty:
        logger.error("Data missing in database even after fetch attempt.")
        return

    df_60m['time_key'] = pd.to_datetime(df_60m['time_key'])
    df_1d['time_key'] = pd.to_datetime(df_1d['time_key'])
    
    # Data0: Hourly
    data0 = bt.feeds.PandasData(
        dataname=df_60m, datetime='time_key',
        open='open', high='high', low='low', close='close', volume='volume',
        openinterest=-1, timeframe=bt.TimeFrame.Minutes, compression=60
    )
    cerebro.adddata(data0)
    
    # Data1: Daily
    data1 = bt.feeds.PandasData(
        dataname=df_1d, datetime='time_key',
        open='open', high='high', low='low', close='close', volume='volume',
        openinterest=-1, timeframe=bt.TimeFrame.Days, compression=1
    )
    cerebro.adddata(data1)
    
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.001)
    
    initial_value = cerebro.broker.getvalue()
    logger.info(f'Starting Portfolio Value: {initial_value:.2f}')
    
    strategies = cerebro.run()
    strategy_instance = strategies[0]
    
    final_value = cerebro.broker.getvalue()
    logger.info(f'Final Portfolio Value: {final_value:.2f}')
    
    # Plotting logic
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        output_file = f"backtest_result_{code}.png"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_file)
        
        plt.figure(figsize=(14, 8))
        timestamps = [bt.num2date(x) for x in cerebro.datas[0].datetime.array]
        closes = cerebro.datas[0].close.array
        plt.plot(timestamps, closes, label='Price (Hourly)', color='blue', alpha=0.6)
        
        if strategy_instance.buy_markers:
            buy_dates, buy_prices = zip(*strategy_instance.buy_markers)
            plt.scatter(buy_dates, buy_prices, marker='^', color='green', s=100, label='Buy', zorder=5)
            
        if strategy_instance.sell_markers:
            sell_dates, sell_prices = zip(*strategy_instance.sell_markers)
            plt.scatter(sell_dates, sell_prices, marker='v', color='red', s=100, label='Sell', zorder=5)
        
        return_pct = (final_value - initial_value) / initial_value * 100
        
        # Asset return
        first_price = closes[0]
        last_price = closes[-1]
        asset_return_pct = (last_price - first_price) / first_price * 100
        
        title = (f'Backtest Result - {code}\n'
                 f'Strategy Return: {return_pct:.2f}% | Asset Return: {asset_return_pct:.2f}%\n'
                 f'Trades: {strategy_instance.trade_count} | Final Value: {final_value:.2f}')
        
        # Font support
        import platform
        font_prop = 'Heiti TC' if platform.system() == 'Darwin' else 'SimHei'
        
        plt.title(title, fontproperties=font_prop, fontsize=14)
        plt.xlabel('Date', fontproperties=font_prop)
        plt.ylabel('Price', fontproperties=font_prop)
        plt.grid(True, alpha=0.3)
        plt.legend(prop={'family': font_prop})
        plt.tight_layout()
        
        plt.savefig(output_path, dpi=300)
        logger.info(f"Plot saved to {output_path}")
        
    except Exception as e:
        logger.error(f"Plotting failed: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run backtest for a specific stock code.")
    parser.add_argument("code", type=str, help="Stock code (e.g., HK.00700, SZ.159915)")
    parser.add_argument("--days", type=int, default=550, help="Days of history to fetch (default: 550)")
    parser.add_argument("--cash", type=float, default=100000.0, help="Initial cash (default: 100000)")
    
    args = parser.parse_args()
    
    # 1. Fetch Data
    success = fetch_and_save_data(args.code, args.days)
    
    # 2. Run Backtest
    if success:
        run_backtest(args.code, args.cash)
