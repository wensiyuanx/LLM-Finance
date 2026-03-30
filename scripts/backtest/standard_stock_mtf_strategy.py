import backtrader as bt
from datetime import datetime
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class StandardStockMTFStrategy(bt.Strategy):
    """
    使用实盘策略逻辑的个股回测策略
    直接调用 strategy.logic.generate_signals
    保持与实盘完全一致
    """
    params = (
        ('sma_fast', 5),
        ('sma_slow', 20),
        ('sma_trend_daily', 50),
        ('rsi_period', 14),
        ('rsi_overbought', 70),
        ('rsi_oversold', 35),
        ('boll_period', 20),
        ('boll_dev', 2.0),
        ('atr_period', 14),
        ('adx_period', 14),
        ('atr_stop_loss_mult', 2.5),
        ('atr_take_profit_mult', 3.0),
        ('fixed_stop_loss', -0.08),
        ('fixed_take_profit', 0.15),
        ('market', 'A'),
        ('start_date', None),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.daily_close = self.datas[1].close

        self.sma_fast = bt.indicators.SMA(self.datas[0], period=self.params.sma_fast)
        self.sma_slow = bt.indicators.SMA(self.datas[0], period=self.params.sma_slow)
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_period)
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=self.params.boll_period, devfactor=self.params.boll_dev)
        self.atr = bt.indicators.ATR(self.datas[0], period=self.params.atr_period)

        self.sma20_h = bt.indicators.SMA(self.datas[0], period=20)
        self.sma60_h = bt.indicators.SMA(self.datas[0], period=60)
        self.sma120_h = bt.indicators.SMA(self.datas[0], period=120)
        self.daily_sma_trend = bt.indicators.SMA(self.datas[1], period=self.params.sma_trend_daily)
        self.daily_adx = bt.indicators.DirectionalMovementIndex(self.datas[1], period=self.params.adx_period)

        self.vol_sma = bt.indicators.SMA(self.datas[0].volume, period=self.params.sma_fast)

        self.order = None
        self.buyprice = None
        self._trend_breakout_used = False

        self.buy_markers = []
        self.sell_markers = []
        self.trade_log = []
        self.trade_count = 0
        self._highest_price = 0.0

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            reason = getattr(order, 'reason', "系统信号")

            if order.isbuy():
                self.buyprice = order.executed.price
                self._highest_price = self.buyprice
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                self.last_buy_date = dt.date()
            elif order.issell():
                self.buyprice = None
                self._highest_price = 0.0
                self.sell_markers.append((dt, order.executed.price))
                if self.position.size == 0:
                    self._trend_breakout_used = False

            self.trade_log.append({
                'date': dt,
                'action': "BUY" if order.isbuy() else "SELL",
                'price': order.executed.price,
                'qty': order.executed.size,
                'reason': reason
            })

        self.order = None

    def next(self):
        if self.params.start_date:
            if self.datas[0].datetime.date(0) < self.params.start_date.date():
                return

        if len(self.datas[1]) < self.params.sma_trend_daily:
            return

        if self.order:
            return

        current_price = self.dataclose[0]
        
        if self.position:
            self._highest_price = max(self._highest_price, current_price)

        if self.position:
            profit_pct = (current_price - self.buyprice) / self.buyprice if self.buyprice else 0

            if profit_pct <= self.params.fixed_stop_loss:
                print(f"{self.datas[0].datetime.datetime(0)} - 极度风控止损 at {current_price:.2f}, profit: {profit_pct:.2%}")
                self.order = self.sell(size=self.position.size)
                self.order.reason = f"极度风控止损 (亏损 {profit_pct*100:.2f}%)"
                return
            if profit_pct >= self.params.fixed_take_profit:
                print(f"{self.datas[0].datetime.datetime(0)} - 极度风控止盈 at {current_price:.2f}, profit: {profit_pct:.2%}")
                self.order = self.sell(size=self.position.size)
                self.order.reason = f"极度风控止盈 (盈利 {profit_pct*100:.2f}%)"
                return

            # Breakeven / Profit Protection Logic (Same as live trading)
            if self._highest_price > 0 and self.buyprice > 0:
                max_profit_pct = (self._highest_price - self.buyprice) / self.buyprice
                # If profit once reached > 2.5%, raise stop loss to entry price + 0.5%
                if max_profit_pct >= 0.025:
                    breakeven_price = self.buyprice * 1.005
                    if current_price < breakeven_price:
                        print(f"{self.datas[0].datetime.datetime(0)} - 触发保本护城河 at {current_price:.2f}")
                        self.order = self.sell(size=self.position.size)
                        if current_price >= self.buyprice:
                            self.order.reason = f"触发保本护城河 (利润曾达 {max_profit_pct*100:.1f}%), 保本微利离场"
                        else:
                            actual_loss_pct = (self.buyprice - current_price) / self.buyprice * 100
                            self.order.reason = f"触发保本护城河 (利润曾达 {max_profit_pct*100:.1f}%), 但因滑点/跳空导致实际亏损离场 ({actual_loss_pct:.2f}%)"
                        return

            if 'ATR_14' in self.datas[0].__dict__:
                atr = self.atr[0]
                if current_price <= (self.buyprice - (self.params.atr_stop_loss_mult * atr)):
                    print(f"{self.datas[0].datetime.datetime(0)} - ATR动态止损 at {current_price:.2f}")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = "ATR动态止损 (当前低于成本2.5倍真实日波动)"
                    return
                if current_price >= (self.buyprice + (self.params.atr_take_profit_mult * atr)):
                    print(f"{self.datas[0].datetime.datetime(0)} - ATR动态止盈 at {current_price:.2f}")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = "ATR动态止盈 (当前高于成本3倍真实日波动)"
                    return

        daily_trend_up = self.daily_close[-1] > self.daily_sma_trend[-1]
        in_strong_trend = self.daily_adx.adx[-1] > 20

        buy_signal = False
        sell_signal = False
        buy_reason = ""
        sell_reason = ""
        buy_signals = []
        sell_signals = []

        if daily_trend_up:
            if current_price <= self.boll.lines.bot[0]:
                buy_signals.append("触及小时线布林带下轨")

            if self.sma_fast[-1] <= self.sma_slow[-1] and self.sma_fast[0] > self.sma_slow[0]:
                if self.datas[0].volume[0] > self.vol_sma[0]:
                    buy_signals.append("小时线均线金叉且放量")

            if self.rsi[0] < self.params.rsi_oversold:
                buy_signals.append(f"小时线RSI超卖({self.rsi[0]:.1f})")

            if not self.position and not self._trend_breakout_used:
                in_strong_uptrend = (
                    self.sma20_h[0] > self.sma60_h[0] > self.sma120_h[0]
                    and current_price > self.sma20_h[0]
                    and 48 < self.rsi[0] < 72
                )
                if in_strong_uptrend:
                    buy_signals.append("趋势确认强力追入")
                    self._trend_breakout_used = True

        if self.position:
            is_extreme_trend = self.daily_adx.adx[-1] > 30

            if current_price >= self.boll.lines.top[0] and not in_strong_trend and not is_extreme_trend:
                sell_signals.append("触及小时线布林带上轨")

            if self.rsi[0] > 70 and not in_strong_trend and not is_extreme_trend:
                sell_signals.append(f"小时线RSI超买({self.rsi[0]:.1f})")

            if self.sma_fast[-1] >= self.sma_slow[-1] and self.sma_fast[0] < self.sma_slow[0]:
                sell_signals.append("小时线均线死叉")

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

        if buy_signal and sell_signal:
            print(f"{self.datas[0].datetime.datetime(0)} - 信号冲突 (Buy+Sell), 观望")
            return

        current_date = self.datas[0].datetime.date(0)

        if self.params.market == 'HK':
            can_sell = True
        else:
            can_sell = getattr(self, 'last_buy_date', None) != current_date

        if not self.position:
            if buy_signal:
                cash = self.broker.getcash()
                target_allocation = cash * 0.95
                qty = int(target_allocation / current_price / 100) * 100
                if qty > 0:
                    print(f"{self.datas[0].datetime.datetime(0)} - BUY SIGNAL at {current_price:.2f}, qty: {qty}, Reason: {buy_reason}")
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason
        else:
            if sell_signal:
                if can_sell:
                    print(f"{self.datas[0].datetime.datetime(0)} - SELL SIGNAL at {current_price:.2f}, Reason: {sell_reason}")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = sell_reason
                else:
                    print(f"{self.datas[0].datetime.datetime(0)} - SELL SIGNAL IGNORED due to T+1 restriction.")
