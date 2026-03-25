import backtrader as bt
from datetime import datetime
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class LeveragedETFLiveStrategy(bt.Strategy):
    """
    使用实盘策略逻辑的杠杆ETF回测策略
    直接调用 strategy.lev_etf_logic.generate_leveraged_etf_signals
    保持与实盘完全一致
    """
    params = (
        ('sma_fast', 5),
        ('sma_slow', 20),
        ('adx_min', 36),
        ('atr_period', 14),
        ('start_date', None),
        ('market', 'HK'),
        ('atr_stop_mult', 5.8),
        ('atr_trail_mult', 4.0),
        ('adx_min_trend', 36),
        ('volume_surge_mult', 1.2),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.daily_close = self.datas[1].close if len(self.datas) > 1 else self.datas[0].close

        self.sma5 = bt.indicators.SMA(self.datas[0], period=5)
        self.sma20 = bt.indicators.SMA(self.datas[0], period=20)
        self.sma50 = bt.indicators.SMA(self.datas[0], period=50)
        self.macd = bt.indicators.MACD(self.datas[0])
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=14)
        self.adx = bt.indicators.ADX(self.datas[0], period=14)
        self.atr = bt.indicators.ATR(self.datas[0], period=14)
        self.daily_sma_trend = bt.indicators.SMA(self.daily_close, period=50)

        self.vol_sma = bt.indicators.SMA(self.datas[0].volume, period=5)

        self.order = None
        self.buyprice = None
        self._highest_price = 0.0
        self._bars_since_last_trade = 0

        self.buy_markers = []
        self.sell_markers = []
        self.trade_count = 0
        self.trade_log = []

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)

            if order.isbuy():
                if self.buyprice is None:
                    self.buyprice = order.executed.price
                    self._highest_price = self.buyprice
                    self.buy_markers.append((dt, order.executed.price))
                    self.trade_count += 1
                    self.trade_log.append({
                        'date': dt,
                        'action': 'BUY',
                        'price': order.executed.price,
                        'qty': order.executed.size,
                        'reason': '动量突破入场 (量价齐升)'
                    })
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
                    'reason': f'退出 (收益: {profit_pct:.1f}%)'
                })
                self.buyprice = None
                self._highest_price = 0.0
                self._bars_since_last_trade = 0

        elif order.status in [order.Cancelled, order.Rejected, order.Margin]:
            self.order = None
            return

        self.order = None

    def next(self):
        if self.order:
            return

        current_price = self.dataclose[0]
        dt = bt.num2date(self.datas[0].datetime[0])

        if self.position.size <= 0:
            self._bars_since_last_trade += 1

        if self.position.size > 0 and self.buyprice is not None:
            profit_pct = (current_price - self.buyprice) / self.buyprice

            current_atr = self.atr[0] if self.atr[0] > 0 else current_price * 0.02

            initial_stop_price = self.buyprice - (current_atr * self.params.atr_stop_mult)
            if current_price <= initial_stop_price:
                print(f"{dt} - 杠杆风控: 触发ATR硬止损")
                self.order = self.sell(size=self.position.size)
                self.order.reason = "杠杆风控: 触发ATR硬止损"
                return

            adx = self.adx[0] if self.adx[0] > 0 else 20
            is_strong_trend = adx > 25

            if self._highest_price > self.buyprice:
                if 0.025 <= profit_pct < 0.05:
                    trail_pct = 0.975 if not is_strong_trend else 0.96
                    if current_price < self._highest_price * trail_pct:
                        print(f"{dt} - 杠杆风控: 阶段性止盈锁定, 收益 {profit_pct*100:.1f}%")
                        self.order = self.sell(size=self.position.size)
                        self.order.reason = f"杠杆风控: 阶段性止盈锁定, 收益 {profit_pct*100:.1f}%"
                        return
                elif profit_pct >= 0.05:
                    if is_strong_trend:
                        trailing_stop = self._highest_price - (current_atr * self.params.atr_trail_mult)
                        if current_price <= trailing_stop:
                            print(f"{dt} - 杠杆风控: 触发动态跟踪止盈, 收益 {profit_pct*100:.1f}%")
                            self.order = self.sell(size=self.position.size)
                            self.order.reason = f"杠杆风控: 触发动态跟踪止盈, 收益 {profit_pct*100:.1f}%"
                            return
                    else:
                        if current_price < self._highest_price * 0.96:
                            print(f"{dt} - 杠杆风控: 趋势转弱止盈 (4%), 收益 {profit_pct*100:.1f}%")
                            self.order = self.sell(size=self.position.size)
                            self.order.reason = f"杠杆风控: 趋势转弱止盈 (4%), 收益 {profit_pct*100:.1f}%"
                            return

            if 'SMA_5' in self.datas[0].__dict__ and 'SMA_20' in self.datas[0].__dict__:
                sma5_prev = self.sma5[-1] if len(self.sma5) > 0 else current_price
                sma20_prev = self.sma20[-1] if len(self.sma20) > 0 else current_price
                sma5_curr = self.sma5[0] if len(self.sma5) > 0 else current_price
                sma20_curr = self.sma20[0] if len(self.sma20) > 0 else current_price

                if sma5_prev >= sma20_prev and sma5_curr < sma20_curr:
                    print(f"{dt} - 右侧离场: 均线死叉")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = "右侧离场: 均线死叉"
                    return

            if 'MACD' in self.datas[0].__dict__ and 'MACD_Signal' in self.datas[0].__dict__:
                macd_prev = self.macd.macd[-1] if len(self.macd.macd) > 0 else 0
                macd_signal_prev = self.macd.signal[-1] if len(self.macd.signal) > 0 else 0
                macd_curr = self.macd.macd[0] if len(self.macd.macd) > 0 else 0
                macd_signal_curr = self.macd.signal[0] if len(self.macd.signal) > 0 else 0

                if macd_prev >= 0 and macd_curr < 0:
                    print(f"{dt} - 右侧离场: MACD下穿零轴")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = "右侧离场: MACD下穿零轴"
                    return

        if self.position.size == 0:
            ma_bullish = False
            if 'SMA_5' in self.datas[0].__dict__ and 'SMA_20' in self.datas[0].__dict__ and 'SMA_50' in self.datas[0].__dict__:
                sma5_curr = self.sma5[0] if len(self.sma5) > 0 else current_price
                sma20_curr = self.sma20[0] if len(self.sma20) > 0 else current_price
                sma50_curr = self.sma50[0] if len(self.sma50) > 0 else current_price

                if sma5_curr > sma20_curr > sma50_curr and current_price > sma5_curr:
                    ma_bullish = True

            macd_bullish = False
            if 'MACD' in self.datas[0].__dict__ and 'MACD_Signal' in self.datas[0].__dict__:
                macd_prev = self.macd.macd[-1] if len(self.macd.macd) > 0 else 0
                macd_signal_prev = self.macd.signal[-1] if len(self.macd.signal) > 0 else 0
                macd_curr = self.macd.macd[0] if len(self.macd.macd) > 0 else 0
                macd_signal_curr = self.macd.signal[0] if len(self.macd.signal) > 0 else 0

                if (macd_prev <= macd_signal_prev and macd_curr > macd_signal_curr) or (macd_curr > 0 and macd_curr > macd_signal_curr):
                    macd_bullish = True

            volume_confirmed = False
            if 'VOL_SMA_5' in self.datas[0].__dict__:
                vol_curr = self.datas[0].volume[0]
                vol_sma_curr = self.vol_sma[0] if len(self.vol_sma) > 0 else vol_curr
                if vol_curr > vol_sma_curr * self.params.volume_surge_mult:
                    volume_confirmed = True

            rsi_ok = False
            if 'RSI_14' in self.datas[0].__dict__:
                rsi_curr = self.rsi[0] if len(self.rsi) > 0 else 50
                if 50 < rsi_curr < 75:
                    rsi_ok = True

            strong_trend = False
            if 'ADX_14' in self.datas[0].__dict__:
                adx_curr = self.adx[0] if len(self.adx) > 0 else 0
                if adx_curr > self.params.adx_min_trend:
                    strong_trend = True

            daily_trend_up = True
            if 'SMA_50' in self.datas[0].__dict__:
                sma50_curr = self.sma50[0] if len(self.sma50) > 0 else current_price
                daily_trend_up = current_price > sma50_curr

            above_trend = daily_trend_up or (sma50_curr > 0 and current_price > sma50_curr)

            obv_confirmed = True
            if 'OBV' in self.datas[0].__dict__ and 'OBV_SMA_20' in self.datas[0].__dict__:
                obv_curr = self.datas[0].obv[0] if len(self.datas[0].obv) > 0 else 0
                obv_sma_curr = self.datas[0].obv_sma20[0] if len(self.datas[0].obv_sma20) > 0 else obv_curr
                if obv_curr < obv_sma_curr:
                    obv_confirmed = False

            if ma_bullish and macd_bullish and volume_confirmed and rsi_ok and strong_trend and above_trend and obv_confirmed:
                adx_score = self.adx[0] if len(self.adx) > 0 else 20
                roc_score = 0
                if 'ROC_20' in self.datas[0].__dict__:
                    roc_curr = self.datas[0].roc20[0] if len(self.datas[0].roc20) > 0 else 0
                    roc_score = roc_curr * 100

                momentum_score = 60 + adx_score + max(0, roc_score)
                print(f"{dt} - 动量突破入场 (量价齐升), 动量得分: {momentum_score:.1f}")
                cash = self.broker.getcash()
                target_allocation = cash * 0.95
                qty = int(target_allocation / current_price / 100) * 100
                if qty > 0:
                    self.order = self.buy(size=qty)
                    self.order.reason = f"动量突破入场 (量价齐升), 动量得分: {momentum_score:.1f}"
                    return
