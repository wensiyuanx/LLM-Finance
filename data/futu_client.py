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
                return None

            all_pages.append(data)

            if page_req_key is None:   # no more pages
                break

        import pandas as pd
        result = pd.concat(all_pages, ignore_index=True)
        print(f"[FutuClient] {code}: fetched {len(result)} candles ({start_date} → {end_date})")
        return result

if __name__ == "__main__":
    # Test connection
    client = FutuClient()
    client.connect()
    # Replace with a real code to test
    # data = client.get_historical_klines("HK.00700", "2024-01-01", "2024-02-01")
    # if data is not None:
    #     print(data.head())
    client.close()
