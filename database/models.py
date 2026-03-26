from sqlalchemy import Column, Integer, String, Float, DateTime, Enum, UniqueConstraint
from .db import Base
from datetime import datetime, timedelta
import enum

def get_beijing_time():
    """Returns the current absolute time in UTC+8 (Beijing Time)"""
    return datetime.utcnow() + timedelta(hours=8)

class TradeAction(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class MarketType(enum.Enum):
    A_SHARE = "A_SHARE"
    HK_SHARE = "HK_SHARE"

class AssetMonitor(Base):
    __tablename__ = "asset_monitor"
    __table_args__ = (UniqueConstraint('user_id', 'code', name='uq_asset_user_code'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False) # e.g. 'SZ.159915', 'AAPL'
    market_type = Column(Enum(MarketType), nullable=False)
    is_active = Column(Integer, default=1) # 1 for active, 0 for inactive
    is_etf = Column(Integer, default=0) # 1 for ETF, 0 for regular stock
    is_leveraged = Column(Integer, default=0) # 1 for leveraged/inverse ETF, 0 for normal
    
    last_price = Column(Float, nullable=True) # Real-time price from websocket
    last_updated = Column(DateTime, nullable=True) # Last time price was updated
    board_lot = Column(Integer, default=100) # Minimum trading unit (e.g. 100 for A-Share)
    
    created_at = Column(DateTime, default=get_beijing_time)

class KLineData(Base):
    __tablename__ = "kline_data"
    __table_args__ = (UniqueConstraint('user_id', 'code', 'time_key', 'timeframe', name='uq_kline_user_code_time_tf'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False) # e.g. 'HK.00700'
    time_key = Column(DateTime, index=True, nullable=False) # update time
    timeframe = Column(String(10), default='1d', index=True, nullable=False) # e.g. '1d', '60m'
    open_price = Column(Float, nullable=False)
    close_price = Column(Float, nullable=False)
    high_price = Column(Float, nullable=False)
    low_price = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    turnover = Column(Float, nullable=False)
    
    created_at = Column(DateTime, default=get_beijing_time)

class TradeRecord(Base):
    __tablename__ = "trade_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False)
    action = Column(Enum(TradeAction), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    order_id = Column(String(100), nullable=True) # Futu Order ID
    status = Column(String(50), default="SUBMITTED") # SUBMITTED, FILLED, FAILED
    reason = Column(String(500), nullable=True) # Strategy reason
    
    # New fields for PnL tracking
    realized_pnl = Column(Float, nullable=True, default=0.0) # Only populated for SELL orders
    pnl_pct = Column(Float, nullable=True, default=0.0) # Percentage return for SELL orders
    
    created_at = Column(DateTime, default=get_beijing_time)

class SignalRecord(Base):
    __tablename__ = "signal_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False)
    action = Column(Enum(TradeAction), nullable=False) # The analysis result (BUY, SELL, HOLD)
    reason = Column(String(500), nullable=True) # Full reason text
    close_price = Column(Float, nullable=True) # The price it was analyzed on
    current_price = Column(Float, nullable=True) # The current asset price when signal is generated
    created_at = Column(DateTime, default=get_beijing_time) # Time analysis fired

class UserWallet(Base):
    __tablename__ = "user_wallets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    market_type = Column(Enum(MarketType), nullable=False) # Which market this wallet covers
    balance = Column(Float, nullable=False, default=0.0)   # Available cash
    currency = Column(String(10), nullable=False)          # e.g. CNY, HKD, USD
    
    # Track overall performance
    total_assets = Column(Float, nullable=False, default=0.0) # Cash + Holdings Value
    total_pnl = Column(Float, nullable=False, default=0.0)    # Total realized + unrealized PnL
    
    updated_at = Column(DateTime, default=get_beijing_time, onupdate=get_beijing_time)


class Holding(Base):
    """
    Persistent position tracking — replaces the in-memory mock_holdings dict.
    One row per (user_id, code). quantity=0 means flat (no open position).
    Updated atomically on every BUY / SELL execution.
    """
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint('user_id', 'code', name='uq_holding_user_code'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False)
    quantity = Column(Float, nullable=False, default=0.0)           # total shares held
    sellable_quantity = Column(Float, nullable=False, default=0.0)  # shares available to sell (T+1 support)
    avg_cost = Column(Float, nullable=False, default=0.0)           # weighted average buy price
    market_type = Column(Enum(MarketType), nullable=False)
    tranches_count = Column(Integer, default=0) # Number of grid tranches (for ETF strategy)
    is_trend = Column(Integer, default=0)       # 1 if entered via Trend Breakout, 0 for Grid
    last_price = Column(Float, default=0.0)     # Real-time price (updated by monitor/bot)
    highest_price = Column(Float, default=0.0)  # Highest price seen while holding (for trailing stop)
    updated_at = Column(DateTime, default=get_beijing_time, onupdate=get_beijing_time)
    created_at = Column(DateTime, default=get_beijing_time)

class BacktestRecord(Base):
    __tablename__ = "backtest_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True, nullable=False)
    code = Column(String(50), index=True, nullable=False)
    oss_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=get_beijing_time)

class ConfigParameter(Base):
    __tablename__ = "config_parameters"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(50), index=True, nullable=False) # e.g. 'standard_stock', 'broad_etf'
    key = Column(String(50), index=True, nullable=False)      # e.g. 'max_tranches'
    value = Column(String(200), nullable=False)              # Store as string, cast in code
    description = Column(String(500), nullable=True)
    updated_at = Column(DateTime, default=get_beijing_time, onupdate=get_beijing_time)
    
    __table_args__ = (UniqueConstraint('category', 'key', name='uq_config_cat_key'),)
