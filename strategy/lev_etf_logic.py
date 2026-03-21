import pandas as pd
from database.models import TradeAction
from strategy.indicators import calculate_indicators
from config import get_config

def generate_leveraged_etf_signals(df_60m: pd.DataFrame, df_day: pd.DataFrame = None, current_position: float = 0.0, avg_cost: float = 0.0, highest_price: float = 0.0, tranches_count: int = 0, last_buy_price: float = 0.0, is_pre_close: bool = False) -> tuple[TradeAction, str, float, bool]:
    """
    专门针对杠杆 ETF (如 HK.07226) 设计的保守动量策略。
    放弃网格左侧交易，因为杠杆品种在下跌中加仓极其危险（损耗极大）。
    """
    config = get_config()["strategies"]["leveraged_etf"]
    
    if df_60m is None or df_60m.empty:
        return TradeAction.HOLD, "无有效小时线数据", 0.0, False

    df_60m = calculate_indicators(df_60m)
    
    # 大级别趋势判断
    daily_trend_up = True
    if df_day is not None and not df_day.empty:
        df_day = calculate_indicators(df_day)
        if len(df_day) > 0 and 'SMA_50' in df_day.columns:
            latest_day = df_day.iloc[-1]
            if latest_day['close'] < latest_day['SMA_50']:
                daily_trend_up = False
    
    if len(df_60m) < 60:
        return TradeAction.HOLD, "数据不足无法计算指标", 0.0, False

    latest = df_60m.iloc[-1]
    prev = df_60m.iloc[-2]
    current_price = latest['close']
    
    # ATR 动态止损
    current_atr = latest.get('ATR_14', current_price * 0.02)
    
    # --- 1. 卖出与风控逻辑 (最高优先级) ---
    if current_position > 0 and avg_cost > 0:
        profit_pct = (current_price - avg_cost) / avg_cost
        
        # A. 动态 ATR 硬止损
        initial_stop_price = avg_cost - (current_atr * config["atr_stop_mult"])
        if current_price <= initial_stop_price:
            return TradeAction.SELL, f"杠杆风控: 触发 ATR 硬止损", 0.0, False
            
        # B. 自适应跟踪止盈 (ADX判定)
        adx = latest.get('ADX_14', 20)
        is_strong_trend = adx > 25
        
        if highest_price > avg_cost:
            # 阶梯止盈：利润到达一定比例后启动
            if 0.025 <= profit_pct < 0.05:
                # 震荡市收紧至 2.5%，趋势市放宽至 4%
                trail_pct = 0.975 if not is_strong_trend else 0.96
                if current_price < highest_price * trail_pct:
                    return TradeAction.SELL, f"杠杆风控: 阶段性止盈锁定, 收益 {profit_pct*100:.1f}%", 0.0, False
            elif profit_pct >= 0.05:
                # 大利阶段：趋势强则用 ATR 跟踪，否则固定 4%
                if is_strong_trend:
                    trailing_stop = highest_price - (current_atr * config["atr_trail_mult"])
                    if current_price <= trailing_stop:
                        return TradeAction.SELL, f"杠杆风控: 触发动态跟踪止盈, 收益 {profit_pct*100:.1f}%", 0.0, False
                else:
                    if current_price < highest_price * 0.96:
                        return TradeAction.SELL, f"杠杆风控: 趋势转弱止盈 (4%), 收益 {profit_pct*100:.1f}%", 0.0, False

        # C. 趋势死叉切换
        if 'SMA_5' in latest and 'SMA_20' in latest:
            if prev['SMA_5'] >= prev['SMA_20'] and latest['SMA_5'] < latest['SMA_20']:
                return TradeAction.SELL, "右侧离场: 均线死叉", 0.0, False
        
        if 'MACD' in latest and latest['MACD_Signal'] in latest:
            if prev['MACD'] >= 0 and latest['MACD'] < 0:
                return TradeAction.SELL, "右侧离场: MACD下穿零轴", 0.0, False

    # --- 2. 买入逻辑 (纯右侧动量，放弃左侧补仓) ---
    if current_position == 0:
        # 只在趋势确立、放量、且 RSI 不极高时入场
        # 指标计算
        ma_bullish = False
        if 'SMA_5' in latest and 'SMA_20' in latest and 'SMA_50' in latest:
            if latest['SMA_5'] > latest['SMA_20'] > latest['SMA_50'] and current_price > latest['SMA_5']:
                ma_bullish = True
        
        macd_bullish = False
        if 'MACD' in latest and 'MACD_Signal' in latest:
            macd_bullish = (prev['MACD'] <= prev['MACD_Signal'] and latest['MACD'] > latest['MACD_Signal']) or (latest['MACD'] > 0 and latest['MACD'] > latest['MACD_Signal'])
            
        volume_confirmed = False
        if 'VOL_SMA_5' in latest and latest['volume'] > latest['VOL_SMA_5'] * config["volume_surge_mult"]:
            volume_confirmed = True
            
        rsi_ok = 'RSI_14' in latest and 50 < latest['RSI_14'] < 75
        strong_trend = latest.get('ADX_14', 0) > config["adx_min_trend"]
        
        above_trend = daily_trend_up or (latest.get('SMA_50', 0) > 0 and current_price > latest['SMA_50'])
        
        if ma_bullish and macd_bullish and volume_confirmed and rsi_ok and strong_trend and above_trend:
            return TradeAction.BUY, "动量突破入场", 80.0, True
            
    return TradeAction.HOLD, "无杠杆策略信号", 0.0, False
