from datetime import datetime, time
import pytz
from database.models import MarketType

# Chinese holidays 2026 (YYYY-MM-DD format)
CHINESE_HOLIDAYS_2026 = [
    "2026-01-01",  # New Year's Day
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",  # Spring Festival
    "2026-04-04", "2026-04-05", "2026-04-06",  # Qingming Festival
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # Labor Day
    "2026-06-19",  # Dragon Boat Festival
    "2026-09-25",  # Mid-Autumn Festival
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",  # National Day
]

# Hong Kong holidays 2026 (YYYY-MM-DD format)
HK_HOLIDAYS_2026 = [
    "2026-01-01",  # New Year's Day
    "2026-02-17", "2026-02-18", "2026-02-19",  # Lunar New Year
    "2026-04-03",  # Good Friday
    "2026-04-04",  # Day following Good Friday
    "2026-04-06",  # Easter Monday
    "2026-04-05",  # Ching Ming Festival (Observed on 4.6)
    "2026-05-01",  # Labour Day
    "2026-05-24",  # Birthday of the Buddha
    "2026-06-19",  # Tuen Ng Festival
    "2026-07-01",  # HK SAR Establishment Day
    "2026-09-26",  # Day following Mid-Autumn Festival
    "2026-10-01",  # National Day
    "2026-10-19",  # Chung Yeung Festival
    "2026-12-25", "2026-12-26",  # Christmas Day
]

def is_holiday(market_type: MarketType) -> bool:
    """
    Check if today is a holiday for the given market.
    """
    tz = pytz.timezone('Asia/Shanghai')
    today = datetime.now(tz).strftime("%Y-%m-%d")

    holidays = []
    if market_type == MarketType.A_SHARE:
        holidays = CHINESE_HOLIDAYS_2026
    elif market_type == MarketType.HK_SHARE:
        holidays = HK_HOLIDAYS_2026

    return today in holidays

def is_market_open(market_type: MarketType) -> bool:
    """
    Checks if the current time is within market hours for the given market.
    Including holiday checks.
    """
    # CST = China Standard Time (UTC+8)
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    
    # 1. Market is closed on weekends
    if now.weekday() >= 5:
        return False
        
    # 2. Market is closed on holidays
    if is_holiday(market_type):
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
