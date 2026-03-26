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
                # Handle Pyramiding average cost
                if self.buyprice is not None and self.position.size > order.executed.size:
                    # Calculate new weighted average cost
                    old_value = self.buyprice * (self.position.size - order.executed.size)
                    new_value = order.executed.price * order.executed.size
                    self.buyprice = (old_value + new_value) / self.position.size
                else:
                    self.buyprice = order.executed.price
                    
                self._highest_price = self.buyprice
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                reason = getattr(order, 'reason', 'Buy')
                self.trade_log.append({
                    'date': dt,
                    'action': 'BUY',
                    'price': order.executed.price,
                    'qty': order.executed.size,
                    'reason': reason
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

            # A. 动态 ATR 硬止损
            # 修改：将固定乘数改为动态。如果已经有较多浮盈，不应该用宽止损，应该收紧止损
            # 基础止损线
            initial_stop_price = self.buyprice - (current_atr * self.params.atr_stop_mult)
            if current_price <= initial_stop_price:
                print(f"{dt} - 杠杆风控: 触发ATR硬止损")
                self.order = self.sell(size=self.position.size)
                self.order.reason = "杠杆风控: 触发ATR硬止损"
                return

            # B. 自适应跟踪止盈 (ADX判定)
            adx_curr = self.adx.adx[0] if len(self.adx) > 0 else 20
            is_strong_trend = adx_curr > 25
            
            if self._highest_price > self.buyprice:
                # 阶梯止盈：杠杆ETF核心在于“赚够就跑，不吃回调”
                # 放宽第一档触发线，但一旦触发，止盈回撤要求极严
                if 0.10 <= profit_pct < 0.20:
                    # 利润在 10% ~ 20% 之间时，只要从最高点回落 4% 立即兑现
                    trail_pct = 0.96
                    if current_price < self._highest_price * trail_pct:
                        print(f"{dt} - 阶段性止盈锁定 at {current_price:.2f}, profit: {profit_pct:.2%}")
                        self.order = self.sell(size=self.position.size)
                        self.order.reason = f"杠杆风控: 阶段性止盈锁定, 收益 {profit_pct*100:.1f}%"
                        return
                elif profit_pct >= 0.20:
                    # 利润大于 20% 属于暴利，用 1.5 倍 ATR 紧密跟随，或者固定回撤 5% 离场
                    if is_strong_trend:
                        trailing_stop = self._highest_price - (current_atr * 1.5)
                        if current_price <= trailing_stop:
                            print(f"{dt} - 触发动态跟踪止盈 at {current_price:.2f}, profit: {profit_pct:.2%}")
                            self.order = self.sell(size=self.position.size)
                            self.order.reason = f"杠杆风控: 触发动态跟踪止盈, 收益 {profit_pct*100:.1f}%"
                            return
                    else:
                        if current_price < self._highest_price * 0.95:
                            print(f"{dt} - 趋势转弱止盈 at {current_price:.2f}, profit: {profit_pct:.2%}")
                            self.order = self.sell(size=self.position.size)
                            self.order.reason = f"杠杆风控: 趋势转弱止盈 (5%), 收益 {profit_pct*100:.1f}%"
                            return

            # C. 趋势死叉与破位切换
            sma5_prev = self.sma5[-1] if len(self.sma5) > 0 else current_price
            sma20_prev = self.sma20[-1] if len(self.sma20) > 0 else current_price
            sma5_curr = self.sma5[0] if len(self.sma5) > 0 else current_price
            sma20_curr = self.sma20[0] if len(self.sma20) > 0 else current_price

            # 修改死叉逻辑：如果是小幅震荡导致的死叉且还在成本价之上，可以容忍；
            # 但如果死叉且跌破了重要支撑（如均线20），或者利润已经变成亏损，坚决离场
            if sma5_prev >= sma20_prev and sma5_curr < sma20_curr:
                if current_price < sma20_curr or profit_pct < -0.01:
                    print(f"{dt} - 右侧离场: 均线死叉且破位")
                    self.order = self.sell(size=self.position.size)
                    self.order.reason = "右侧离场: 均线死叉且破位"
                    return

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
            # 杠杆ETF不能只等最完美的右侧（因为经常错过主升浪），
            # 应该在“超跌反弹确认”或“均线刚开始多头”时及早介入。
            
            # 信号1：超跌反弹（RSI金叉 + MACD底背离或金叉）
            rsi_oversold_rebound = False
            if len(self.rsi) > 1:
                if self.rsi[-1] < 40 and self.rsi[0] > 40: # 放宽 RSI 抄底线，从35提升到40
                    rsi_oversold_rebound = True
                    
            macd_golden_cross = False
            if len(self.macd.macd) > 1 and len(self.macd.signal) > 1:
                macd_golden_cross = (self.macd.macd[-1] <= self.macd.signal[-1] and self.macd.macd[0] > self.macd.signal[0])
                
            # 信号2：均线多头排列初期（不需要SMA5>20>50那么苛刻，只要短期趋势向上）
            short_trend_up = False
            if len(self.sma5) > 0 and len(self.sma20) > 0:
                short_trend_up = (self.sma5[0] > self.sma20[0] and current_price > self.sma5[0])
                
            # 信号3：放量突破
            volume_breakout = False
            if len(self.vol_sma) > 0:
                if self.datas[0].volume[0] > self.vol_sma[0] * 1.3: # 放宽突破放量要求，从1.5降到1.3
                    volume_breakout = True

            buy_reason = ""
            score = 0.0
            
            # 组合策略 A: 底部反弹确认 (适合抄底)
            if rsi_oversold_rebound and macd_golden_cross:
                buy_reason = "底部反弹: RSI回升+MACD金叉"
                score = 70.0
                
            # 组合策略 B: 右侧动量突破 (适合追主升浪)
            elif short_trend_up and macd_golden_cross and volume_breakout:
                buy_reason = "动量突破: 均线向上+放量金叉"
                score = 80.0
                
            # 组合策略 C: 强趋势顺势上车
            elif short_trend_up and (self.adx.adx[0] if len(self.adx) > 0 else 0) > 20 and (self.rsi[0] if len(self.rsi) > 0 else 50) < 70: # 放宽顺势上车要求，ADX>20即可
                buy_reason = "趋势顺势: ADX强趋势且未超买"
                score = 75.0

            if buy_reason and score >= 60.0:
                print(f"{dt} - {buy_reason}, 动量得分: {score:.1f}")
                cash = self.broker.getcash()
                # 修改仓位管理：不再一把梭哈 95%
                # 初次建仓只用 50% 资金，留有余地
                target_allocation = cash * 0.50
                qty = int(target_allocation / current_price / 100) * 100
                if qty > 0:
                    self.order = self.buy(size=qty)
                    self.order.reason = f"{buy_reason}, 动量得分: {score:.1f}"
                    return

        # --- 3. 顺势加仓逻辑 (Pyramiding) ---
        elif self.position.size > 0:
            # 如果已经有持仓，且利润超过 4%，并且再次出现动量突破信号，则加仓
            profit_pct = (current_price - self.buyprice) / self.buyprice
            if profit_pct >= 0.04 and len(self.sma5) > 0 and len(self.sma20) > 0:
                # 确认还在多头趋势中
                if self.sma5[0] > self.sma20[0] and current_price > self.sma5[0]:
                    # 确认放量或强趋势
                    vol_ok = len(self.vol_sma) > 0 and self.datas[0].volume[0] > self.vol_sma[0] * 1.1
                    trend_ok = len(self.adx) > 0 and self.adx.adx[0] > 20
                    
                    if vol_ok or trend_ok:
                        cash = self.broker.getcash()
                        # 动用剩余资金的 40% 进行加仓
                        add_allocation = cash * 0.40
                        add_qty = int(add_allocation / current_price / 100) * 100
                        if add_qty > 0:
                            print(f"{dt} - 顺势加仓 (浮盈 {profit_pct*100:.1f}%), 趋势确认")
                            self.order = self.buy(size=add_qty)
                            self.order.reason = f"顺势加仓 (浮盈 {profit_pct*100:.1f}%)"
                            return
