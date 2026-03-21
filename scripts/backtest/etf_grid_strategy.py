import backtrader as bt
from datetime import datetime

class ETFGridMeanReversionStrategy(bt.Strategy):
    """
    专门针对宽基 ETF (如创业板 SZ.159915) 设计的左侧均值回归与动态网格策略。
    放弃趋势跟踪（均线金叉死叉），利用 ETF "跌不穿底"的特性，采用越跌越买的左侧建仓法。
    支持分批卖出（网格平仓）或整体反弹止盈。
    """
    params = (
        ('rsi_period', 14),
        ('rsi_oversold', 25),    # 极度超卖 RSI 阈值
        ('boll_period', 20),
        ('boll_dev', 2.0),
        ('boll_drop_pct', 0.02), # 跌破布林带下轨 2%
        
        # 网格与仓位管理参数
        ('grid_drop_pct', 0.03), # 每次加仓要求比上次买入价低 3%
        ('grid_profit_pct', 0.03), # 每次网格反弹 3% 卖出一批（分批卖出）
        ('take_profit_pct', 0.04), # 整体仓位的目标利润 4% (清仓止盈)
        ('max_tranches', 4),     # 最多允许分 4 批建仓（防弹衣）
        ('market', 'A'),         # 'A' or 'HK'
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        
        # 仅在小时线上运行
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_period)
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=self.params.boll_period, devfactor=self.params.boll_dev)

        # 趋势判断均线 (用于趋势追入再入场)
        self.sma20  = bt.indicators.SMA(self.datas[0], period=20)
        self.sma60  = bt.indicators.SMA(self.datas[0], period=60)
        self.sma120 = bt.indicators.SMA(self.datas[0], period=120)
        
        # 记录上次卖出后价格的最低点，用于判断是否形成新的趋势
        self._last_exit_price = None
        self._trend_breakout_used = False  # 一个牛市阶段只追一次
        self._is_trend_position = False    # 标记当前仓位是否为趋势买入（若是，就不走固定止盈）
        self._trailing_stop_price = None   # 动态跟踪止盈价
        
        # 网格状态管理
        self.order = None
        self.buy_tranches = []  # 记录每一批次的买入 (price, qty)
        
        # 绘图标记
        self.buy_markers = []
        self.sell_markers = []
        self.trade_log = []      # (dt, action, price, qty, reason)
        self.trade_count = 0
        
        # 资金动用记录
        self.max_capital_deployed = 0.0

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
            
        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            reason = getattr(order, 'reason', "N/A")
            if order.isbuy():
                action = "BUY"
                # 记录这批买入的价格和数量
                self.buy_tranches.append((order.executed.price, order.executed.size))
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                self.last_buy_date = dt.date()
                
                # 记录最大资金动用量 (持仓数量 * 当前价格，或按成本计算均可)
                # 这里我们记录持有的总成本
                total_cost = sum(price * qty for price, qty in self.buy_tranches)
                self.max_capital_deployed = max(self.max_capital_deployed, total_cost)
                
                print(f"{dt} - [网格建仓] 成功买入, 价格: {order.executed.price:.3f}, 数量: {order.executed.size}, 当前批次: {len(self.buy_tranches)}/{self.params.max_tranches}")
                
            elif order.issell():
                self.sell_markers.append((dt, order.executed.price))
                if self.position.size == 0:
                    # 全仓平仓模式，清空记录
                    self.buy_tranches = []
                    self._is_trend_position = False # 重置趋势标记
                    self._on_full_exit()  # 重置趋势追入标志，允许下次牛市再入场
                    print(f"{dt} - [网格清仓] 成功全部卖出, 价格: {order.executed.price:.3f}")
                else:
                    # 分批平仓，移除最后一批
                    if self.buy_tranches:
                        sold_tranche = self.buy_tranches.pop()
                        print(f"{dt} - [网格分批止盈] 成功卖出批次, 价格: {order.executed.price:.3f}, 数量: {abs(order.executed.size)}")
                
            # Log all completed trades (OUTSIDE the sell block)
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
        
        # 1. 卖出逻辑 (网格分批止盈或整体均值回归止盈)
        if self.position:
            avg_cost = self.position.price
            profit_pct = (current_price - avg_cost) / avg_cost
            
            sell_signal = False
            sell_reason = ""
            sell_size = 0
            
            current_date = self.datas[0].datetime.date(0)
            if self.params.market == 'HK':
                can_sell = True
            else:
                can_sell = getattr(self, 'last_buy_date', None) != current_date
            
            # --- 核心改进：动态跟踪止盈 ---
            
            # 如果当前利润已经超过目标利润 (4%)，开启/更新 跟踪止盈
            if profit_pct >= self.params.take_profit_pct:
                potential_stop = current_price * 0.95 # 允许从最高点回撤 5%
                if self._trailing_stop_price is None or potential_stop > self._trailing_stop_price:
                    if self._trailing_stop_price is None:
                        print(f"{dt} - [跟踪止盈] 开启: 当前利润 {profit_pct*100:.1f}%, 初始动态止盈线 {potential_stop:.3f}")
                    self._trailing_stop_price = potential_stop
            
            # 退出检票 A：触发跟踪止盈 (价格打破最高点回撤线)
            if self._trailing_stop_price is not None and current_price < self._trailing_stop_price:
                sell_signal = True
                sell_size = self.position.size
                sell_reason = f"触发动态跟踪止盈 (回撤5%), 锁定收益: {profit_pct*100:.1f}%"
                
            # 退出检票 B：趋势破位 (仅针对趋势单，作为双重保障)
            elif self._is_trend_position and (self.sma20[0] < self.sma60[0] or current_price < self.sma60[0]):
                sell_signal = True
                sell_size = self.position.size
                sell_reason = f"趋势破位止损 (SMA20<60), 收益: {profit_pct*100:.1f}%"

            # 退出检票 C：左侧网格止盈 (非趋势单，且未开启跟踪止盈时，触及布林上轨)
            elif not self._is_trend_position and self._trailing_stop_price is None:
                if current_price >= self.boll.lines.top[0] and profit_pct > 0.01:
                    sell_signal = True
                    sell_size = self.position.size
                    sell_reason = "触及布林上轨止盈 (网格模式), 全仓平仓"
            
            # --- 网格分批止盈 (仅限多批次重仓状态) ---
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

        # 2. 买入逻辑 (左侧网格建仓)
        
        # 避免 A 股早盘冲高回落的 T+1 风险，限制在下午建仓 (可根据需要开启或关闭)
        if dt.time().hour < 13:
            return
            
        current_tranches = len(self.buy_tranches)
        
        # 检查是否还有剩余子弹
        if current_tranches < self.params.max_tranches:
            buy_signal = False
            buy_reason = ""
            
            # 场景一：首仓建仓 (空仓状态)
            if current_tranches == 0:
                # RSI 极度超卖 (< 25) 或 跌破布林带下轨 2%
                boll_bot = self.boll.lines.bot[0]
                if self.rsi[0] < self.params.rsi_oversold or current_price <= boll_bot * (1 - self.params.boll_drop_pct):
                    buy_signal = True
                    buy_reason = f"首仓: RSI超卖({self.rsi[0]:.1f})或跌破下轨2%"
            
            # 场景二：网格加仓 (已被套状态)
            else:
                last_buy_price, _ = self.buy_tranches[-1]
                drop_from_last = (last_buy_price - current_price) / last_buy_price
                
                # 每跌 3% 加仓一次 (马丁格尔变种，不需要RSI超卖也可加仓)
                if drop_from_last >= self.params.grid_drop_pct:
                    buy_signal = True
                    buy_reason = f"网格加仓(第{current_tranches+1}批): 距离上次买入下跌 {drop_from_last*100:.1f}%"

            if buy_signal:
                cash = self.broker.getcash()
                
                # 马丁格尔变种：越跌买得越多。
                if self.params.market == 'HK':
                    # 港股杠杆标的更激进
                    allocation_map = {0: 0.50, 1: 0.20, 2: 0.15, 3: 0.15}
                else:
                    # A股标的维持 35%
                    allocation_map = {0: 0.35, 1: 0.25, 2: 0.20, 3: 0.20}
                target_cash_use = self.broker.getvalue() * allocation_map[current_tranches]
                
                # 确保不超过可用现金
                target_cash_use = min(target_cash_use, cash * 0.95)
                
                qty = int(target_cash_use / current_price / 100) * 100
                
                if qty > 0:
                    print(f"{dt} - 触发买入: {buy_reason}, 计划买入 {qty} 股")
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason

        # 3. 趋势突破追入（防止错过牛市）—— 空仓 + 均线多头排列 + RSI 适中
        if current_tranches == 0 and not self._trend_breakout_used:
            in_uptrend = (
                self.sma20[0] > self.sma60[0] > self.sma120[0]  # 均线多头排列
                and current_price > self.sma20[0]                # 价格在20均线上方
                and 48 < self.rsi[0] < 85                        # RSI 放宽到 85 (激进追涨)
                and current_price > self.boll.lines.mid[0]       # 价格在布林中轨上方
            )
            if in_uptrend:
                cash = self.broker.getcash()
                # 趋势追入仅用 15% 仓位，较保守
                target_cash_use = min(self.broker.getvalue() * 0.15, cash * 0.95)
                qty = int(target_cash_use / current_price / 100) * 100
                if qty > 0:
                    buy_reason = f"趋势追入: 均线多头排列, RSI={self.rsi[0]:.1f}, 价格在中轨上方"
                    print(f"{dt} - 触发趋势买入: {buy_reason}, 计划买入 {qty} 股")
                    self.order = self.buy(size=qty)
                    self.order.reason = buy_reason
                    self._trend_breakout_used = True  # 本轮牛市只追一次
                    self._is_trend_position = True    # 标记为趋势单

    def _on_full_exit(self):
        """Called when position is fully closed to reset breakout flag."""
        self._trend_breakout_used = False
        self._trailing_stop_price = None # 复位跟踪止盈
        self._last_exit_price = self.dataclose[0]
