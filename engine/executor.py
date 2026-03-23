import uuid
from sqlalchemy.orm import Session
from database.models import TradeRecord, TradeAction
from futu import TrdSide, OrderType, TrdEnv

import logging
logger = logging.getLogger(__name__)

class OrderExecutor:
    def __init__(self, db_session: Session, futu_client=None, simulate=True):
        self.db = db_session
        self.futu = futu_client
        self.simulate = simulate

    def execute_trade(self, user_id: int, code: str, action: TradeAction, price: float, quantity: float, reason: str, is_trend_entry: bool = False):
        """
        Executes a trade order and logs it to the database with user_id.
        """
        if action == TradeAction.HOLD:
            return None

        order_id = str(uuid.uuid4())
        status = "SUBMITTED"

        if not self.simulate:
            # Real-order path: guard against unconfigured trade_ctx to prevent
            # wallet/holding state from being updated without an actual order.
            if self.futu is None or self.futu.trade_ctx is None:
                logger.error(f"simulate=False but Futu trade_ctx is not initialised. Order REJECTED ({action.name} {code}).")
                return None

            from futu import RET_OK
            trd_side = TrdSide.BUY if action == TradeAction.BUY else TrdSide.SELL
            
            # --- Live order implementation ---
            ret, data = self.futu.trade_ctx.place_order(
                price=price, qty=quantity, code=code,
                trd_side=trd_side, order_type=OrderType.NORMAL,
                trd_env=TrdEnv.REAL
            )
            
            if ret == RET_OK:
                order_id = data['order_id'][0]
                status = "SUBMITTED_LIVE"
                logger.info(f"[LIVE] Order submitted successfully: {order_id}")
            else:
                logger.error(f"Futu place_order failed for {code}: {data}")
                return None
        else:
            # Simulation mode
            status = "SIMULATED_FILLED"

        # Record trade in DB
        trade_record = TradeRecord(
            user_id=user_id,
            code=code,
            action=action,
            price=price,
            quantity=quantity,
            order_id=order_id,
            status=status,
            reason=reason
        )
        self.db.add(trade_record)
        
        # NOTE: Do NOT call self.db.commit() here! 
        # Committing here breaks transaction atomicity for the caller 
        # (who needs to update Wallet and Holding in the same transaction).
        # We flush to get the ID instead.
        self.db.flush()

        logger.info(f"[{status}] Executed {action.name} {quantity} shares of {code} at {price}. Reason: {reason}")
        return trade_record
