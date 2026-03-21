from typing import List, Tuple, Dict
from database.models import TradeAction, MarketType

class PortfolioManager:
    """
    Stage 3: Portfolio Manager
    Evaluates individual signals against global portfolio constraints:
    - Available buying power
    - Maximum allocation per asset
    - Overall market exposure
    Calculates specific position sizing (number of shares), accounting for Board Lots and T+1.
    """
    def __init__(self, db_session, current_cash: float, total_value: float = None, max_position_pct: float = 0.25, max_total_exposure_pct: float = 0.90):
        self.db = db_session
        self.current_cash = current_cash
        self.total_value = total_value if total_value is not None else current_cash
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct

    def get_board_lot(self, code: str, market_type: MarketType) -> int:
        """
        获取标的真实的每手股数。
        实盘中必须调用 Futu API 的 get_stock_basicinfo。
        """
        if not hasattr(self, 'board_lots'):
            self.board_lots = {}
            
        if code in self.board_lots:
            return self.board_lots[code]
            
        try:
            from data.futu_client import FutuClient
            futu = FutuClient()
            if futu.connect():
                from futu import RET_OK
                ret, data = futu.quote_ctx.get_stock_basicinfo(market=code.split('.')[0], stock_type='STOCK', code_list=[code])
                futu.close()
                if ret == RET_OK and not data.empty:
                    lot_size = int(data['lot_size'].iloc[0])
                    self.board_lots[code] = lot_size
                    return lot_size
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to fetch real lot_size for {code}, defaulting to 100: {e}")
            
        return 100 # Fallback

    def evaluate_signals(self, signals_context: List[Dict]) -> List[dict]:
        """
        Takes context from strategy and filter/sizes them.
        signals_context: list of dicts with {code, market_type, action, price, sellable_qty, reason, score}
        Returns: list of dicts with {code, action, quantity, price, reason}
        """
        executable_orders = []
        
        # Track total exposure
        current_held_val = self.total_value - self.current_cash
        max_allowed_held_val = self.total_value * self.max_total_exposure_pct
        
        # Sort signals by score descending (highest priority first)
        # Sell signals should have absolute highest priority to free up cash
        signals_context.sort(key=lambda x: x.get('score', 0.0) if x['action'] == TradeAction.BUY else float('inf'), reverse=True)
        
        # Process Sells first to free up cash
        for ctx in signals_context:
            if ctx['action'] == TradeAction.SELL:
                sellable_qty = ctx['sellable_qty']
                if sellable_qty <= 0:
                    continue # T+1 restricted, cannot sell yet
                
                executable_orders.append({
                    "code": ctx['code'],
                    "action": ctx['action'],
                    "quantity": sellable_qty,
                    "price": ctx['price'],
                    "reason": ctx['reason']
                })
                # Pseudo cash increment (ignoring commissions for now)
                proceeds = sellable_qty * ctx['price']
                self.current_cash += proceeds
                current_held_val -= proceeds
                
        # Process Buys
        for ctx in signals_context:
            if ctx['action'] == TradeAction.BUY:
                is_etf = ctx.get('is_etf', False)
                if is_etf:
                    # 根据市场设置不同首仓比例（仅限 ETF）
                    market_type = ctx.get('market_type')
                    if market_type == MarketType.HK_SHARE:
                        allocation_map = {0: 0.50, 1: 0.20, 2: 0.15, 3: 0.15}
                    else:
                        allocation_map = {0: 0.35, 1: 0.25, 2: 0.20, 3: 0.20}
                    
                    tranches_count = ctx.get('tranches_count', 0)
                    if tranches_count >= 4:
                        continue
                    
                    target_ratio = allocation_map.get(tranches_count, 0.0)
                    target_allocation = self.total_value * target_ratio
                else:
                    # 普通股票维持标准仓位比例 (如 25%)
                    # Check if already holding enough
                    current_holding_val = ctx.get('current_holding_val', 0.0)
                    target_allocation = self.total_value * self.max_position_pct
                    if current_holding_val >= target_allocation * 0.95:
                        continue
                
                # --- Global Exposure Check ---
                remaining_exposure_quota = max_allowed_held_val - current_held_val
                if remaining_exposure_quota <= 0:
                    # Global limit reached
                    continue
                
                # Allocation must not exceed global quota
                target_allocation = min(target_allocation, remaining_exposure_quota)

                # Check if we have enough cash for this allocation
                available_to_allocate = min(target_allocation, self.current_cash)
                
                board_lot = self.get_board_lot(ctx['code'], ctx['market_type'])
                min_cost = ctx['price'] * board_lot
                
                # Cannot buy if available allocation is smaller than 1 lot
                if available_to_allocate < min_cost:
                    continue
                    
                # Calculate quantity (round down to nearest board lot)
                raw_qty = available_to_allocate / ctx['price']
                lot_qty = int(raw_qty // board_lot) * board_lot
                
                if lot_qty > 0:
                    executable_orders.append({
                        "code": ctx['code'],
                        "action": ctx['action'],
                        "quantity": lot_qty,
                        "price": ctx['price'],
                        "reason": ctx['reason'],
                        "is_trend_entry": ctx.get('is_trend_entry', False)
                    })
                    cost = lot_qty * ctx['price']
                    self.current_cash -= cost
                    current_held_val += cost

        return executable_orders