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
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        
        # 仅在小时线上运行
        self.rsi = bt.indicators.RSI_Safe(self.datas[0], period=self.params.rsi_period)
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=self.params.boll_period, devfactor=self.params.boll_dev)
        
        # 网格状态管理
        self.order = None
        self.buy_tranches = []  # 记录每一批次的买入 (price, qty)
        
        # 绘图标记
        self.buy_markers = []
        self.sell_markers = []
        self.trade_count = 0
        
        # 资金动用记录
        self.max_capital_deployed = 0.0

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
            
        if order.status in [order.Completed]:
            dt = bt.num2date(order.executed.dt)
            if order.isbuy():
                # 记录这批买入的价格和数量
                self.buy_tranches.append((order.executed.price, order.executed.size))
                self.buy_markers.append((dt, order.executed.price))
                self.trade_count += 1
                
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
                    print(f"{dt} - [网格清仓] 成功全部卖出, 价格: {order.executed.price:.3f}")
                else:
                    # 分批平仓，移除最后一批
                    if self.buy_tranches:
                        sold_tranche = self.buy_tranches.pop()
                        print(f"{dt} - [网格分批止盈] 成功卖出批次, 价格: {order.executed.price:.3f}, 数量: {abs(order.executed.size)}")
        
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
            
            # 止盈条件 A：整体仓位达到网格目标利润 (如 4%)，全部清仓
            if profit_pct >= self.params.take_profit_pct:
                sell_signal = True
                sell_size = self.position.size
                sell_reason = f"达到网格整体止盈目标 (+{profit_pct*100:.1f}%)，全仓平仓"
                
            # 止盈条件 B：左侧超跌反弹触及布林带上轨，落袋为安，全部清仓
            elif current_price >= self.boll.lines.top[0] and profit_pct > 0.01:
                sell_signal = True
                sell_size = self.position.size
                sell_reason = "超跌反弹触及布林上轨止盈，全仓平仓"
            
            # 止盈条件 C：网格分批止盈 (如果最后一批买入已经盈利超过网格目标)
            elif len(self.buy_tranches) > 1:
                last_buy_price, last_buy_qty = self.buy_tranches[-1]
                tranche_profit = (current_price - last_buy_price) / last_buy_price
                if tranche_profit >= self.params.grid_profit_pct:
                    sell_signal = True
                    sell_size = last_buy_qty
                    sell_reason = f"最新批次反弹盈利超过 {self.params.grid_profit_pct*100:.1f}%，分批止盈"
                
            if sell_signal and sell_size > 0:
                self.order = self.sell(size=sell_size)
                print(f"{dt} - 触发卖出: {sell_reason}, 均价: {avg_cost:.3f} -> 现价: {current_price:.3f}, 卖出数量: {sell_size}")
                return

        # 2. 买入逻辑 (左侧网格建仓)
        
        # 避免 A 股早盘冲高回落的 T+1 风险，限制在下午建仓 (可根据需要开启或关闭)
        if dt.time().hour < 14:
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
                
                # 马丁格尔变种：越跌买得越多。首仓 20%，第二仓 20%，第三仓 30%，第四仓 30%
                allocation_map = {0: 0.20, 1: 0.20, 2: 0.30, 3: 0.30}
                target_cash_use = self.broker.getvalue() * allocation_map[current_tranches]
                
                # 确保不超过可用现金
                target_cash_use = min(target_cash_use, cash * 0.95)
                
                qty = int(target_cash_use / current_price / 100) * 100
                
                if qty > 0:
                    print(f"{dt} - 触发买入: {buy_reason}, 计划买入 {qty} 股")
                    self.order = self.buy(size=qty)
