import os
import sys
import pandas as pd

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database.db import SessionLocal
from database.models import KLineData
from strategy.indicators import calculate_indicators
from strategy.logic import generate_signals

def diagnose_asset(code):
    db = SessionLocal()
    try:
        # Load last 300 rows from DB
        res = db.query(KLineData).filter(KLineData.code == code).order_by(KLineData.time_key.asc()).all()
        if not res:
            print(f"No data for {code}")
            return
            
        data = []
        for r in res:
            data.append({
                'open': r.open_price,
                'high': r.high_price,
                'low': r.low_price,
                'close': r.close_price,
                'volume': r.volume,
                'time_key': r.time_key
            })
        
        df = pd.DataFrame(data)
        df.set_index('time_key', inplace=True)
        
        # Calculate indicators
        working_df = calculate_indicators(df.copy())
        
        latest = working_df.iloc[-1]
        prev = working_df.iloc[-2]
        
        print(f"\n=== Diagnosis for {code} (Latest: {latest.name}) ===")
        print(f"Close: {latest['close']:.2f}")
        print(f"SMA_5: {latest.get('SMA_5', 0):.2f} | SMA_20: {latest.get('SMA_20', 0):.2f} | SMA_200: {latest.get('SMA_200', 0):.2f}")
        print(f"RSI_14: {latest.get('RSI_14', 0):.2f}")
        print(f"Boll Lower: {latest.get('BOLL_LOWER', 0):.2f}")
        print(f"Volume: {latest['volume']:.0f} | Vol SMA 5: {latest.get('VOL_SMA_5', 0):.0f}")
        
        # Run signal logic
        action, reason, score, is_trend_entry = generate_signals(df, current_position=0, avg_cost=0, code=code)
        print(f"\nFinal Action: {action.name}")
        print(f"Reason: {reason}")
        print(f"Score: {score:.1f} | Is Trend: {is_trend_entry}")
        
        # Internal check of signal list logic
        buy_signals = []
        in_downtrend = latest['close'] < latest.get('SMA_200', latest.get('SMA_60', 0))
        
        # 1. MA Crossover
        if prev['SMA_5'] <= prev['SMA_20'] and latest['SMA_5'] > latest['SMA_20']:
            if latest['volume'] > latest['VOL_SMA_5']:
                buy_signals.append("MA Gold Cross (Confirmed)")
            else:
                buy_signals.append("MA Gold Cross (Low Volume - REJECTED)")
        
        # 2. RSI
        if latest['RSI_14'] < 35:
            if in_downtrend:
                buy_signals.append(f"RSI Oversold ({latest['RSI_14']:.1f}) - REJECTED (In Downtrend)")
            else:
                buy_signals.append(f"RSI Oversold ({latest['RSI_14']:.1f}) (Accepted)")
        
        # 3. Boll
        if latest['close'] <= latest['BOLL_LOWER'] * 1.01:
            if in_downtrend:
                buy_signals.append("Boll Lower Touch - REJECTED (In Downtrend)")
            else:
                buy_signals.append("Boll Lower Touch (Accepted)")
                
        print(f"Active Buy Signal Components: {buy_signals}")

    finally:
        db.close()

if __name__ == "__main__":
    diagnose_asset("HK.00700")
    print("-" * 40)
    diagnose_asset("SZ.159915")
