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
            # 杠杆ETF波动大，适当放宽止盈触发线，让利润奔跑
            if 0.04 <= profit_pct < 0.08:
                # 震荡市收紧至 3%，趋势市放宽至 5%
                trail_pct = 0.97 if not is_strong_trend else 0.95
                if current_price < highest_price * trail_pct:
                    return TradeAction.SELL, f"杠杆风控: 阶段性止盈锁定, 收益 {profit_pct*100:.1f}%", 0.0, False
            elif profit_pct >= 0.08:
                # 大利阶段：趋势强则用 2倍ATR 跟踪，否则固定回撤 5%
                if is_strong_trend:
                    trailing_stop = highest_price - (current_atr * 2.0)
                    if current_price <= trailing_stop:
                        return TradeAction.SELL, f"杠杆风控: 触发动态跟踪止盈, 收益 {profit_pct*100:.1f}%", 0.0, False
                else:
                    if current_price < highest_price * 0.95:
                        return TradeAction.SELL, f"杠杆风控: 趋势转弱止盈 (5%), 收益 {profit_pct*100:.1f}%", 0.0, False

        # C. 趋势死叉切换
        if 'SMA_5' in latest and 'SMA_20' in latest:
            if prev['SMA_5'] >= prev['SMA_20'] and latest['SMA_5'] < latest['SMA_20']:
                return TradeAction.SELL, "右侧离场: 均线死叉", 0.0, False
        
        if 'MACD' in latest and latest['MACD_Signal'] in latest:
            if prev['MACD'] >= 0 and latest['MACD'] < 0:
                return TradeAction.SELL, "右侧离场: MACD下穿零轴", 0.0, False

    # --- 2. 买入逻辑 (动量反转与趋势共振) ---
    if current_position == 0:
        # 杠杆ETF不能只等最完美的右侧（因为经常错过主升浪），
        # 应该在“超跌反弹确认”或“均线刚开始多头”时及早介入。
        
        # 信号1：超跌反弹（RSI金叉 + MACD底背离或金叉）
        rsi_oversold_rebound = False
        if 'RSI_14' in latest and 'RSI_14' in prev:
            # RSI从超卖区回升
            if prev['RSI_14'] < 35 and latest['RSI_14'] > 35:
                rsi_oversold_rebound = True
                
        macd_golden_cross = False
        if 'MACD' in latest and 'MACD_Signal' in latest:
            macd_golden_cross = (prev['MACD'] <= prev['MACD_Signal'] and latest['MACD'] > latest['MACD_Signal'])
            
        # 信号2：均线多头排列初期（不需要SMA5>20>50那么苛刻，只要短期趋势向上）
        short_trend_up = False
        if 'SMA_5' in latest and 'SMA_20' in latest:
            short_trend_up = (latest['SMA_5'] > latest['SMA_20'] and current_price > latest['SMA_5'])
            
        # 信号3：放量突破
        volume_breakout = False
        if 'VOL_SMA_5' in latest and latest['volume'] > latest['VOL_SMA_5'] * 1.5:
            volume_breakout = True

        buy_reason = ""
        score = 0.0
        
        # 组合策略 A: 底部反弹确认 (适合抄底)
        if rsi_oversold_rebound and macd_golden_cross:
            buy_reason = "底部反弹: RSI回升+MACD金叉"
            score = 70.0
            
        # 组合策略 B: 右侧动量突破 (适合追主升浪)
        elif short_trend_up and macd_golden_cross and volume_breakout:
            buy_reason = "动量突破: 均线向上+放量金叉"
            score = 80.0
            
        # 组合策略 C: 强趋势顺势上车
        elif short_trend_up and latest.get('ADX_14', 0) > 25 and latest.get('RSI_14', 50) < 70:
            buy_reason = "趋势顺势: ADX强趋势且未超买"
            score = 75.0

        if buy_reason:
            # OBV 确认资金没有明显流出即可（放宽限制）
            if 'OBV' in latest and 'OBV_SMA_20' in latest and pd.notna(latest['OBV_SMA_20']):
                if latest['OBV'] < latest['OBV_SMA_20'] * 0.95: # 允许5%的误差
                    buy_reason += " [资金流出警告: 降低得分]"
                    score -= 20.0
                    
            if score >= 60.0:
                # 杠杆ETF统一视为趋势单处理
                return TradeAction.BUY, buy_reason, score, True
            
    return TradeAction.HOLD, "无杠杆策略信号", 0.0, False
