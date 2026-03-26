import pandas as pd
import pandas_ta as ta
import logging
from datetime import datetime, timedelta
from data.futu_client import FutuClient
from futu import KLType

logger = logging.getLogger(__name__)

class RegimeDetector:
    """
    Detects the current market regime based on benchmark indices (e.g., SH.000300 for A-Shares, HK.800000 for HK-Shares).
    Uses 200-day SMA and ADX to classify the market into STRONG_BULL, STRONG_BEAR, or CHOPPY.
    """
    _cache = {}
    
    @classmethod
    def get_market_regime(cls, market_type: str, futu_client: FutuClient = None) -> str:
        """
        market_type: 'A_SHARE' or 'HK_SHARE'
        Returns: 'STRONG_BULL', 'STRONG_BEAR', 'CHOPPY', or 'UNKNOWN'
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        if market_type in cls._cache:
            cached_date, cached_regime = cls._cache[market_type]
            if cached_date == today:
                return cached_regime
                
        ticker = "SH.000300" if market_type == "A_SHARE" else "HK.800000"
        
        own_client = False
        if futu_client is None:
            futu_client = FutuClient()
            if not futu_client.connect():
                logger.warning(f"Failed to connect to FutuOpenD for regime detection ({ticker})")
                return "UNKNOWN"
            own_client = True
        
        try:
            # Fetch ~400 calendar days of data to compute 200-SMA
            start_date = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
            
            # Add simple retry for the regime fetch as well
            max_retries = 3
            df = None
            for attempt in range(max_retries):
                try:
                    df = futu_client.get_historical_klines(ticker, start_date=start_date, end_date=today, ktype=KLType.K_DAY)
                    break
                except Exception as e:
                    if "频率太高" in str(e) or "30秒最多" in str(e):
                        if attempt < max_retries - 1:
                            logger.warning(f"Rate limit hit in regime detector for {ticker}, sleeping 10s...")
                            time.sleep(10)
                            continue
                    raise e
                    
            if df is None or df.empty:
                logger.warning(f"Failed to download benchmark data for {ticker}")
                return "UNKNOWN"
                
            close_col = 'close'
            high_col = 'high'
            low_col = 'low'
            
            # Ensure float types
            df[close_col] = df[close_col].astype(float)
            df[high_col] = df[high_col].astype(float)
            df[low_col] = df[low_col].astype(float)

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
        finally:
            if own_client:
                futu_client.close()
