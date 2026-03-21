import pandas as pd
from database.models import TradeAction
from strategy.indicators import calculate_indicators

def generate_leveraged_etf_signals(df_60m: pd.DataFrame, df_day: pd.DataFrame = None, current_position: float = 0.0, avg_cost: float = 0.0, highest_price: float = 0.0, tranches_count: int = 0, last_buy_price: float = 0.0, is_pre_close: bool = False) -> tuple[TradeAction, str, float, bool]:
    """
    专门针对杠杆 ETF (如 HK.07226) 设计的右侧动量与防耗损策略 (加入金字塔网格加仓)。
    核心逻辑：
    1. 绝不左侧接飞刀（禁用 RSI 超卖抄底、布林带下轨抄底）。
    2. 严格的右侧突破买入（MACD 零轴上金叉 / 均线多头排列且放量）。
    3. 极其敏感的动态止盈/止损（防范杠杆损耗和单边暴跌）。
    4. 结合日线级别的大趋势过滤，逆势不操作。
    5. 金字塔加仓：在入场后若遇回调，在趋势未破坏的前提下分批加仓摊薄成本。
    """
    if df_60m is None or df_60m.empty:
        return TradeAction.HOLD, "无有效小时线数据", 0.0, False

    df_60m = calculate_indicators(df_60m)
    
    # Calculate daily indicators if provided
    daily_trend_up = True # Default to True if no daily data
    if df_day is not None and not df_day.empty:
        df_day = calculate_indicators(df_day)
        if len(df_day) > 0 and 'SMA_50' in df_day.columns:
            latest_day = df_day.iloc[-1]
            if latest_day['close'] < latest_day['SMA_50']:
                daily_trend_up = False
    
    if len(df_60m) > 250:
        df_60m = df_60m.iloc[-250:].copy()

    if len(df_60m) < 60:
        return TradeAction.HOLD, "数据不足无法计算指标", 0.0, False

    latest = df_60m.iloc[-1]
    prev = df_60m.iloc[-2]
    current_price = latest['close']
    
    score = 0.0
    
    # ATR for dynamic stop loss (Calculate manually if not in dataframe)
    current_atr = 0.0
    if 'ATR_14' in latest:
        current_atr = latest['ATR_14']
    else:
        # Fallback approximation if ATR is missing: 2% of price
        current_atr = current_price * 0.02
        
    atr_stop_mult = 3.0   # 3x ATR for hard stop loss (give it room to breathe)
    atr_trail_mult = 2.0  # 2x ATR trailing from highest price
    
    # --- 1. 卖出与风控逻辑 (最高优先级) ---
    if current_position > 0 and avg_cost > 0:
        profit_pct = (current_price - avg_cost) / avg_cost
        
        # A. 动态 ATR 硬止损 (入场价 - 3 * ATR)
        initial_stop_price = avg_cost - (current_atr * atr_stop_mult)
        if current_price <= initial_stop_price:
            return TradeAction.SELL, f"杠杆风控: 触发 ATR 硬止损", 0.0, False
            
        # B. 动态 ATR 跟踪止盈 (最高价 - 2 * ATR)
        if highest_price > avg_cost: # 只有当产生过盈利时才激活跟踪止盈
            trailing_stop_price = highest_price - (current_atr * atr_trail_mult)
            if current_price <= trailing_stop_price:
                return TradeAction.SELL, f"杠杆风控: 触发 ATR 跟踪止盈 (收益 {profit_pct*100:.1f}%)", 0.0, False
                
        # C. 极端超买暴涨止盈 (分批止盈/逃顶)
        if 'RSI_14' in latest and latest['RSI_14'] > 85 and profit_pct > 0.15:
             # 如果出现极端暴涨，RSI极高，且已有丰厚利润，主动锁定离场，防大阴线砸盘
             return TradeAction.SELL, f"波段逃顶: 极端超买且获利丰厚 (收益 {profit_pct*100:.1f}%)", 0.0, False
                
        # D. 趋势死叉 / 动量衰竭
        if 'SMA_5' in latest and 'SMA_20' in latest:
            if prev['SMA_5'] >= prev['SMA_20'] and latest['SMA_5'] < latest['SMA_20']:
                return TradeAction.SELL, "右侧离场: 小时线均线死叉", 0.0, False
                
        # D. MACD High-level Death Cross or MACD drops below zero
        if 'MACD' in latest and 'MACD_Signal' in latest:
            if prev['MACD'] >= prev['MACD_Signal'] and latest['MACD'] < latest['MACD_Signal']:
                if latest['MACD'] > 0: # 零轴上方死叉，动量反转
                    return TradeAction.SELL, "右侧离场: MACD高位死叉", 0.0, False
            if prev['MACD'] >= 0 and latest['MACD'] < 0:
                return TradeAction.SELL, "右侧离场: MACD下穿零轴", 0.0, False

    # --- 2. 买入逻辑 (纯右侧动量突破 OR 金字塔加仓) ---
    max_tranches = 3
    grid_drop_pct = 0.08
    
    if current_position == 0 or (current_position > 0 and tranches_count < max_tranches):
        buy_reason = ""
        
        # A. 金字塔网格加仓 (已持仓情况下)
        if current_position > 0 and last_buy_price > 0:
            drop_from_last = (last_buy_price - current_price) / last_buy_price
            if drop_from_last >= grid_drop_pct:
                if daily_trend_up: # 大级别趋势不能破位
                    return TradeAction.BUY, f"金字塔加仓: 第 {tranches_count + 1} 批 (跌幅 {drop_from_last*100:.1f}%)", 60.0, False
                    
        # B. 首次突破买入
        if current_position == 0:
            # 动量条件 1: 均线多头排列 (短期趋势确立)
        ma_bullish = False
        if 'SMA_5' in latest and 'SMA_20' in latest and 'SMA_50' in latest:
            if latest['SMA_5'] > latest['SMA_20'] > latest['SMA_50'] and current_price > latest['SMA_5']:
                ma_bullish = True
                
        # 动量条件 2: MACD 零轴上方金叉或持续多头
        macd_bullish = False
        if 'MACD' in latest and 'MACD_Signal' in latest:
            # 要么刚刚金叉，要么在零轴上方且 MACD > Signal
            just_crossed = prev['MACD'] <= prev['MACD_Signal'] and latest['MACD'] > latest['MACD_Signal']
            strong_above_zero = latest['MACD'] > 0 and latest['MACD'] > latest['MACD_Signal']
            if just_crossed or strong_above_zero:
                macd_bullish = True
                
        # 动量条件 3: 极其显著的放量确认 (避免假突破)
        volume_confirmed = False
        current_vol = latest['volume']
        if is_pre_close:
            current_vol *= 1.2
        if 'VOL_SMA_5' in latest and current_vol > latest['VOL_SMA_5'] * 1.5: # 要求放量至少 50%
            volume_confirmed = True
            
        # 避免追高条件: RSI 不能严重超买，但需保持强势
        rsi_ok = False
        if 'RSI_14' in latest and 50 < latest['RSI_14'] < 75:
            rsi_ok = True
            
        # MACD必须在零轴上方
        macd_positive = False
        if 'MACD' in latest and latest['MACD'] > 0:
            macd_positive = True
            
        # ADX 趋势强度过滤 (必须大于 20，避免在震荡市频繁触发)
        strong_trend = False
        super_trend = False
        if 'ADX_14' in latest:
            if latest['ADX_14'] > 20:
                strong_trend = True
            if latest['ADX_14'] > 40: # ADX > 40 代表极强主升浪/主跌浪
                super_trend = True
            
        # 价格必须在 50 均线之上 (大级别趋势必须是多头)
        above_trend = False
        if daily_trend_up: # Prioritize Daily SMA50 if available
            above_trend = True
        elif 'SMA_50' in latest and current_price > latest['SMA_50']:
            above_trend = True
            
        # 如果处于超级主升浪中 (ADX > 40 且 MACD 强烈向上)，可以稍微放宽日线过滤（比如刚从大底拉升时，日线均线还没跟上）
        if super_trend and macd_bullish and macd_positive:
            above_trend = True # 强行豁免日线过滤，抓住底部首个暴涨波段
            
        # 触发买入
        if ma_bullish and macd_bullish and volume_confirmed and rsi_ok and strong_trend and above_trend:
            score = 80.0
            return TradeAction.BUY, "动量突破: 均线多头 + MACD向好 + 显著放量(>50%) + 强趋势(ADX>20)", score, True
            
        # 备选买入: 均线刚金叉且放量 (较早期的右侧)
        if 'SMA_5' in latest and 'SMA_20' in latest:
            if prev['SMA_5'] <= prev['SMA_20'] and latest['SMA_5'] > latest['SMA_20']:
                if volume_confirmed and rsi_ok and macd_positive and above_trend and strong_trend:
                    return TradeAction.BUY, "右侧入场: 均线金叉 + 显著放量 + MACD>0 + 价格>SMA50 + 强趋势", 70.0, True

    return TradeAction.HOLD, "无符合杠杆策略的动量信号", 0.0, False
