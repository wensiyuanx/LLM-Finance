from typing import List, Tuple, Dict
from database.models import TradeAction, MarketType
from sqlalchemy import select

class PortfolioManager:
    """
    Stage 3: Portfolio Manager
    Evaluates individual signals against global portfolio constraints:
    - Available buying power
    - Maximum allocation per asset
    - Overall market exposure
    Calculates specific position sizing (number of shares), accounting for Board Lots and T+1.
    """
    def __init__(self, db_session, market_type: str, config: dict):
        self.db = db_session
        self.market_type = market_type
        
        # --- Config Parsing ---
        global_config = config.get('global', {})
        
        self.max_concurrent_assets = global_config.get('max_concurrent_assets', 5)
        self.max_total_exposure_pct = global_config.get('max_total_exposure_pct', 0.90)
        self.max_position_pct = global_config.get('max_position_pct', 0.25)
        
        # --- Account State (will be loaded async) ---
        self.current_cash = 0.0
        self.holdings_value = 0.0
        self.active_holdings_count = 0
        self.total_value = 0.0

    @classmethod
    async def create(cls, db_session, market_type: str, config: dict):
        """
        Async factory method to create PortfolioManager with database queries.
        """
        instance = cls(db_session, market_type, config)
        await instance._load_account_state()
        return instance

    async def _load_account_state(self):
        """
        Load account state from database using async queries.
        """
        from database.models import UserWallet, Holding
        
        # Load wallet balance
        stmt = select(UserWallet).filter(UserWallet.market_type == self.market_type).execution_options(populate_existing=True)
        result = await self.db.execute(stmt)
        wallet = result.scalar_one_or_none()
        
        if not wallet:
            self.current_cash = 0.0
        else:
            self.current_cash = float(wallet.balance)
            
        # Load holdings
        stmt = select(Holding).filter(
            Holding.market_type == self.market_type,
            Holding.quantity > 0
        ).execution_options(populate_existing=True)
        result = await self.db.execute(stmt)
        holdings = result.scalars().all()
        
        self.holdings_value = 0.0
        self.active_holdings_count = len(holdings)
        for h in holdings:
            # Prefer last_price if available, fallback to avg_cost
            price = float(h.last_price) if getattr(h, 'last_price', None) and h.last_price > 0 else float(h.avg_cost)
            self.holdings_value += float(h.quantity) * price
            
        self.total_value = self.current_cash + self.holdings_value

    async def get_board_lot(self, code: str, market_type: MarketType) -> int:
        """
        Retrieves the board lot size for a stock, preferring cached values.
        """
        if not hasattr(self, 'board_lots'):
            self.board_lots = {}
            
        if code in self.board_lots:
            return self.board_lots[code]
            
        # Try database first
        from database.models import AssetMonitor
        from sqlalchemy import select
        
        # Async query to fetch asset
        stmt = select(AssetMonitor).filter(AssetMonitor.code == code)
        result = await self.db.execute(stmt)
        asset = result.scalar_one_or_none()
        
        if asset and asset.board_lot and asset.board_lot > 0:
            self.board_lots[code] = asset.board_lot
            return asset.board_lot

        # Fallback to Futu API
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
                    # Cache back to DB if possible
                    if asset:
                        asset.board_lot = lot_size
                        await self.db.commit()
                    return lot_size
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to fetch real lot_size for {code}, defaulting to 100: {e}")
            
        return 100 # Fallback

    async def evaluate_signals(self, signals_context: List[Dict]) -> List[dict]:
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
        # Buy signals for existing holdings (tranches_count > 0) get priority over new positions
        def sort_key(ctx):
            if ctx['action'] == TradeAction.BUY:
                score = ctx.get('score', 0.0)
                # Boost score significantly if it's a grid refill for an existing holding
                if ctx.get('tranches_count', 0) > 0:
                    score += 1000.0
                return score
            return float('inf')
            
        signals_context.sort(key=sort_key, reverse=True)
        
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
                # We DO NOT increment self.current_cash here.
                # In live trading, sell proceeds are not instantly available for the very next buy order
                # in the same evaluation loop. We only deduct current_held_val for exposure tracking.
                proceeds = sellable_qty * ctx['price']
                current_held_val -= proceeds
                
        # Calculate slot limits (Ensuring 10% cash reserve is respected globally)
        max_allowed_held_val = self.total_value * self.max_total_exposure_pct
        
        # Use actual holdings count if it exceeds the target concurrent limit,
        # to ensure we don't over-allocate cash to a few assets when many are held.
        num_slots = max(self.max_concurrent_assets, self.active_holdings_count)
        slot_value = max_allowed_held_val / max(1, num_slots)
        current_active_holdings = self.active_holdings_count

        # Process Buys
        for ctx in signals_context:
            if ctx['action'] == TradeAction.BUY:
                tranches_count = ctx.get('tranches_count', 0)
                
                # Check slot limit for NEW positions
                if tranches_count == 0:
                    if current_active_holdings >= self.max_concurrent_assets:
                        import logging
                        logging.getLogger(__name__).info(
                            f"[{ctx['code']}] Skipped BUY (Slot Limit Reached: {current_active_holdings}/{self.max_concurrent_assets})"
                        )
                        continue
                        
                is_etf = ctx.get('is_etf', False)
                if is_etf:
                    # 根据市场设置不同首仓比例（仅限 ETF）
                    market_type = ctx.get('market_type')
                    if market_type == MarketType.HK_SHARE:
                        allocation_map = {0: 0.50, 1: 0.20, 2: 0.15, 3: 0.15}
                    else:
                        allocation_map = {0: 0.35, 1: 0.25, 2: 0.20, 3: 0.20}
                    
                    is_trend_entry = ctx.get('is_trend_entry', False)
                    
                    if tranches_count >= 4 and not is_trend_entry:
                        continue
                    
                    if is_trend_entry:
                        # 趋势追入仓位提高到 50%，最大化牛市收益
                        target_ratio = 0.50
                    else:
                        target_ratio = allocation_map.get(tranches_count, 0.0)
                        
                    # ETF grid allocation is based on the SLOT value, not total value
                    target_allocation = slot_value * target_ratio
                else:
                    # 普通股票维持标准仓位比例
                    # Check if already holding enough
                    current_holding_val = ctx.get('current_holding_val', 0.0)
                    target_allocation = slot_value * self.max_position_pct
                    if current_holding_val >= target_allocation * 0.95:
                        continue
                
                # --- Global Exposure Check ---
                remaining_exposure_quota = max_allowed_held_val - current_held_val
                if remaining_exposure_quota <= 0:
                    # Global exposure limit reached
                    continue
                
                # Allocation must not exceed global quota
                target_allocation = min(target_allocation, remaining_exposure_quota)

                available_to_allocate = min(target_allocation, self.current_cash)
                
                # Get board lot from context or DB or Futu
                board_lot = ctx.get('board_lot')
                if not board_lot:
                    board_lot = await self.get_board_lot(ctx['code'], ctx['market_type'])
                
                min_cost = ctx['price'] * board_lot
                
                # --- Dynamic Slot Merging for High-Priced Assets ---
                if available_to_allocate < min_cost:
                    # If a single slot isn't enough, see if we can "borrow" from empty slots
                    empty_slots = self.max_concurrent_assets - current_active_holdings
                    if empty_slots > 0:
                        # Max borrow up to what's actually in cash, but limit by empty slots
                        max_borrowable = empty_slots * slot_value
                        if (available_to_allocate + max_borrowable) >= min_cost and self.current_cash >= min_cost:
                            import logging
                            logging.getLogger(__name__).info(
                                f"[{ctx['code']}] Dynamic Slot Merge: Borrowing from {empty_slots} empty slots to meet min cost {min_cost:.2f}"
                            )
                            available_to_allocate = min(min_cost * 1.05, self.current_cash) # Allocate just enough for 1 lot + buffer

                # Cannot buy if available allocation is smaller than 1 lot
                if available_to_allocate < min_cost:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[{ctx['code']}] Skipped BUY (Insufficient allocation: {available_to_allocate:.2f} < 1 lot cost {min_cost:.2f})."
                    )
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
                    
                    # If this is a new position, increment the active holdings count
                    if tranches_count == 0:
                        current_active_holdings += 1

        return executable_orders