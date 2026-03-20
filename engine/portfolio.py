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
    def __init__(self, db_session, current_cash: float, total_value: float = None, max_position_pct: float = 0.25):
        self.db = db_session
        self.current_cash = current_cash
        self.total_value = total_value if total_value is not None else current_cash
        self.max_position_pct = max_position_pct

    def get_board_lot(self, code: str, market_type: MarketType) -> int:
        """Helper to get accurate lot sizes. In real system, fetch from Futu."""
        if market_type == MarketType.A_SHARE:
            # STAR market / A-shares are strictly 100
            return 100
        elif market_type == MarketType.HK_SHARE:
            # HK shares have variable lots. Mocking default 100.
            # Real impl would cache Futu's get_market_snapshot lot_size
            return 100
        return 1

    def evaluate_signals(self, signals_context: List[Dict]) -> List[dict]:
        """
        Takes context from strategy and filter/sizes them.
        signals_context: list of dicts with {code, market_type, action, price, sellable_qty, reason, score}
        Returns: list of dicts with {code, action, quantity, price, reason}
        """
        executable_orders = []
        
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
                self.current_cash += (sellable_qty * ctx['price'])
                
        # Process Buys
        for ctx in signals_context:
            if ctx['action'] == TradeAction.BUY:
                # Use total portfolio value to determine position size limit, not just remaining cash
                target_allocation = self.total_value * self.max_position_pct
                
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
                        "reason": ctx['reason']
                    })
                    self.current_cash -= (lot_qty * ctx['price'])

        return executable_orders