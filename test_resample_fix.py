import pandas as pd
import numpy as np

data = {
    'time_key': [
        '2026-03-16 10:30:00', '2026-03-16 11:30:00', '2026-03-16 14:00:00', '2026-03-16 15:00:00',
        '2026-03-17 10:30:00', '2026-03-17 11:30:00', '2026-03-17 14:00:00', '2026-03-17 15:00:00'
    ],
    'close': [1, 2, 3, 4, 5, 6, 7, 8]
}
df = pd.DataFrame(data)
df.set_index(pd.to_datetime(df['time_key']), inplace=True)
df['open'] = df['close']
df['high'] = df['close']
df['low'] = df['close']
df['volume'] = 100

# NEW LOGIC
df['date'] = df.index.date
df['daily_bar_idx'] = df.groupby('date').cumcount() // 2
df_2h = df.groupby(['date', 'daily_bar_idx']).agg({
    'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
})
# Get the actual last timestamp for the group
df_2h.index = df.groupby(['date', 'daily_bar_idx']).apply(lambda x: x.index[-1]).values
print(df_2h)
