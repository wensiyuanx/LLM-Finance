import pandas as pd
from database.models import TradeAction
from strategy.indicators import calculate_indicators
from config import get_config

def generate_signals(df_60m: pd.DataFrame, df_day: pd.DataFrame = None, current_position: float = 0.0, avg_cost: float = 0.0, code: str = None, is_trend_position: bool = False, highest_price: float = 0.0, is_pre_close: bool = False) -> tuple[TradeAction, str, float, bool]:
    """
    Analyzes K-line data using a Multi-Timeframe (MTF) approach.
    Returns: (TradeAction, Reason, Score, is_trend_entry)
    """
    config = get_config()["strategies"]["standard_stock"]

    if df_60m is None or df_60m.empty:
        return TradeAction.HOLD, "无有效小时线数据", 0.0, False

    # Assumes calculate_indicators has already been called on df_60m before passing it here
    if 'SMA_5' not in df_60m.columns:
        df_60m = calculate_indicators(df_60m)
    score = 0.0

    # Calculate indicators for Daily if provided
    daily_trend_up = True # Default to True if no daily data
    if df_day is not None and not df_day.empty:
        if 'SMA_50' not in df_day.columns:
            df_day = calculate_indicators(df_day)
        if len(df_day) > 0:
            latest_day = df_day.iloc[-1]
            daily_close = latest_day['close']
            # Require Daily Close > Daily SMA 50 AND ADX > 20 for a valid macro uptrend
            if 'SMA_50' in latest_day and pd.notna(latest_day['SMA_50']):
                if daily_close < latest_day['SMA_50']:
                    daily_trend_up = False
            if 'ADX_14' in latest_day and pd.notna(latest_day['ADX_14']):
                if latest_day['ADX_14'] < 20:
                    daily_trend_up = False

    # Slice AFTER computing indicators.
    if len(df_60m) > 250:
        df_60m = df_60m.iloc[-250:].copy()

    # Needs enough rows for hourly indicators (SMA 50, ADX 14, etc.)
    if len(df_60m) < 60:
        return TradeAction.HOLD, "小时线数据不足(需至少60条)无法计算指标", 0.0, False

    latest = df_60m.iloc[-1]
    prev = df_60m.iloc[-2]

    current_price = latest['close']

    # Risk Management: Dynamic Stop-Loss / Take-Profit via ATR
    if current_position > 0 and avg_cost > 0:
        profit_pct = (current_price - avg_cost) / avg_cost

        # 1. Fallback extreme stop loss/profit - using configured values
        if profit_pct <= config["fixed_stop_loss"]:
            return TradeAction.SELL, f"触发极度风控止损 (亏损 {profit_pct*100:.2f}%)", 0.0, False
        elif profit_pct >= config["fixed_take_profit"]:
            return TradeAction.SELL, f"触发极度风控止盈 (盈利 {profit_pct*100:.2f}%)", 0.0, False

        # 2. ATR Trailing Stop / Volatility Risk
        if 'ATR_14' in latest and pd.notna(latest['ATR_14']):
            atr = latest['ATR_14']

            # Stop loss at 2.5x ATR below average cost
            if current_price <= (avg_cost - (2.5 * atr)):
                return TradeAction.SELL, f"触发ATR动态止损 (当前低于成本2.5倍真实日波动)", 0.0, False

            # Take profit at 3x ATR above average cost
            if current_price >= (avg_cost + (3 * atr)):
                return TradeAction.SELL, f"触发ATR动态止盈 (当前高于成本3倍真实日波动)", 0.0, False

        # 3. Dynamic Trailing Stop
        if highest_price > 0 and current_price < highest_price * (1 - config.get("trailing_stop_pct", 0.05)) and profit_pct > config.get("trailing_activation_pct", 0.04):
            return TradeAction.SELL, f"触发 {config.get('trailing_stop_pct', 0.05)*100:.0f}% 动态追踪止盈, 锁定收益: {profit_pct*100:.1f}%", 0.0, False

        # 4. Breakeven / Profit Protection Logic (If it went up, don't let it turn into a loss)
        # Check historical max profit based on highest_price
        if highest_price > 0 and avg_cost > 0:
            max_profit_pct = (highest_price - avg_cost) / avg_cost
            # If profit once reached > 2.5%, raise stop loss to entry price + 0.5%
            if max_profit_pct >= 0.025:
                breakeven_price = avg_cost * 1.005
                if current_price < breakeven_price:
                    return TradeAction.SELL, f"触发保本护城河 (利润曾达 {max_profit_pct*100:.1f}%), 保本微利离场", 0.0, False

    # Trend Regime Filter (Hourly)
    in_downtrend = False
    in_strong_trend = False

    if 'SMA_50' in latest and pd.notna(latest['SMA_50']):
        if current_price < latest['SMA_50']:
            in_downtrend = True

    if 'ADX_14' in latest and pd.notna(latest['ADX_14']):
        if latest['ADX_14'] > 20:
            in_strong_trend = True

    buy_signals = []
    sell_signals = []

    # BUY LOGIC (Only permitted if Daily Trend is UP)
    if daily_trend_up:
        # 1. Moving Average Crossover Logic (Hourly Golden Cross)
        if 'SMA_5' in latest and 'SMA_20' in latest:
            sma5_latest, sma20_latest = latest['SMA_5'], latest['SMA_20']
            sma5_prev, sma20_prev = prev['SMA_5'], prev['SMA_20']

            if pd.notna(sma5_latest) and pd.notna(sma20_latest):
                if sma5_prev <= sma20_prev and sma5_latest > sma20_latest:
                    # Require Volume Confirmation for Golden Crosses
                    current_vol = latest['volume']
                    if is_pre_close:
                        current_vol *= 1.2 # Scale up for missing 10 mins (50/60)

                    if 'volume' in latest and 'VOL_SMA_5' in latest and current_vol > latest['VOL_SMA_5']:
                        buy_signals.append("小时线均线金叉且放量")

        # 2. RSI Logic (Hourly Oversold Dip Buying)
        if 'RSI_14' in latest and pd.notna(latest['RSI_14']):
            rsi = latest['RSI_14']
            if rsi < 35:
                buy_signals.append(f"小时线RSI超卖({rsi:.1f})")

            if 'BOLL_LOWER' in latest:
                lower_band = latest['BOLL_LOWER']
                if pd.notna(lower_band) and current_price <= lower_band:
                    buy_signals.append("触及小时线布林带下轨")

            # 4. NEW: Trend Alignment Re-entry (防止牛市踏空)
            # If position > 0 but not trend, promote to trend
            # If position == 0, initiate trend entry
            if 'SMA_20' in latest and 'SMA_60' in latest and 'SMA_120' in latest:
                if latest['SMA_20'] > latest['SMA_60'] > (latest['SMA_120'] if pd.notna(latest['SMA_120']) else 0):
                    if current_price > latest['SMA_20'] and 48 < latest['RSI_14'] < 72:
                        obv_confirmed = True
                        if 'OBV' in latest and 'OBV_SMA_20' in latest and pd.notna(latest['OBV_SMA_20']):
                            if latest['OBV'] < latest['OBV_SMA_20']:
                                obv_confirmed = False
                                
                        if obv_confirmed:
                            if current_position == 0:
                                buy_signals.append("趋势确认强力追入")
                                adx_val = latest.get('ADX_14', 20)
                                roc_val = latest.get('ROC_20', 0)
                                momentum_score = 50 + adx_val + max(0, roc_val * 100)
                                return TradeAction.BUY, "趋势追入: 均线多头且量价齐升", momentum_score, True
                            elif not is_trend_position:
                                # PROMOTE to trend
                                return TradeAction.HOLD, "持仓晋升: 当前行情进入趋势模式", 0.0, True

    # SELL LOGIC (Can trigger regardless of daily trend to protect capital)
    # 1. Moving Average Death Cross
    if 'SMA_5' in latest and 'SMA_20' in latest:
        sma5_latest, sma20_latest = latest['SMA_5'], latest['SMA_20']
        sma5_prev, sma20_prev = prev['SMA_5'], prev['SMA_20']
        if pd.notna(sma5_latest) and pd.notna(sma20_latest):
            if sma5_prev >= sma20_prev and sma5_latest < sma20_latest:
                sell_signals.append("小时线均线死叉")

    # 2. RSI Overbought
    if 'RSI_14' in latest and pd.notna(latest['RSI_14']):
        rsi = latest['RSI_14']
        if rsi > 70 and not in_strong_trend:
            sell_signals.append(f"小时线RSI超买({rsi:.1f})")

    # 3. Bollinger Bands Upper Band
    if 'BOLL_UPPER' in latest:
        upper_band = latest['BOLL_UPPER']
        if pd.notna(upper_band) and current_price >= upper_band and not in_strong_trend:
            sell_signals.append("触及小时线布林带上轨")

    # 4. Trend Break Exit (for Trend positions)
    if is_trend_position:
        if 'SMA_20' in latest and 'SMA_60' in latest:
            if latest['SMA_20'] < latest['SMA_60'] or current_price < latest['SMA_60']:
                return TradeAction.SELL, "趋势破位止盈 (SMA20<60)", 0.0, False

    # Multi-Factor Consensus & Conflict Resolution
    # Priority 1: If both buy and sell signals are present, prioritize SELL to protect capital
    if current_position > 0 and len(sell_signals) > 0:
        if len(sell_signals) >= 2:
            return TradeAction.SELL, "强卖出信号 (冲突优选): " + " + ".join(sell_signals), 0.0, False
        else:
            return TradeAction.SELL, f"卖出信号 (冲突优选): {sell_signals[0]}", 0.0, False

    score = 0.0
    # Base score on RSI for dip buying, but we will adjust for momentum
    if 'RSI_14' in latest and pd.notna(latest['RSI_14']):
        score = 100 - latest['RSI_14']

    # Buy: only if no position
    if current_position == 0 and len(buy_signals) > 0:
        if len(buy_signals) >= 2:
            return TradeAction.BUY, "强买入信号: " + " + ".join(buy_signals), score, False
        elif len(buy_signals) == 1:
            if "均线金叉" in buy_signals[0]:
                # For moving average crossovers, use momentum score instead of RSI oversold score
                adx_val = latest.get('ADX_14', 20)
                roc_val = latest.get('ROC_20', 0)
                mom_score = 40 + adx_val + max(0, roc_val * 100)
                return TradeAction.BUY, "买入信号: " + buy_signals[0], max(score, mom_score), False
            elif "RSI超卖" in buy_signals[0]:
                return TradeAction.BUY, "买入信号: " + buy_signals[0], score, False

    # Sell: only if holding
    if current_position > 0:
        if len(sell_signals) >= 2:
            return TradeAction.SELL, "强卖出信号: " + " + ".join(sell_signals), 0.0, False
        elif len(sell_signals) == 1:
            if "均线死叉" in sell_signals[0] or "布林带上轨" in sell_signals[0]:
                return TradeAction.SELL, f"卖出信号: {sell_signals[0]}", 0.0, False

    return TradeAction.HOLD, "无明确可执行信号 (观望/持仓)", 0.0, False

def generate_grid_trend_signals(df_60m: pd.DataFrame, df_day: pd.DataFrame = None, current_position: float = 0.0, avg_cost: float = 0.0, tranches_count: int = 0, is_trend_position: bool = False, highest_price: float = 0.0, is_pre_close: bool = False) -> tuple[TradeAction, str, float, bool]:
    """
    统一的"网格+趋势"激进策略：结合左侧超跌网格建模与右侧右侧趋势追入。
    适用于 ETF 和绩优蓝筹股。
    Returns: (TradeAction, Reason, Score, is_trend_entry)
    """
    config = get_config()["strategies"]["broad_etf"]

    if df_60m is None or df_60m.empty:
        return TradeAction.HOLD, "无有效小时线数据", 0.0, False

    # Assumes calculate_indicators has already been called on df_60m before passing it here
    if 'SMA_5' not in df_60m.columns:
        df_60m = calculate_indicators(df_60m)

    if df_day is not None and not df_day.empty and 'SMA_50' not in df_day.columns:
        df_day = calculate_indicators(df_day)

    if len(df_60m) > 250:
        df_60m = df_60m.iloc[-250:].copy()

    if len(df_60m) < 60:
        return TradeAction.HOLD, "小时线数据不足(需至少60条)无法计算指标", 0.0, False

    latest = df_60m.iloc[-1]
    current_price = latest['close']

    score = 0.0
    if 'RSI_14' in latest and pd.notna(latest['RSI_14']):
        score = 100 - latest['RSI_14'] # More oversold = higher score

    # 策略参数设定
    rsi_oversold = config.get("rsi_oversold", 25)
    boll_drop_pct = config.get("boll_drop_pct", 0.02)
    grid_drop_pct = config["grid_drop_pct"]
    take_profit_pct = config.get("take_profit_pct", 0.04)
    trailing_stop_pct = config["profit_target_pct"] # 追踪止盈阈值
    max_tranches = config["max_tranches"]

    # --- 1. 卖出逻辑 (动态跟踪止盈 或 趋势破位) ---
    if current_position > 0 and avg_cost > 0:
        profit_pct = (current_price - avg_cost) / avg_cost

        # 激活追踪止盈：利润超过 4%，且当前触及最高价回落 5%
        if profit_pct >= take_profit_pct and highest_price > 0:
            if current_price < highest_price * (1 - trailing_stop_pct):
                return TradeAction.SELL, f"触发 5% 动态追踪止盈, 锁定收益: {profit_pct*100:.1f}%, 最高价: {highest_price:.3f}", 0.0, False

        # 整体止盈 B：超跌反弹触及布林带上轨 (仅对非趋势持仓有效，且未处于大幅盈利保护中)
        if 'BOLL_UPPER' in latest and profit_pct < take_profit_pct:
            upper_band = latest['BOLL_UPPER']
            if not is_trend_position and pd.notna(upper_band) and current_price >= upper_band and profit_pct > 0.01:
                return TradeAction.SELL, "超跌反弹触及布林上轨止盈，全仓平仓", 0.0, False

        # --- 趋势持仓专属退出逻辑 (基于回测优化的 自适应 跟踪回撤) ---
        if is_trend_position:
            # 记录趋势单持仓期间的最高价 (注意：实盘中 highest_price 已由 main.py 维护并传入)
            if highest_price > 0:
                # 0. 计算趋势强度 (ADX)
                adx = latest.get('ADX', 20) # 默认为弱
                is_strong_trend = adx > 25

                # 1. 保本策略 (Breakeven): 利润 > 2% 时，止损上移至成本+0.5%
                if profit_pct >= 0.02:
                    breakeven_line = avg_cost * 1.005
                    if current_price < breakeven_line:
                        return TradeAction.SELL, f"趋势单触发保本止损 (利润曾达2%), 收益: {profit_pct*100:.1f}%", 0.0, False

                # 2. 阶梯与自适应跟踪 (Adaptive Trailing):
                if 0.015 <= profit_pct < 0.04:
                    if not is_strong_trend:
                        # 震荡市：紧凑离场 (2%)
                        if current_price < highest_price * 0.98:
                            return TradeAction.SELL, f"趋势弱势回调 (最高价 {highest_price:.3f} 回撤2%), 收益: {profit_pct*100:.1f}%", 0.0, False
                    else:
                        # 强势趋势：放宽至 4%
                        if current_price < highest_price * 0.96:
                            return TradeAction.SELL, f"趋势强势回调 (最高价 {highest_price:.3f} 回撤4%), 收益: {profit_pct*100:.1f}%", 0.0, False
                elif profit_pct >= 0.04:
                    if is_strong_trend:
                        # 极强持股：留出 6% 波动空间
                        if current_price < highest_price * 0.94:
                            return TradeAction.SELL, f"趋势主升浪回撤 (最高价 {highest_price:.3f} 6%), 收益: {profit_pct*100:.1f}%", 0.0, False
                    else:
                        # 趋势弱化：收紧至 3%
                        if current_price < highest_price * 0.97:
                            return TradeAction.SELL, f"趋势转弱落袋 (最高价 {highest_price:.3f} 3%), 收益: {profit_pct*100:.1f}%", 0.0, False

            # 策略 B: 大级别趋势彻底反转 (SMA60 < SMA120 代替之前的 SMA20 < 60)
            if 'SMA_60' in latest and 'SMA_120' in latest:
                if pd.notna(latest['SMA_120']) and latest['SMA_60'] < latest['SMA_120']:
                    return TradeAction.SELL, f"趋势大级别破位 (SMA60<120), 收益: {profit_pct*100:.1f}%", 0.0, False

            elif 'SMA_60' in latest and current_price < latest['SMA_60'] * 0.98: # 价格大幅跌破 60 均线
                return TradeAction.SELL, f"趋势价格严重跌破 SMA60, 收益: {profit_pct*100:.1f}%", 0.0, False

        # 注: 实盘暂不支持记住每笔买入的具体价位，因此先做整体止盈处理，或依赖平均成本判断

    # --- 2. 买入逻辑 (左侧网格建仓) ---
    if tranches_count < max_tranches:
        # 首仓建仓
        if current_position == 0:
            if 'RSI_14' in latest and pd.notna(latest['RSI_14']):
                if latest['RSI_14'] < rsi_oversold:
                    return TradeAction.BUY, f"首仓: RSI极度超卖({latest['RSI_14']:.1f})", score, False

            if 'BOLL_LOWER' in latest:
                lower_band = latest['BOLL_LOWER']
                if pd.notna(lower_band) and current_price <= lower_band * (1 - boll_drop_pct):
                    # Slightly boost score if it breaks bollinger lower band heavily
                    return TradeAction.BUY, "首仓: 跌破布林下轨2%", score + 10.0, False

        # 网格加仓 (被套状态)
        elif current_position > 0 and avg_cost > 0:
            # 简化版：只要当前价格比平均成本跌幅超过 3% * 批次，就加仓。
            # 这里 tranches_count 必须至少是 1（因为已经有持仓了），避免乘以 0 导致错误加仓
            effective_tranches = max(1, tranches_count)
            expected_drop = grid_drop_pct * effective_tranches
            drop_from_cost = (avg_cost - current_price) / avg_cost

            if drop_from_cost >= expected_drop:
                # 额外加一个 RSI 超卖限制避免单边瀑布过快加仓
                if 'RSI_14' in latest and pd.notna(latest['RSI_14']) and latest['RSI_14'] < 35:
                    return TradeAction.BUY, f"网格加仓(第{effective_tranches+1}批): 距成本下跌 {drop_from_cost*100:.1f}%, 且RSI超卖", score + (drop_from_cost * 100), False

    # --- 3. 趋势突破追入（防止错过波段牛） ---
    # 场景：空仓/非趋势仓 + 均线多头排列 + RSI 适中强势
    if 'SMA_20' in latest and 'SMA_60' in latest and 'SMA_120' in latest:
        sma20, sma60, sma120 = latest['SMA_20'], latest['SMA_60'], latest['SMA_120']
        # Relax SMA120 check if data is slightly insufficient
        sma120_val = sma120 if pd.notna(sma120) else (sma60 * 0.95)
        if sma20 > sma60 > sma120_val and current_price > sma20:
            if 'RSI_14' in latest and 48 < latest['RSI_14'] < 85: # 放宽 RSI 到 85
                if 'BOLL_MID' in latest and current_price > latest['BOLL_MID']:
                    obv_confirmed = True
                    if 'OBV' in latest and 'OBV_SMA_20' in latest and pd.notna(latest['OBV_SMA_20']):
                        if latest['OBV'] < latest['OBV_SMA_20']:
                            obv_confirmed = False
                            
                    if obv_confirmed:
                        if current_position == 0:
                            # 动态动能打分：取代原先的低分 (100-RSI + 15)
                            adx_val = latest.get('ADX_14', 20)
                            roc_val = latest.get('ROC_20', 0)
                            momentum_score = 60 + adx_val + max(0, roc_val * 100)
                            return TradeAction.BUY, f"趋势追入 (激进): 均线多头且量价齐升, RSI={latest['RSI_14']:.1f}", momentum_score, True
                        elif not is_trend_position:
                            return TradeAction.HOLD, f"持仓晋升 (激进): 开启趋势追踪模式, RSI={latest['RSI_14']:.1f}", 0.0, True

    return TradeAction.HOLD, "无网格/趋势执行信号", 0.0, False
