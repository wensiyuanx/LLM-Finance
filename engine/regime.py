import yfinance as yf
import pandas as pd
import pandas_ta as ta
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class RegimeDetector:
    """
    Detects the current market regime based on benchmark indices (e.g., CSI 300 for A-Shares, HSI for HK-Shares).
    Uses 200-day SMA and ADX to classify the market into STRONG_BULL, STRONG_BEAR, or CHOPPY.
    """
    _cache = {}
    
    @classmethod
    def get_market_regime(cls, market_type: str) -> str:
        """
        market_type: 'A_SHARE' or 'HK_SHARE'
        Returns: 'STRONG_BULL', 'STRONG_BEAR', 'CHOPPY', or 'UNKNOWN'
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        if market_type in cls._cache:
            cached_date, cached_regime = cls._cache[market_type]
            if cached_date == today:
                return cached_regime
                
        ticker = "000300.SS" if market_type == "A_SHARE" else "^HSI"
        
        try:
            # Fetch ~300 trading days of data to compute 200-SMA
            df = yf.download(ticker, period="400d", progress=False)
            if df.empty:
                logger.warning(f"Failed to download benchmark data for {ticker}")
                return "UNKNOWN"
                
            # Handle yfinance MultiIndex output for single tickers
            if isinstance(df.columns, pd.MultiIndex):
                close_col = ('Close', ticker)
                high_col = ('High', ticker)
                low_col = ('Low', ticker)
            else:
                close_col = 'Close'
                high_col = 'High'
                low_col = 'Low'

            df['SMA_200'] = ta.sma(df[close_col], length=200)
            adx_res = ta.adx(df[high_col], df[low_col], df[close_col], length=14)
            
            if adx_res is not None:
                df['ADX'] = adx_res.iloc[:, 0]
            else:
                df['ADX'] = 0.0
                
            latest = df.iloc[-1]
            
            close_val = latest[close_col]
            sma200_val = latest['SMA_200']
            adx_val = latest['ADX']
            
            # Handle pd.Series extraction if necessary
            if isinstance(close_val, pd.Series): close_val = close_val.iloc[0]
            if isinstance(sma200_val, pd.Series): sma200_val = sma200_val.iloc[0]
            if isinstance(adx_val, pd.Series): adx_val = adx_val.iloc[0]
            
            if pd.isna(sma200_val) or pd.isna(adx_val):
                return "UNKNOWN"
                
            is_bull = close_val > sma200_val
            
            if is_bull and adx_val > 25:
                regime = "STRONG_BULL"
            elif not is_bull and adx_val > 25:
                regime = "STRONG_BEAR"
            else:
                regime = "CHOPPY"
                
            cls._cache[market_type] = (today, regime)
            logger.info(f"Market Regime for {market_type} ({ticker}): {regime} (Close: {close_val:.2f}, SMA200: {sma200_val:.2f}, ADX: {adx_val:.1f})")
            return regime
            
        except Exception as e:
            logger.error(f"Error detecting market regime for {market_type}: {e}")
            return "UNKNOWN"
