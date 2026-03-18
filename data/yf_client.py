import yfinance as yf
import pandas as pd
from datetime import datetime

class YFinanceClient:
    def __init__(self):
        pass

    def get_historical_klines(self, code, start_date, end_date, interval="1h"):
        """
        Fetch historical K-lines from Yahoo Finance and format it 
        to match the Futu API dataframe structure.
        """
        try:
            ticker = yf.Ticker(code)
            
            # auto_adjust=True ensures we get back-adjusted prices to prevent split/dividend gaps
            df = ticker.history(start=start_date, end=end_date, interval=interval, auto_adjust=True)
            
            if df is None or df.empty:
                print(f"Warning: No data found for {code} from {start_date} to {end_date}")
                return None
                
            # Handle potential multi-index columns (sometimes occurs in certain yfinance versions)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Ensure column names are standard (yfinance occasionally changes case)
            current_cols = {c.lower(): c for c in df.columns}
            rename_map = {}
            for target in ["open", "high", "low", "close", "volume"]:
                if target in current_cols:
                    rename_map[current_cols[target]] = target
            
            if rename_map:
                df = df.rename(columns=rename_map)
            
            # Validate required columns
            required = ["open", "high", "low", "close", "volume"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                print(f"Error: Missing required columns {missing} for {code}")
                return None

            # Create a time_key column from the Datetime index (normalized to naive UTC)
            if df.index.tz is not None:
                df.index = df.index.tz_convert('UTC').tz_localize(None)
            df['time_key'] = df.index
            
            # Add a fake turnover column since yfinance doesn't provide it natively
            df['turnover'] = df['volume'] * ((df['high'] + df['low']) / 2)
            
            return df
            
        except Exception as e:
            import traceback
            print(f"Failed to fetch data from Yahoo Finance for {code}: {e}")
            traceback.print_exc()
            return None
