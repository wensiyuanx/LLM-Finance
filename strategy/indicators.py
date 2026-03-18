import pandas as pd
import pandas_ta as ta

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate technical indicators for a given dataframe of K-line data.
    Expected columns in df: 'open', 'close', 'high', 'low', 'volume'
    """
    if df is None or df.empty:
        return df

    # Example: Calculate Moving Averages
    df['SMA_5'] = ta.sma(df['close'], length=5)
    df['SMA_20'] = ta.sma(df['close'], length=20)
    
    # 50-day SMA for broad market regime filtering
    if len(df) >= 50:
        df['SMA_50'] = ta.sma(df['close'], length=50)

    # Calculate MACD
    macd = ta.macd(df['close'])
    if macd is not None and not macd.empty:
        # MACD returns MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        # Assuming defaults (12, 26, 9)
        macd_columns = macd.columns
        df['MACD'] = macd[macd_columns[0]]
        df['MACD_Histogram'] = macd[macd_columns[1]]
        df['MACD_Signal'] = macd[macd_columns[2]]

    # Calculate RSI
    df['RSI_14'] = ta.rsi(df['close'], length=14)

    # Calculate Bollinger Bands (BOLL)
    bbands = ta.bbands(df['close'], length=20, std=2)
    if bbands is not None and not bbands.empty:
        bbands_columns = bbands.columns
        df['BOLL_LOWER'] = bbands[bbands_columns[0]] # BBL_20_2.0
        df['BOLL_MID'] = bbands[bbands_columns[1]]   # BBM_20_2.0
        df['BOLL_UPPER'] = bbands[bbands_columns[2]] # BBU_20_2.0

    # Calculate Average True Range (ATR) for dynamic risk
    df['ATR_14'] = ta.atr(high=df['high'], low=df['low'], close=df['close'], length=14)
    
    # Calculate Average Directional Index (ADX) for trend strength
    adx_df = ta.adx(high=df['high'], low=df['low'], close=df['close'], length=14)
    if adx_df is not None and not adx_df.empty:
        df['ADX_14'] = adx_df[adx_df.columns[0]] # ADX_14
        
    # Calculate On-Balance Volume (OBV) and Volume SMA
    df['OBV'] = ta.obv(close=df['close'], volume=df['volume'])
    df['VOL_SMA_5'] = ta.sma(df['volume'], length=5)

    return df
