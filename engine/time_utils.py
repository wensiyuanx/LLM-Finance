from datetime import datetime, time
import pytz
from database.models import MarketType

def is_market_open(market_type: MarketType) -> bool:
    """
    Checks if the current time is within market hours for the given market.
    
    A-Share: 09:30-11:30, 13:00-15:00 (CST)
    HK-Share: 09:30-12:00, 13:00-16:00 (CST)
    """
    # CST = China Standard Time (UTC+8)
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    
    # Market is closed on weekends
    if now.weekday() >= 5:
        return False
        
    current_time = now.time()
    
    if market_type == MarketType.A_SHARE:
        # 09:30 - 11:30
        morning_start = time(9, 30)
        morning_end = time(11, 30)
        # 13:00 - 15:00
        afternoon_start = time(13, 0)
        afternoon_end = time(15, 0)
        
        return (morning_start <= current_time <= morning_end) or \
               (afternoon_start <= current_time <= afternoon_end)
               
    elif market_type == MarketType.HK_SHARE:
        # 09:30 - 12:00
        morning_start = time(9, 30)
        morning_end = time(12, 0)
        # 13:00 - 16:00
        afternoon_start = time(13, 0)
        afternoon_end = time(16, 0)
        
        return (morning_start <= current_time <= morning_end) or \
               (afternoon_start <= current_time <= afternoon_end)
               
    return False

if __name__ == "__main__":
    # Test script
    print(f"A-Share Open: {is_market_open(MarketType.A_SHARE)}")
    print(f"HK-Share Open: {is_market_open(MarketType.HK_SHARE)}")
