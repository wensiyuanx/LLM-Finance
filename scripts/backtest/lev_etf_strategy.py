import backtrader as bt
from datetime import datetime
import pandas as pd

class LeveragedETFMomentumStrategy(bt.Strategy):
    """
    Optimized Hit-and-Run Leveraged ETF Strategy.
    Balances trade frequency with quality for better returns.
    """
    params = (
        ('sma_fast', 5),
        ('sma_slow', 20),
        ('adx_min', 36),        # Balanced threshold - not too strict, not too loose
        ('atr_period', 14),
        ('start_date', None),
        ('market', 'HK'),
        ('stop_atr_mult', 5.8), # Balanced stop distance
        ('profit_target', 0.22), # Higher target to compensate for fewer trades
        ('max_position_size', 0.80), # Moderate position size
        ('min_trades_bars', 12), # Prevent overtrading
        ('trailing_stop_activation', 0.10), # Activate trailing stop at 10% profit
        ('breakeven_profit', 0.06), # Move to breakeven at 6% profit
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.daily_close = self.datas[1].close if len(self.datas) > 1 else self.datas[0].close
        self.daily_sma_trend = bt.indicators.SMA(self.daily_close, period=20) # Switched to 20 for faster confirmation
        
        # Indicators
        self.sma5 = bt.indicators.SMA(self.datas[0], period=5)
        self.sma20 = bt.indicators.SMA(self.datas[0], period=20)
        self.macd = bt.indicators.MACD(self.datas[0])
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=14)
        self.adx = bt.indicators.ADX(self.datas[0], period=14)
        self.atr = bt.indicators.ATR(self.datas[0], period=14)

        self.order = None
        self.buyprice = None
        self._highest_price = 0.0
        self._dynamic_stop_price = 0.0
        self._bars_since_last_trade = 0
        
        # Plotting metadata
        self.buy_markers = []
        self.sell_markers = []
        self.trade_count = 0
        self.trade_log = []

    def notify_order(self, order):
        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            if order.isbuy():
                if self.buyprice is None: # Only update for the first buy in a slot
                    self.buyprice = order.executed.price
                    self._highest_price = self.buyprice
                    self._dynamic_stop_price = self.buyprice - (self.atr[0] * self.params.stop_atr_mult)
                self.buy_markers.append((dt, order.executed.price))
                self.trade_log.append({
                    'date': dt,
                    'action': 'BUY',
                    'price': order.executed.price,
                    'qty': order.executed.size,
                    'reason': 'Momentum Entry'
                })
                # Reset bars counter on buy
                self._bars_since_last_trade = 0
            elif order.issell():
                self.sell_markers.append((dt, order.executed.price))
                self.trade_count += 1
                profit_pct = (order.executed.price - self.buyprice) / self.buyprice * 100 if self.buyprice else 0
                self.trade_log.append({
                    'date': dt,
                    'action': 'SELL',
                    'price': order.executed.price,
                    'qty': order.executed.size,
                    'reason': f'Exit (P&L: {profit_pct:.1f}%)'
                })
                self.buyprice = None
                self._highest_price = 0.0
                self._dynamic_stop_price = 0.0
                self._bars_since_last_trade = 0  # Reset counter after sell
        elif order.status in [order.Cancelled, order.Rejected, order.Margin]:
            self.order = None
            return
        self.order = None

    def next(self):
        if self.order:
            return

        current_price = self.dataclose[0]
        dt = bt.num2date(self.datas[0].datetime[0])
        
        # Track bars since last trade
        if self.position.size <= 0:
            self._bars_since_last_trade += 1

        # 1. EXIT LOGIC - Enhanced with smarter risk management
        if self.position.size > 0 and self.buyprice is not None:
            profit_pct = (current_price - self.buyprice) / self.buyprice
            
            if current_price > self._highest_price:
                self._highest_price = current_price

            sell_signal = False
            reason = ""
            
            # Take profit at target
            if profit_pct > self.params.profit_target:
                sell_signal = True
                reason = "Take Profit"
            
            # Breakeven stop after reaching breakeven_profit
            if profit_pct > self.params.breakeven_profit:
                self._dynamic_stop_price = max(self._dynamic_stop_price, self.buyprice * 1.015)
            
            # Smarter trailing stop - tighter when in profit, looser when in loss
            if profit_pct > self.params.trailing_stop_activation:
                # Use tighter trailing when significantly profitable
                trailing_mult = self.params.stop_atr_mult * 0.7
            elif profit_pct > 0:
                # Use moderately tighter trailing when slightly profitable
                trailing_mult = self.params.stop_atr_mult * 0.85
            else:
                # Use looser trailing when losing
                trailing_mult = self.params.stop_atr_mult
                
            trailing = self._highest_price - (self.atr[0] * trailing_mult)
            self._dynamic_stop_price = max(self._dynamic_stop_price, trailing)

            # Dynamic stop hit
            if not sell_signal and current_price <= self._dynamic_stop_price:
                sell_signal = True
                reason = "Dynamic Stop"
            # Extreme overbought condition with profit - very conservative
            elif not sell_signal and self.rsi[0] > 92 and profit_pct > 0.10:
                sell_signal = True
                reason = "Extreme Overbought"
            # Momentum reversal - only exit if both MACD crosses below AND RSI drops below 42 AND significant loss
            elif not sell_signal and self.macd.macd[0] < self.macd.signal[0] and self.rsi[0] < 42 and profit_pct < -0.06:
                sell_signal = True
                reason = "Momentum Reversal"

            if sell_signal:
                self.order = self.sell(size=self.position.size)
                print(f"{dt} - 离场: {reason}, 收益 {profit_pct*100:.1f}%")
                return

        # 2. ENTRY LOGIC - Stricter filtering to improve win rate
        if self.position.size <= 0 and self._bars_since_last_trade >= self.params.min_trades_bars:
            # Check all indicators are available
            if len(self.dataclose) < self.params.sma_slow:
                return
                
            daily_ok = self.daily_close[0] > self.daily_sma_trend[0]
            adx_extreme = self.adx[0] > self.params.adx_min
            momentum_ok = self.sma5[0] > self.sma20[0] and self.macd.macd[0] > self.macd.signal[0]
            
            # More flexible RSI range - allow more entries while avoiding extremes
            rsi_ok = 52 < self.rsi[0] < 80
            
            # Additional filter: price above 20-period high - slightly more flexible
            highest_20 = max([self.dataclose[-i] for i in range(1, min(21, len(self.dataclose)))])
            price_strength = current_price > highest_20 * 0.975
            
            # Volume confirmation - ensure recent volume is above average
            recent_volume = self.datas[0].volume[0]
            avg_volume = sum([self.datas[0].volume[-i] for i in range(1, min(21, len(self.datas[0].volume)))]) / min(20, len(self.datas[0].volume) - 1)
            volume_ok = recent_volume > avg_volume * 0.75
            
            # Only enter if all conditions are met and no pending order
            if daily_ok and adx_extreme and momentum_ok and rsi_ok and price_strength and volume_ok and self.order is None:
                cash = self.broker.getcash()
                qty = int(cash * self.params.max_position_size / current_price / 100) * 100
                if qty > 0:
                    self.order = self.buy(size=qty)
                    print(f"{dt} - 极端动量入场: 价格 {current_price:.3f}, ADX: {self.adx[0]:.1f}, RSI: {self.rsi[0]:.1f}, Vol: {recent_volume:.0f}")
