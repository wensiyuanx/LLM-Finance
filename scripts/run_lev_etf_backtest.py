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

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import engine, SessionLocal
from data.futu_client import FutuClient
from main import save_klines_to_db, format_futu_df

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def fetch_and_save_data(code, days=550):
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    logger.info(f"Checking data for {code} from {start_date} to {end_date}...")
    
    query_check = f"SELECT count(*) as count FROM kline_data WHERE code='{code}' AND timeframe='60m'"
    try:
        df_check = pd.read_sql_query(query_check, engine)
        count = df_check['count'].iloc[0]
        expected_candles = int(days * 0.7 * 4) * 0.9
        if count > expected_candles:
            logger.info(f"Found {count} records in DB. Skipping fresh fetch.")
            return True
    except Exception as e:
        logger.warning(f"DB check failed: {e}")

    futu = FutuClient()
    connected = futu.connect()
    
    df_60m = None
    if connected:
        from futu import KLType
        logger.info("Fetching from Futu...")
        df_60m = futu.get_historical_klines(code, start_date, end_date, ktype=KLType.K_60M)
        if df_60m is not None: df_60m = format_futu_df(df_60m)
        futu.close()
    
    if df_60m is None or df_60m.empty:
        logger.error("Failed to fetch data.")
        return False
        
    session = SessionLocal()
    try:
        save_klines_to_db(session, 1, code, df_60m, timeframe='60m')
        logger.info(f"Successfully saved data for {code} to database.")
        return True
    except Exception as e:
        logger.error(f"Failed to save to DB: {e}")
        return False
    finally:
        session.close()

def run_backtest(code, cash=100000.0, start_date=None, end_date=None):
    import backtrader as bt
    from scripts.backtest.lev_etf_live_strategy import LeveragedETFLiveStrategy
    
    cerebro = bt.Cerebro()
    curr_market = 'HK' if 'HK' in code.upper() else 'SZ'
    
    from_date = None
    if start_date:
        from_date = datetime.strptime(start_date, "%Y-%m-%d")
        
    cerebro.addstrategy(LeveragedETFLiveStrategy, market=curr_market, start_date=from_date)
    
    logger.info(f"Loading data for {code} from database...")
    
    query_60m = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='60m'"
    query_day = f"SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='{code}' AND timeframe='1d'"
    
    if start_date:
        query_60m += f" AND time_key >= '{start_date} 00:00:00'"
        query_day += f" AND time_key >= '{start_date} 00:00:00'"
    if end_date:
        query_60m += f" AND time_key <= '{end_date} 23:59:59'"
        query_day += f" AND time_key <= '{end_date} 23:59:59'"
        
    query_60m += " ORDER BY time_key ASC"
    query_day += " ORDER BY time_key ASC"
    
    raw_conn = engine.raw_connection()
    try:
        df_60m = pd.read_sql_query(query_60m, raw_conn)
        df_day = pd.read_sql_query(query_day, raw_conn)
    finally:
        raw_conn.close()
    
    if df_60m.empty:
        logger.error("Data missing.")
        return

    df_60m['time_key'] = pd.to_datetime(df_60m['time_key'])
    df_day['time_key'] = pd.to_datetime(df_day['time_key'])
    
    data0 = bt.feeds.PandasData(
        dataname=df_60m, datetime='time_key',
        open='open', high='high', low='low', close='close', volume='volume',
        openinterest=-1, timeframe=bt.TimeFrame.Minutes, compression=60
    )
    cerebro.adddata(data0)
    
    if not df_day.empty:
        data1 = bt.feeds.PandasData(
            dataname=df_day, datetime='time_key',
            open='open', high='high', low='low', close='close', volume='volume',
            openinterest=-1, timeframe=bt.TimeFrame.Days, compression=1
        )
        cerebro.adddata(data1)
    
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.0003)  # Further reduced to 0.03%
    cerebro.broker.set_slippage_perc(perc=0.0008)  # Reduced to 0.08%
    
    initial_value = cerebro.broker.getvalue()
    logger.info(f'Starting Portfolio Value: {initial_value:.2f}')
    
    strategies = cerebro.run()
    strategy_instance = strategies[0]
    
    final_value = cerebro.broker.getvalue()
    logger.info(f'Final Portfolio Value: {final_value:.2f}')
    
    # Plotting
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import platform
        font_prop = 'Heiti TC' if platform.system() == 'Darwin' else 'SimHei'
        
        output_file = f"lev_etf_backtest_result_{code}.png"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_file)
        
        fig, (ax_chart, ax_table) = plt.subplots(2, 1, figsize=(14, 12), gridspec_kw={'height_ratios': [2, 1]})
        
        all_timestamps = [bt.num2date(x) for x in cerebro.datas[0].datetime.array]
        all_closes = cerebro.datas[0].close.array
        
        if start_date:
            display_start = datetime.strptime(start_date, "%Y-%m-%d")
            plot_indices = [i for i, ts in enumerate(all_timestamps) if ts >= display_start]
            if plot_indices:
                timestamps = [all_timestamps[i] for i in plot_indices]
                closes = [all_closes[i] for i in plot_indices]
            else:
                timestamps, closes = all_timestamps, all_closes
                plot_indices = None # Explicitly set to None to avoid unbound local error
        else:
            timestamps, closes = all_timestamps, all_closes
            plot_indices = None

        ax_chart.plot(timestamps, closes, label='价格 (小时线)', color='blue', alpha=0.6)
        
        if strategy_instance.buy_markers:
            buy_markers = [m for m in strategy_instance.buy_markers if m[0] >= display_start] if start_date else strategy_instance.buy_markers
            if buy_markers:
                buy_dates, buy_prices = zip(*buy_markers)
                ax_chart.scatter(buy_dates, buy_prices, marker='^', color='green', s=100, label='突破买入', zorder=5)
            
        if strategy_instance.sell_markers:
            sell_markers = [m for m in strategy_instance.sell_markers if m[0] >= display_start] if start_date else strategy_instance.sell_markers
            if sell_markers:
                sell_dates, sell_prices = zip(*sell_markers)
                ax_chart.scatter(sell_dates, sell_prices, marker='v', color='red', s=100, label='风控止盈/损', zorder=5)
        
        return_pct = (final_value - initial_value) / initial_value * 100
        
        # FIX: Calculate asset return exactly matching the displayed chart period
        if plot_indices is not None and len(plot_indices) > 0:
            first_price = closes[0] # closes is already sliced above
            last_price = closes[-1]
        elif len(closes) > 0:
            first_price = closes[0]
            last_price = closes[-1]
        else:
            first_price = 1
            last_price = 1
            
        asset_return_pct = (last_price - first_price) / first_price * 100

        title = (f'杠杆ETF专用动量策略回测 - {code}\n'
                 f'策略收益: {return_pct:.2f}% | 标的收益: {asset_return_pct:.2f}%\n'
                 f'交易次数: {strategy_instance.trade_count} | 最终净值: {final_value:.2f}')
        
        ax_chart.set_title(title, fontproperties=font_prop, fontsize=14)
        ax_chart.set_xlabel('日期', fontproperties=font_prop)
        ax_chart.set_ylabel('价格', fontproperties=font_prop)
        ax_chart.grid(True, alpha=0.3)
        ax_chart.legend(prop={'family': font_prop})
        
        ax_table.axis('off')
        if hasattr(strategy_instance, 'trade_log') and strategy_instance.trade_log:
            trades = [t for t in strategy_instance.trade_log if t['date'] >= display_start] if start_date else strategy_instance.trade_log
            display_trades = trades[-30:]
            table_data = [[t['date'].strftime("%Y-%m-%d %H:%M"), "买入" if t['action'] == "BUY" else "卖出", f"{t['price']:.3f}", str(abs(t['qty'])), t['reason']] for t in display_trades]
            
            if table_data:
                the_table = ax_table.table(cellText=table_data, colLabels=["时间", "动作", "价格", "数量", "原因"], loc='center', cellLoc='center')
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
    parser = argparse.ArgumentParser(description="Run Leveraged ETF backtest.")
    parser.add_argument("code", type=str, help="Stock code (e.g., HK.07226)")
    parser.add_argument("--days", type=int, default=550)
    parser.add_argument("--start_date", type=str, default=None)
    parser.add_argument("--end_date", type=str, default=None)
    parser.add_argument("--cash", type=float, default=100000.0)
    
    args = parser.parse_args()
    if fetch_and_save_data(args.code, args.days + 60):
        run_backtest(args.code, args.cash, start_date=args.start_date, end_date=args.end_date)
