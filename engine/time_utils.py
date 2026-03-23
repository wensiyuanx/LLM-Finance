from datetime import datetime, time
import pytz
from database.models import MarketType

# Chinese holidays (YYYY-MM-DD format)
# Note: This is a simplified list. In production, use an API or comprehensive library
CHINESE_HOLIDAYS_2024 = [
    "2024-01-01",  # New Year's Day
    "2024-02-10", "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16", "2024-02-17",  # Spring Festival
    "2024-04-04", "2024-04-05", "2024-04-06",  # Qingming Festival
    "2024-05-01", "2024-05-02", "2024-05-03", "2024-05-04", "2024-05-05",  # Labor Day
    "2024-06-10",  # Dragon Boat Festival
    "2024-09-15", "2024-09-16", "2024-09-17",  # Mid-Autumn Festival
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-05", "2024-10-06", "2024-10-07",  # National Day
]

# Hong Kong holidays (YYYY-MM-DD format)
HK_HOLIDAYS_2024 = [
    "2024-01-01",  # New Year's Day
    "2024-02-10", "2024-02-11", "2024-02-12",  # Lunar New Year
    "2024-02-13",  # Fourth day of Lunar New Year
    "2024-03-29",  # Good Friday
    "2024-04-01",  # Easter Monday
    "2024-04-04",  # Ching Ming Festival
    "2024-05-01",  # Labour Day
    "2024-05-15",  # Birthday of the Buddha
    "2024-06-10",  # Tuen Ng Festival
    "2024-07-01",  # HK SAR Establishment Day
    "2024-09-18",  # Day following Mid-Autumn Festival
    "2024-10-01",  # National Day
    "2024-10-11",  # Chung Yeung Festival
    "2024-12-25", "2024-12-26",  # Christmas Day
]

def is_holiday(market_type: MarketType) -> bool:
    """
    Check if today is a holiday for the given market.
    """
    tz = pytz.timezone('Asia/Shanghai')
    today = datetime.now(tz).strftime("%Y-%m-%d")

    holidays = []
    if market_type == MarketType.A_SHARE:
        holidays = CHINESE_HOLIDAYS_2024
    elif market_type == MarketType.HK_SHARE:
        holidays = HK_HOLIDAYS_2024

    return today in holidays

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
