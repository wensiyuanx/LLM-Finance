
import sys
import os
import argparse
import pandas as pd
import logging
from datetime import datetime, timedelta

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

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
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
        # Approximation: roughly 4 hourly candles per day * trading days (~0.7 of total days)
        expected_candles = int(days * 0.7 * 4) * 0.9 # 10% buffer
        if count > expected_candles:
            logger.info(f"Found {count} records in DB (expected ~{expected_candles}). Skipping fresh fetch.")
            return True
        else:
            logger.info(f"Found {count} records, but expected ~{expected_candles}. Will fetch from API to ensure full history.")
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

def run_backtest(code, cash=100000.0, start_date="2025-01-01"):
    """
    Invokes the Backtrader strategy with the specified code.
    """
    import backtrader as bt
    from scripts.backtest.standard_stock_mtf_strategy import StandardStockMTFStrategy
    
    cerebro = bt.Cerebro()
    curr_market = 'HK' if 'HK' in code.upper() else 'SZ'
    
    from_date = None
    if start_date:
        from_date = datetime.strptime(start_date, "%Y-%m-%d")
        
    cerebro.addstrategy(StandardStockMTFStrategy, market=curr_market, start_date=from_date)
    
    logger.info(f"Loading data for {code} from database...")
    
    query_60m = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='60m' ORDER BY time_key ASC"
    query_1d = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='1d' ORDER BY time_key ASC"
    raw_conn = engine.raw_connection()
    try:
        df_60m = pd.read_sql_query(query_60m, raw_conn)
        df_1d = pd.read_sql_query(query_1d, raw_conn)
    finally:
        raw_conn.close()
    
    if df_60m.empty or df_1d.empty:
        logger.error("Data missing in database even after fetch attempt.")
        return

    df_60m['time_key'] = pd.to_datetime(df_60m['time_key'])
    df_1d['time_key'] = pd.to_datetime(df_1d['time_key'])
    
    from_date = None
    if start_date:
        from_date = datetime.strptime(start_date, "%Y-%m-%d")

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
    cerebro.broker.set_slippage_perc(perc=0.002) # 0.2% slippage
    
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
        import platform
        font_prop = 'Heiti TC' if platform.system() == 'Darwin' else 'SimHei'
        
        output_file = f"backtest_result_{code}.png"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_file)
        
        # Adjust figure to accommodate the table below (height_ratios 2:1)
        fig, (ax_chart, ax_table) = plt.subplots(2, 1, figsize=(14, 12), gridspec_kw={'height_ratios': [2, 1]})
        
        all_timestamps = [bt.num2date(x) for x in cerebro.datas[0].datetime.array]
        all_closes = cerebro.datas[0].close.array
        
        # Filter for display
        if start_date:
            display_start = datetime.strptime(start_date, "%Y-%m-%d")
            plot_indices = [i for i, ts in enumerate(all_timestamps) if ts >= display_start]
            if plot_indices:
                timestamps = [all_timestamps[i] for i in plot_indices]
                closes = [all_closes[i] for i in plot_indices]
            else:
                timestamps, closes = all_timestamps, all_closes
        else:
            timestamps, closes = all_timestamps, all_closes

        ax_chart.plot(timestamps, closes, label='价格 (小时线)', color='blue', alpha=0.6)
        
        if strategy_instance.buy_markers:
            buy_markers = strategy_instance.buy_markers
            if start_date: buy_markers = [m for m in buy_markers if m[0] >= display_start]
            if buy_markers:
                buy_dates, buy_prices = zip(*buy_markers)
                ax_chart.scatter(buy_dates, buy_prices, marker='^', color='green', s=100, label='买入', zorder=5)
            
        if strategy_instance.sell_markers:
            sell_markers = strategy_instance.sell_markers
            if start_date: sell_markers = [m for m in sell_markers if m[0] >= display_start]
            if sell_markers:
                sell_dates, sell_prices = zip(*sell_markers)
                ax_chart.scatter(sell_dates, sell_prices, marker='v', color='red', s=100, label='卖出', zorder=5)
        
        return_pct = (final_value - initial_value) / initial_value * 100
        first_price = closes[0]
        last_price = closes[-1]
        asset_return_pct = (last_price - first_price) / first_price * 100
        
        title = (f'回测结果 - {code} (标准MTF策略)\n'
                 f'策略收益: {return_pct:.2f}% | 标的收益: {asset_return_pct:.2f}%\n'
                 f'交易次数: {strategy_instance.trade_count} | 最终净值: {final_value:.2f}')
        
        ax_chart.set_title(title, fontproperties=font_prop, fontsize=14)
        ax_chart.set_xlabel('日期', fontproperties=font_prop)
        ax_chart.set_ylabel('价格', fontproperties=font_prop)
        ax_chart.grid(True, alpha=0.3)
        ax_chart.legend(prop={'family': font_prop})

        # --- Add Trade Details Table ---
        ax_table.axis('off')
        if hasattr(strategy_instance, 'trade_log') and strategy_instance.trade_log:
            trades = strategy_instance.trade_log
            if start_date:
                trades = [t for t in trades if t['date'] >= display_start]
            
            # Show last 25 trades for context
            display_trades = trades[-25:]
            table_data = []
            for t in display_trades:
                table_data.append([
                    t['date'].strftime("%m-%d %H:%M"),
                    "买入" if t['action'] == "BUY" else "卖出",
                    f"{t['price']:.2f}",
                    str(abs(int(t['qty']))),
                    t['reason']
                ])
            
            if table_data:
                col_labels = ["时间", "动作", "价格", "数量", "原因"]
                the_table = ax_table.table(cellText=table_data, colLabels=col_labels, loc='center', cellLoc='center')
                the_table.auto_set_font_size(False)
                the_table.set_fontsize(10)
                the_table.scale(1.0, 1.5)
                for key, cell in the_table.get_celld().items():
                    cell.set_text_props(fontproperties=font_prop)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=300)
        logger.info(f"Plot saved to {output_path}")
        
    except Exception as e:
        logger.error(f"Plotting failed: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run backtest for a specific stock code.")
    parser.add_argument("code", type=str, help="Stock code (e.g., HK.00700, SZ.159915)")
    parser.add_argument("--days", type=int, default=550, help="Days of history to fetch (default: 550)")
    parser.add_argument("--start_date", type=str, default=None, help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--cash", type=float, default=100000.0, help="Initial cash (default: 100000)")
    
    args = parser.parse_args()
    
    # 1. Fetch Data
    success = fetch_and_save_data(args.code, args.days)
    
    # 2. Run Backtest
    if success:
        run_backtest(args.code, args.cash, start_date=args.start_date)
