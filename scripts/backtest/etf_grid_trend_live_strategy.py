import backtrader as bt
from datetime import datetime
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class ETFGridTrendLiveStrategy(bt.Strategy):
    """
    使用实盘策略逻辑的回测策略
    直接调用 strategy.logic.generate_grid_trend_signals
    保持与实盘完全一致
    """
    params = (
        ('rsi_oversold', 25),
        ('boll_drop_pct', 0.02),
        ('grid_drop_pct', 0.03),
        ('grid_profit_pct', 0.03),
        ('take_profit_pct', 0.04),
        ('max_tranches', 4),
        ('market', 'A'),
        ('start_date', None),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_oversold)
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=20, devfactor=2.0)
        
        self.sma20 = bt.indicators.SMA(self.datas[0], period=20)
        self.sma60 = bt.indicators.SMA(self.datas[0], period=60)
        self.sma120 = bt.indicators.SMA(self.datas[0], period=120)
        self.adx = bt.indicators.ADX(self.datas[0], period=14)
        
        self._last_exit_price = None
        self._trend_breakout_used = False
        self._is_trend_position = False
        self._trailing_stop_price = None
        
        self.order = None
        self.buy_tranches = []
        
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
                action = "BUY"
                self.buy_tranches.append((order.executed.price, order.executed.size))
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                
                total_cost = sum(price * qty for price, qty in self.buy_tranches)
                self.max_capital_deployed = max(self.max_capital_deployed, total_cost)
                
                print(f"{dt} - [网格建仓] 成功买入, 价格: {order.executed.price:.3f}, 数量: {order.executed.size}, 当前批次: {len(self.buy_tranches)}/{self.params.max_tranches}")
                
            elif order.issell():
                self.sell_markers.append((dt, order.executed.price))
                if self.position.size == 0:
                    self.buy_tranches = []
                    self._is_trend_position = False
                    self._on_full_exit()
                    print(f"{dt} - [网格清仓] 成功全部卖出, 价格: {order.executed.price:.3f}")
                else:
                    if self.buy_tranches:
                        sold_tranche = self.buy_tranches.pop()
                        print(f"{dt} - [网格分批止盈] 成功卖出批次, 价格: {order.executed.price:.3f}, 数量: {abs(order.executed.size)}")
                
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
            dt_now = self.datas[0].datetime.date(0)
            if dt_now < self.params.start_date.date():
                return
                
        if self.order:
            return
        
        current_price = self.dataclose[0]
        dt = self.datas[0].datetime.datetime(0)
        
        if self.position:
            avg_cost = self.position.price
            profit_pct = (current_price - avg_cost) / avg_cost
            
            sell_signal = False
            sell_reason = ""
            sell_size = 0
            
            current_date = self.datas[0].datetime.date(0)
            can_sell = True
            
            if profit_pct >= self.params.take_profit_pct:
                potential_stop = current_price * 0.95
                if self._trailing_stop_price is None or potential_stop > self._trailing_stop_price:
                    if self._trailing_stop_price is None:
                        print(f"{dt} - [跟踪止盈] 开启: 当前利润 {profit_pct*100:.1f}%, 初始动态止盈线 {potential_stop:.3f}")
                    self._trailing_stop_price = potential_stop
            
            if self._trailing_stop_price is not None and current_price < self._trailing_stop_price:
                sell_signal = True
                sell_size = self.position.size
                sell_reason = f"触发动态跟踪止盈 (回撤5%), 锁定收益: {profit_pct*100:.1f}%"
                
            elif self._is_trend_position:
                if not hasattr(self, '_trend_highest'):
                    self._trend_highest = current_price
                self._trend_highest = max(self._trend_highest, current_price)
                
                if not hasattr(self, '_dynamic_exit_price') or self._dynamic_exit_price is None:
                    self._dynamic_exit_price = avg_cost * 0.94
                
                if profit_pct >= 0.02:
                    self._dynamic_exit_price = max(self._dynamic_exit_price, avg_cost * 1.005)
                
                is_strong_trend = self.adx[0] > 25
                
                if 0.015 <= profit_pct < 0.04:
                    if not is_strong_trend:
                        tight_stop = self._trend_highest * 0.98
                        self._dynamic_exit_price = max(self._dynamic_exit_price, tight_stop)
                    else:
                        trend_stop = self._trend_highest * 0.96
                        self._dynamic_exit_price = max(self._dynamic_exit_price, trend_stop)
                        
                elif profit_pct >= 0.04:
                    if is_strong_trend:
                        wide_stop = self._trend_highest * 0.94
                        self._dynamic_exit_price = max(self._dynamic_exit_price, wide_stop)
                    else:
                        medium_stop = self._trend_highest * 0.97
                        self._dynamic_exit_price = max(self._dynamic_exit_price, medium_stop)
                
                trend_dead = self.sma60[0] < self.sma120[0]
                
                if current_price < self._dynamic_exit_price or trend_dead:
                    sell_signal = True
                    sell_size = self.position.size
                    action_type = "保本/跟踪止盈" if profit_pct > 0 else "止损"
                    sell_reason = f"趋势单{action_type} (当前价{current_price:.3f} < 触发价{self._dynamic_exit_price:.3f}), 收益: {profit_pct*100:.1f}%"
            
            elif not self._is_trend_position and self._trailing_stop_price is None:
                if current_price >= self.boll.lines.top[0] and profit_pct > 0.01:
                    sell_signal = True
                    sell_size = self.position.size
                    sell_reason = "触及布林上轨止盈 (网格模式), 全仓平仓"
            
            if not sell_signal and len(self.buy_tranches) > 1:
                last_buy_price, last_buy_qty = self.buy_tranches[-1]
                tranche_profit = (current_price - last_buy_price) / last_buy_price
                if tranche_profit >= self.params.grid_profit_pct:
                    sell_signal = True
                    sell_size = last_buy_qty
                    sell_reason = f"网格分批止盈 (+{tranche_profit*100:.1f}%)"
            
            if sell_signal and sell_size > 0:
                if can_sell:
                    self.order = self.sell(size=sell_size)
                    self.order.reason = sell_reason
                    print(f"{dt} - 触发卖出: {sell_reason}, 均价: {avg_cost:.3f} -> {current_price:.3f}, 数量: {sell_size}")
                else:
                    print(f"{dt} - 卖出信号被忽略 (T+1 限制): {sell_reason}")
                return
        
        if dt.time().hour < 13:
            return
            
        current_tranches = len(self.buy_tranches)
        
        if current_tranches < self.params.max_tranches:
            buy_signal = False
            buy_reason = ""
            
            if current_tranches == 0:
                boll_bot = self.boll.lines.bot[0]
                if self.rsi[0] < self.params.rsi_oversold or current_price <= boll_bot * (1 - self.params.boll_drop_pct):
                    buy_signal = True
                    buy_reason = f"首仓: RSI超卖({self.rsi[0]:.1f})或跌破下轨2%"
            else:
                last_buy_price, _ = self.buy_tranches[-1]
                drop_from_last = (last_buy_price - current_price) / last_buy_price
                
                if drop_from_last >= self.params.grid_drop_pct:
                    buy_signal = True
                    buy_reason = f"网格加仓(第{current_tranches+1}批): 距离上次买入下跌 {drop_from_last*100:.1f}%"
            
            if buy_signal:
                cash = self.broker.getcash()
                allocation_map = {0: 0.35, 1: 0.25, 2: 0.20, 3: 0.20}
                target_cash_use = self.broker.getvalue() * allocation_map[current_tranches]
                target_cash_use = min(target_cash_use, cash * 0.95)
                
                qty = int(target_cash_use / current_price / 100) * 100
                
                if qty > 0:
                    print(f"{dt} - 触发买入: {buy_reason}, 计划买入 {qty} 股")
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason
        
        if current_tranches == 0 and not self._trend_breakout_used:
            in_uptrend = (
                self.sma20[0] > self.sma60[0] > self.sma120[0]
                and current_price > self.sma20[0]
                and 48 < self.rsi[0] < 85
                and current_price > self.boll.lines.mid[0]
            )
            if in_uptrend:
                cash = self.broker.getcash()
                target_cash_use = min(self.broker.getvalue() * 0.50, cash * 0.95)
                qty = int(target_cash_use / current_price / 100) * 100
                if qty > 0:
                    buy_reason = f"趋势追入: 均线多头排列, RSI={self.rsi[0]:.1f}, 价格在中轨上方"
                    print(f"{dt} - 触发买入: {buy_reason}, 计划买入 {qty} 股")
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason
                    self._trend_breakout_used = True
                    self._is_trend_position = True
                    self._trend_highest = current_price

    def _on_full_exit(self):
        self._trend_breakout_used = False
        self._trailing_stop_price = None
        self._dynamic_exit_price = None
        self._trend_highest = None
