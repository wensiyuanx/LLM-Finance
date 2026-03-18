import sys
import os
import argparse
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.futu_client import FutuClient
from data.yf_client import YFinanceClient
from strategy.indicators import calculate_indicators
from scripts.visualizer import generate_kline_chart
from database.models import MarketType

def main():
    parser = argparse.ArgumentParser(description="Standalone K-line Chart Generator")
    parser.add_argument("--code", type=str, required=True, help="Stock code (e.g., HK.00700, AAPL, SZ.000001)")
    parser.add_argument("--days", type=int, default=180, help="Number of days to fetch (default 180)")
    parser.add_argument("--market", type=str, choices=["A", "HK", "US"], help="Market type (A, HK, US). Auto-detected if omitted.")
    
    args = parser.parse_args()
    code = args.code
    
    # Simple market detection logic if not provided
    market = args.market
    if not market:
        if code.startswith("HK."):
            market = "HK"
        elif code.startswith("SH.") or code.startswith("SZ."):
            market = "A"
        else:
            market = "US"

    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")

    df = None
    if market in ["A", "HK"]:
        futu = FutuClient()
        if futu.connect():
            df = futu.get_historical_klines(code, start_date=start_date, end_date=end_date)
            futu.close()
            # Futu data needs formatting
            if df is not None:
                df['time_key'] = pd.to_datetime(df['time_key'])
                df.set_index('time_key', inplace=True)
        else:
            print("Error: Could not connect to FutuOpenD.")
            return
    else:
        yf = YFinanceClient()
        df = yf.get_historical_klines(code, start_date=start_date, end_date=end_date)
        if df is not None:
            df.set_index('time_key', inplace=True)

    if df is not None and not df.empty:
        # Calculate indicators for the chart
        df = calculate_indicators(df)
        path = generate_kline_chart(df, code)
        if path:
            print(f"\nSuccess! Chart saved to: {path}")
    else:
        print(f"Error: No data found for {code}")

if __name__ == "__main__":
    import pandas as pd
    # Set protobuf env var as a fallback in script
    os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
    main()
