import backtrader as bt
from datetime import datetime
import pandas as pd

class MultiTimeframeStrategy(bt.Strategy):
    """
    Backtrader implementation of the Logic.py MTF strategy.
    Data0 = Hourly (60m) - Used for precise entries/exits
    Data1 = Daily (1d) - Used for trend filtering
    """
    params = (
        ('sma_fast', 5),         # Hourly fast SMA
        ('sma_slow', 20),        # Hourly slow SMA
        ('sma_trend_daily', 50), # Daily trend SMA
        ('rsi_period', 14),
        ('rsi_overbought', 70),  
        ('rsi_oversold', 35),    
        ('boll_period', 20),
        ('boll_dev', 2.0),
        ('atr_period', 14),
        ('adx_period', 14),
        ('adx_trend', 20),       
        ('atr_stop_loss_mult', 2.5), 
        ('atr_take_profit_mult', 3.0), 
        ('fixed_stop_loss', -0.08),  
        ('fixed_take_profit', 0.15), 
    )

    def __init__(self):
        # Data0: Hourly
        self.dataclose = self.datas[0].close
        # Data1: Daily
        self.daily_close = self.datas[1].close

        # Hourly Indicators
        self.sma_fast = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.params.sma_fast)
        self.sma_slow = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.params.sma_slow)
        
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_period)
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=self.params.boll_period, devfactor=self.params.boll_dev)
        
        self.atr = bt.indicators.ATR(self.datas[0], period=self.params.atr_period)
        
        # Daily Indicators
        self.daily_sma_trend = bt.indicators.SimpleMovingAverage(self.datas[1], period=self.params.sma_trend_daily)
        self.daily_adx = bt.indicators.DirectionalMovementIndex(self.datas[1], period=self.params.adx_period)
        
        # We need volume SMA on hourly
        self.vol_sma = bt.indicators.SimpleMovingAverage(self.datas[0].volume, period=self.params.sma_fast)

        self.order = None
        self.buyprice = None
        
        # To track buy/sell points for plotting
        self.buy_markers = []
        self.sell_markers = []
        self.trade_count = 0

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
            
        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            if order.isbuy():
                self.buyprice = order.executed.price
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
            elif order.issell():
                self.buyprice = None
                self.sell_markers.append((dt, order.executed.price))
        
        self.order = None

    def next(self):
        # Wait for enough daily data to calculate daily SMA
        if len(self.datas[1]) < self.params.sma_trend_daily:
            return

        if self.order:
            return

        current_price = self.dataclose[0]
        
        # Risk Management & Dynamic Stop Loss checks
        if self.position:
            profit_pct = (current_price - self.buyprice) / self.buyprice if self.buyprice else 0
            
            # Extreme SL/TP
            if profit_pct <= self.params.fixed_stop_loss:
                print(f"{self.datas[0].datetime.datetime(0)} - STOP LOSS triggered at {current_price:.2f}, profit: {profit_pct:.2%}")
                self.order = self.sell(size=self.position.size)
                return
            if profit_pct >= self.params.fixed_take_profit:
                print(f"{self.datas[0].datetime.datetime(0)} - TAKE PROFIT triggered at {current_price:.2f}, profit: {profit_pct:.2%}")
                self.order = self.sell(size=self.position.size)
                return
                
            # ATR Trailing
            if current_price <= (self.buyprice - (self.params.atr_stop_loss_mult * self.atr[0])):
                print(f"{self.datas[0].datetime.datetime(0)} - ATR TRAILING STOP triggered at {current_price:.2f}")
                self.order = self.sell(size=self.position.size)
                return
            if current_price >= (self.buyprice + (self.params.atr_take_profit_mult * self.atr[0])):
                print(f"{self.datas[0].datetime.datetime(0)} - ATR TAKE PROFIT triggered at {current_price:.2f}")
                self.order = self.sell(size=self.position.size)
                return

        # Daily Trend Filter
        # Strict macro filter: only buy when daily price is above 50-day SMA
        daily_trend_up = self.daily_close[0] > self.daily_sma_trend[0]
        
        # Hourly Strong Trend Filter (for exit rules)
        # Relaxed ADX filter for A-share ETFs which tend to be more volatile/choppy
        in_strong_trend = self.daily_adx.adx[0] > 15

        buy_signal = False
        sell_signal = False
        buy_reason = ""
        sell_reason = ""
        buy_signals = []
        sell_signals = []

        # BUY LOGIC: Only if Daily Trend is UP
        if daily_trend_up:
            # 1. Buy the dip: Price touches lower Bollinger Band
            if current_price <= self.boll.lines.bot[0]:
                buy_signals.append("触及小时线布林带下轨")
            
            # 2. Golden Cross
            if self.sma_fast[-1] <= self.sma_slow[-1] and self.sma_fast[0] > self.sma_slow[0]:
                if self.datas[0].volume[0] > self.vol_sma[0]:
                    buy_signals.append("小时线均线金叉且放量")
                
            # 3. RSI Oversold
            if self.rsi[0] < self.params.rsi_oversold:
                buy_signals.append(f"小时线RSI超卖({self.rsi[0]:.1f})")

        # SELL LOGIC: Take profits and cut losses (Independent of daily trend)
        if self.position:
            # 1. Take Profit: Hit upper Bollinger Band
            # For ETFs, we want to secure profits quickly in sideways markets
            if current_price >= self.boll.lines.top[0] and not in_strong_trend:
                sell_signals.append("触及小时线布林带上轨")
                
            # 2. Take Profit: RSI Overbought
            # Lowered threshold to 65 for ETFs to secure profits earlier
            if self.rsi[0] > 65 and not in_strong_trend:
                sell_signals.append(f"小时线RSI超买({self.rsi[0]:.1f})")
                
            # 3. Stop Loss / Trend Reversal: Fast MA crosses below Slow MA
            if self.sma_fast[-1] >= self.sma_slow[-1] and self.sma_fast[0] < self.sma_slow[0]:
                sell_signals.append("小时线均线死叉")

        # Multi-Factor Consensus
        if len(buy_signals) >= 2:
            buy_signal = True
            buy_reason = " + ".join(buy_signals)
        elif len(buy_signals) == 1 and ("均线金叉" in buy_signals[0] or "RSI超卖" in buy_signals[0]):
            buy_signal = True
            buy_reason = buy_signals[0]

        if len(sell_signals) >= 2:
            sell_signal = True
            sell_reason = " + ".join(sell_signals)
        elif len(sell_signals) == 1 and ("均线死叉" in sell_signals[0] or "布林带上轨" in sell_signals[0]):
            sell_signal = True
            sell_reason = sell_signals[0]

        # A-Share T+1 Protection (Simulation)
        # We temporarily disable this to test if morning buys improve yield
        current_time = self.datas[0].datetime.time()
        # if buy_signal and current_time.hour < 14:
        #     buy_signal = False # Suppress morning buys

        # Execution
        if not self.position:
            if buy_signal:
                cash = self.broker.getcash()
                target_allocation = cash * 0.95 
                qty = int(target_allocation / current_price / 100) * 100
                if qty > 0:
                    print(f"{self.datas[0].datetime.datetime(0)} - BUY SIGNAL at {current_price:.2f}, qty: {qty}, Reason: {buy_reason}")
                    self.order = self.buy(size=qty)
        else:
            if sell_signal:
                print(f"{self.datas[0].datetime.datetime(0)} - SELL SIGNAL at {current_price:.2f}, Reason: {sell_reason}")
                self.order = self.sell(size=self.position.size)

if __name__ == "__main__":
    import sys
    import os
    import pandas as pd
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    
    from database.db import engine

    cerebro = bt.Cerebro()
    cerebro.addstrategy(MultiTimeframeStrategy)

    print("Fetching data from local database...")
    
    # Fetch Hourly Data
    query_60m = "SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='HK.07226' AND timeframe='60m' ORDER BY time_key ASC"
    df_60m = pd.read_sql_query(query_60m, engine)
    
    # Fetch Daily Data
    query_1d = "SELECT time_key, open_price as open, high_price as high, low_price as low, close_price as close, volume FROM kline_data WHERE code='HK.07226' AND timeframe='1d' ORDER BY time_key ASC"
    df_1d = pd.read_sql_query(query_1d, engine)
    
    if not df_60m.empty and not df_1d.empty:
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
        
        cerebro.broker.setcash(100000.0)
        cerebro.broker.setcommission(commission=0.001)
        
        print('Starting Portfolio Value: %.2f' % cerebro.broker.getvalue())
        cerebro.run()
        print('Final Portfolio Value: %.2f' % cerebro.broker.getvalue())
        
        # Fallback Matplotlib
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backtest_result.png")
        
        try:
            plt.figure(figsize=(14, 8))
            timestamps = [bt.num2date(x) for x in cerebro.datas[0].datetime.array]
            closes = cerebro.datas[0].close.array
            plt.plot(timestamps, closes, label='标的价格走势(小时线)', color='blue', alpha=0.6)
            
            # Plot Buy/Sell markers
            strategy_instance = cerebro.runstrats[0][0]
            
            if strategy_instance.buy_markers:
                buy_dates, buy_prices = zip(*strategy_instance.buy_markers)
                plt.scatter(buy_dates, buy_prices, marker='^', color='green', s=100, label='买入 (B)', zorder=5)
                
            if strategy_instance.sell_markers:
                sell_dates, sell_prices = zip(*strategy_instance.sell_markers)
                plt.scatter(sell_dates, sell_prices, marker='v', color='red', s=100, label='卖出 (S)', zorder=5)
            
            initial_value = 100000.00
            final_value = cerebro.broker.get_value()
            return_pct = (final_value - initial_value) / initial_value * 100
            
            first_valid_idx = 100 
            if len(closes) > first_valid_idx:
                first_price = closes[first_valid_idx]
            else:
                first_price = closes[0]
                
            last_price = closes[-1]
            asset_return_pct = (last_price - first_price) / first_price * 100
            
            # Final Holding Quantity
            final_holding = strategy_instance.position.size if strategy_instance.position else 0
            
            title = (f'MTF跨周期策略回测结果 - HK.07226\n'
                     f'策略收益: {return_pct:.2f}% | 标的资产涨幅: {asset_return_pct:.2f}%\n'
                     f'期初资金: {initial_value:.2f} -> 期末资金: {final_value:.2f}\n'
                     f'交易次数: {strategy_instance.trade_count}次 | 最终持仓份额: {final_holding}')
            
            import platform
            font_prop = 'Heiti TC' if platform.system() == 'Darwin' else 'SimHei'
            
            plt.title(title, fontproperties=font_prop, fontsize=14)
            plt.xlabel('日期', fontproperties=font_prop)
            plt.ylabel('价格', fontproperties=font_prop)
            plt.grid(True, alpha=0.3)
            plt.legend(prop={'family': font_prop})
            
            plt.tight_layout()
            plt.savefig(output_path, dpi=300)
            print(f"Plot successfully generated using reliable fallback to {output_path}")
                
        except Exception as e:
            print(f"Plot generation failed with error: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("Failed to fetch data for backtest.")
