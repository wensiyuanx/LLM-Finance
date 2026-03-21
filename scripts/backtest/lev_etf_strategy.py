import backtrader as bt
from datetime import datetime
import pandas as pd

class LeveragedETFMomentumStrategy(bt.Strategy):
    """
    Backtrader implementation of the Leveraged ETF Momentum Strategy.
    Uses strict right-side breakout logic to avoid volatility decay and falling knives.
    """
    params = (
        ('sma_fast', 5),
        ('sma_slow', 20),
        ('sma_trend', 50),
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
        ('rsi_period', 14),
        ('rsi_overbought', 75),
        ('vol_sma_period', 5),
        ('adx_period', 14),
        ('adx_min', 20),           
        ('atr_period', 14),        
        ('atr_stop_mult', 3.0),    
        ('atr_trail_mult', 2.0),   
        ('max_tranches', 3),       # Maximum number of pyramid entries
        ('grid_drop_pct', 0.08),   # Buy next tranche if price drops 8% from last buy
        ('market', 'HK'), 
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.volume = self.datas[0].volume
        self.high = self.datas[0].high
        self.low = self.datas[0].low
        
        # Load Daily Data for Macro Trend Filter (assuming Data1 is daily)
        if len(self.datas) > 1:
            self.daily_close = self.datas[1].close
            self.daily_sma_trend = bt.indicators.SMA(self.datas[1].close, period=self.params.sma_trend)
        else:
            self.daily_close = None
            self.daily_sma_trend = None
        
        # Indicators
        self.sma_fast = bt.indicators.SMA(self.datas[0], period=self.params.sma_fast)
        self.sma_slow = bt.indicators.SMA(self.datas[0], period=self.params.sma_slow)
        self.sma_trend = bt.indicators.SMA(self.datas[0], period=self.params.sma_trend)
        
        self.macd = bt.indicators.MACD(self.datas[0], 
                                       period_me1=self.params.macd_fast, 
                                       period_me2=self.params.macd_slow, 
                                       period_signal=self.params.macd_signal)
        
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_period)
        self.vol_sma = bt.indicators.SMA(self.datas[0].volume, period=self.params.vol_sma_period)
        
        # Add ADX to measure trend strength and filter out choppy markets
        self.adx = bt.indicators.AverageDirectionalMovementIndex(self.datas[0], period=self.params.adx_period)
        
        # Add ATR for dynamic volatility-based stops
        self.atr = bt.indicators.ATR(self.datas[0], period=self.params.atr_period)

        self.order = None
        self.buyprice = None
        self._highest_price = None
        self._dynamic_stop_price = None 
        
        # Pyramid / Grid tracking
        self.tranches_count = 0
        self.last_buy_price = None
        
        # Tracking for visualization
        self.buy_markers = []
        self.sell_markers = []
        self.trade_log = []
        self.trade_count = 0
        self.max_capital_deployed = 0.0

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
            
        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            reason = getattr(order, 'reason', "N/A")
            
            if order.isbuy():
                # If first tranche, set initial prices
                if self.tranches_count == 0:
                    self.buyprice = order.executed.price
                    self._highest_price = order.executed.price
                    self._dynamic_stop_price = self.buyprice - (self.atr[0] * self.params.atr_stop_mult)
                else:
                    # Update average cost
                    total_cost = self.buyprice * self.position.size(before=True) + order.executed.price * order.executed.size
                    total_size = self.position.size(before=True) + order.executed.size
                    self.buyprice = total_cost / total_size
                    
                    # Update stop loss based on new average cost
                    new_stop = self.buyprice - (self.atr[0] * self.params.atr_stop_mult)
                    self._dynamic_stop_price = max(self._dynamic_stop_price, new_stop) # Ensure stop doesn't move down too much

                self.last_buy_price = order.executed.price
                self.tranches_count += 1
                
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                self.last_buy_date = dt.date()
                
                cost = order.executed.price * order.executed.size
                self.max_capital_deployed = max(self.max_capital_deployed, cost)
                
            elif order.issell():
                self.buyprice = None
                self._highest_price = None
                self._dynamic_stop_price = None
                self.tranches_count = 0
                self.last_buy_price = None
                self.sell_markers.append((dt, order.executed.price))
                
            self.trade_log.append({
                'date': dt,
                'action': "BUY" if order.isbuy() else "SELL",
                'price': order.executed.price,
                'qty': order.executed.size,
                'reason': reason
            })
        
        self.order = None

    def next(self):
        if self.order:
            return

        current_price = self.dataclose[0]
        dt = self.datas[0].datetime.datetime(0)
        
        # T+0 / T+1 restriction logic
        current_date = self.datas[0].datetime.date(0)
        if self.params.market == 'HK':
            can_sell = True
        else:
            can_sell = getattr(self, 'last_buy_date', None) != current_date

        # --- 1. EXIT & RISK MANAGEMENT ---
        if self.position:
            # Update highest price and dynamic trailing stop
            if current_price > self._highest_price:
                self._highest_price = current_price
                # Calculate new trailing stop based on recent high and ATR
                new_trail_stop = self._highest_price - (self.atr[0] * self.params.atr_trail_mult)
                # Ensure stop loss only moves up, never down
                self._dynamic_stop_price = max(self._dynamic_stop_price, new_trail_stop)
                
            sell_signal = False
            sell_reason = ""
            
            # A. Dynamic ATR Stop Loss / Trailing Stop
            if current_price <= self._dynamic_stop_price:
                sell_signal = True
                profit_pct = (current_price - self.buyprice) / self.buyprice
                if profit_pct > 0:
                    sell_reason = f"杠杆风控: 触发ATR跟踪止盈 (收益 {profit_pct*100:.1f}%)"
                else:
                    sell_reason = f"杠杆风控: 触发ATR硬止损 (亏损 {profit_pct*100:.1f}%)"
                    
            # B. Extreme Overbought Profit Taking (Wave riding)
            elif self.rsi[0] > 85 and ((current_price - self.buyprice) / self.buyprice) > 0.15:
                sell_signal = True
                sell_reason = f"波段逃顶: 极端超买且获利丰厚 (收益 {((current_price - self.buyprice)/self.buyprice)*100:.1f}%)"
                    
            # C. Trend Death Cross
            elif self.sma_fast[-1] >= self.sma_slow[-1] and self.sma_fast[0] < self.sma_slow[0]:
                sell_signal = True
                sell_reason = "右侧离场: 小时线均线死叉"
                
            # D. MACD High-level Death Cross or MACD drops below zero
            elif self.macd.macd[-1] >= self.macd.signal[-1] and self.macd.macd[0] < self.macd.signal[0]:
                if self.macd.macd[0] > 0:
                    sell_signal = True
                    sell_reason = "右侧离场: MACD高位死叉"
            elif self.macd.macd[-1] >= 0 and self.macd.macd[0] < 0:
                sell_signal = True
                sell_reason = "右侧离场: MACD下穿零轴"

            if sell_signal and can_sell:
                self.order = self.sell(size=self.position.size)
                self.order.reason = sell_reason
                print(f"{dt} - 触发卖出: {sell_reason}, 价格: {current_price:.3f}")
                return

        # --- 2. ENTRY LOGIC (Strict Right-Side Breakout OR Pyramid Scaling) ---
        if not self.position or (self.position and self.tranches_count < self.params.max_tranches):
            buy_signal = False
            buy_reason = ""
            
            # Check for Pyramid Scaling (Average Down) if already in position
            if self.position and self.last_buy_price:
                drop_from_last = (self.last_buy_price - current_price) / self.last_buy_price
                if drop_from_last >= self.params.grid_drop_pct:
                    # Additional check: Don't average down if MACD is dead crossing or Daily trend is down
                    daily_ok = True
                    if self.daily_close is not None and self.daily_sma_trend is not None:
                        daily_ok = self.daily_close[-1] > self.daily_sma_trend[-1]
                        
                    if daily_ok:
                        buy_signal = True
                        buy_reason = f"金字塔加仓: 第 {self.tranches_count + 1} 批 (跌幅 {drop_from_last*100:.1f}%)"
                        
            # Original Momentum Breakout (Only for initial entry)
            if not self.position:
                # Condition 1: Moving Averages Bullish Alignment
                ma_bullish = (self.sma_fast[0] > self.sma_slow[0] > self.sma_trend[0]) and (current_price > self.sma_fast[0])
                
                # Condition 2: MACD Momentum
                macd_just_crossed = (self.macd.macd[-1] <= self.macd.signal[-1]) and (self.macd.macd[0] > self.macd.signal[0])
                macd_strong = (self.macd.macd[0] > 0) and (self.macd.macd[0] > self.macd.signal[0])
                macd_bullish = macd_just_crossed or macd_strong
                
                # Condition 3: Volume Surge (>50% above SMA)
                volume_confirmed = self.volume[0] > (self.vol_sma[0] * 1.5)
                
                # Condition 4: Not overbought, but strong
                rsi_ok = 50 < self.rsi[0] < self.params.rsi_overbought
                
                # Additional trend filter: MACD must be positive (above zero line) for ALL buys
                macd_positive = self.macd.macd[0] > 0
                
                # ADX Trend Filter: Ensure we are in a sufficiently strong trend environment
                strong_trend = self.adx[0] > self.params.adx_min
                super_trend = self.adx[0] > 40
                
                # Price must be above 50-period SMA to confirm broader trend
                if self.daily_close is not None and self.daily_sma_trend is not None:
                    above_trend = self.daily_close[-1] > self.daily_sma_trend[-1]
                else:
                    above_trend = current_price > self.sma_trend[0]
                    
                # Super Trend Exemption: If ADX is insanely high and MACD is strong, bypass daily filter
                # This catches the "V-shape" violent bottoms that leveraged ETFs sometimes produce
                if super_trend and macd_bullish and macd_positive:
                    above_trend = True
                
                # Golden Cross alternative (Early Right Side)
                golden_cross = (self.sma_fast[-1] <= self.sma_slow[-1]) and (self.sma_fast[0] > self.sma_slow[0])
                
                if ma_bullish and macd_bullish and volume_confirmed and rsi_ok and strong_trend and above_trend:
                    buy_signal = True
                    if super_trend:
                        buy_reason = "超级动量突破: 均线多头 + MACD向好 + 显著放量 + 极强趋势(ADX>40)"
                    else:
                        buy_reason = "动量突破: 均线多头 + MACD向好 + 显著放量 + 强趋势 + 大级别多头"
                elif golden_cross and volume_confirmed and rsi_ok and macd_positive and above_trend and strong_trend:
                    buy_signal = True
                    buy_reason = "右侧入场: 均线金叉 + 显著放量 + MACD>0 + 大级别多头 + 强趋势"

            if buy_signal:
                cash = self.broker.getcash()
                
                # Sizing: Divide capital by max tranches
                # If total capital is 100k, and max tranches = 3, each tranche is ~33k
                # But we only use 80% of total capital max to leave room
                total_target_capital = (self.broker.getvalue() * 0.8) 
                tranche_capital = total_target_capital / self.params.max_tranches
                
                # Ensure we have enough cash
                alloc = min(cash * 0.95, tranche_capital)
                qty = int(alloc / current_price / 100) * 100
                
                if qty > 0:
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason
                    print(f"{dt} - 触发买入: {buy_reason}, 价格: {current_price:.3f}, 数量: {qty}")
