import os
import sys

# Monkey patch Futu logger to avoid Sandbox PermissionError
import logging
import logging.handlers

class MockFileHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__()
    def emit(self, record):
        pass

logging.handlers.TimedRotatingFileHandler = MockFileHandler

from futu import OpenQuoteContext, OpenHKTradeContext, TrdEnv, KLType, AuType, RET_OK, RET_ERROR
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11111"))

class FutuClient:
    def __init__(self):
        self.quote_ctx = None
        self.trade_ctx = None

    def connect(self):
        try:
            print(f"Connecting to FutuOpenD {FUTU_HOST}:{FUTU_PORT}")
            self.quote_ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            # You can initialize trade ctx as well, e.g., OpenHKTradeContext
            # self.trade_ctx = OpenHKTradeContext(host=FUTU_HOST, port=FUTU_PORT, trd_env=TrdEnv.SIMULATE)
            print("Connected to FutuOpenD Quote Context.")
            return True
        except Exception as e:
            print(f"Failed to connect to FutuOpenD: {e}")
            if "ECONNREFUSED" in str(e) or "10061" in str(e):
                print("\n" + "="*50)
                print("CRITICAL: Connection refused. Is FutuOpenD running?")
                print(f"Please ensure FutuOpenD is open and listening on {FUTU_HOST}:{FUTU_PORT}")
                print("="*50 + "\n")
            return False

    def close(self):
        if self.quote_ctx:
            self.quote_ctx.close()
            print("Quote context closed.")
        if self.trade_ctx:
            self.trade_ctx.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=False)
    def get_historical_klines(self, code, start_date, end_date, ktype=KLType.K_60M, autype=AuType.QFQ):
        """
        Fetch historical K-lines with automatic pagination.
        Using QFQ (Forward Adjust) by default to prevent technical indicators from corrupting after corporate actions.
        """
        if not self.quote_ctx:
            print("FutuClient not connected.")
            return None

        all_pages = []
        page_req_key = None

        while True:
            ret, data, page_req_key = self.quote_ctx.request_history_kline(
                code, start=start_date, end=end_date,
                ktype=ktype, autype=autype,
                max_count=1000, page_req_key=page_req_key
            )
            if ret != RET_OK:
                print(f"Request history kline failed: {data}")
                raise Exception(f"Futu API Error: {data}")

            all_pages.append(data)

            if page_req_key is None:   # no more pages
                break

        import pandas as pd
        result = pd.concat(all_pages, ignore_index=True)
        print(f"[FutuClient] {code}: fetched {len(result)} candles ({start_date} → {end_date})")
        return result

    def get_realtime_quote(self, code: str):
        """
        Fetch real-time snapshot quote (including Bid 1 / Ask 1) for a specific asset.
        """
        if not self.quote_ctx:
            return None
            
        ret, data = self.quote_ctx.get_market_snapshot([code])
        if ret == RET_OK and not data.empty:
            return data.iloc[0].to_dict()
        else:
            logger.warning(f"Failed to get realtime quote for {code}: {data}")
            return None

if __name__ == "__main__":
    # Test connection
    client = FutuClient()
    client.connect()
    # Replace with a real code to test
    # data = client.get_historical_klines("HK.00700", "2024-01-01", "2024-02-01")
    # if data is not None:
    #     print(data.head())
    client.close()
