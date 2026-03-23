import os
import platform
import pandas as pd
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg') # Set non-interactive backend for background threads
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

def generate_kline_chart(df: pd.DataFrame, code: str, output_dir: str = "output/charts"):
    """
    Generates a 90-day K-line chart for the given dataframe and saves it to output_dir.
    """
    if df is None or df.empty:
        print(f"No data to plot for {code}")
        return None

    # Ensure output directory exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Clone and prepare data
    plot_df = df.copy()
    
    # Ensure index is DatetimeIndex
    if not isinstance(plot_df.index, pd.DatetimeIndex):
        if 'time_key' in plot_df.columns:
            plot_df['time_key'] = pd.to_datetime(plot_df['time_key'])
            plot_df.set_index('time_key', inplace=True)
        else:
            # Try to convert current index
            plot_df.index = pd.to_datetime(plot_df.index)

    # Filter to last 180 trading days
    if len(plot_df) > 180:
        plot_df = plot_df.iloc[-180:]

    # Define chart file path
    timestamp = datetime.now().strftime("%Y%m%d")
    filename = f"{code.replace('.', '_')}_{timestamp}.png"
    filepath = os.path.join(output_dir, filename)

    # --- Premium Flat Design Styling ---
    # Colors inspired by TradingView / Binance
    up_color = '#26a69a'    # Modern Teal
    down_color = '#ef5350'  # Modern Red
    bg_color = '#131722'    # Deep Dark Blue
    grid_color = '#2a2e39'  # Subtle dark grid
    text_color = '#d1d4dc'  # Light Gray Text
    
    mc = mpf.make_marketcolors(
        up=up_color, down=down_color,
        edge='inherit',
        wick='inherit',
        volume='inherit',
        ohlc='inherit'
    )
    
    s = mpf.make_mpf_style(
        base_mpf_style='charles', 
        marketcolors=mc,
        facecolor=bg_color,
        edgecolor=grid_color,
        gridcolor=grid_color,
        gridstyle='-',
        y_on_right=True,
        rc={
            'axes.labelcolor': text_color,
            'axes.edgecolor': grid_color,
            'xtick.color': text_color,
            'ytick.color': text_color,
            'figure.facecolor': bg_color,
            'text.color': text_color,
            'grid.alpha': 0.4
        }
    )

    # Plot
    try:
        # We'll plot Candlesticks, Volume, and indicators in panels
        add_plots = []
        
        # Panel 0: SMAs
        if 'SMA_5' in plot_df.columns and not plot_df['SMA_5'].isnull().all():
            add_plots.append(mpf.make_addplot(plot_df['SMA_5'], color='#f0b90b', width=1.0, panel=0, alpha=0.8))
        if 'SMA_20' in plot_df.columns and not plot_df['SMA_20'].isnull().all():
            add_plots.append(mpf.make_addplot(plot_df['SMA_20'], color='#2196f3', width=1.0, panel=0, alpha=0.8))

        # MACD on Panel 2
        if 'MACD' in plot_df.columns and 'MACD_Signal' in plot_df.columns:
            add_plots.append(mpf.make_addplot(plot_df['MACD'], panel=2, color='#2962FF', width=1.0, secondary_y=False))
            add_plots.append(mpf.make_addplot(plot_df['MACD_Signal'], panel=2, color='#FF6D00', width=1.0, secondary_y=False))
            if 'MACD_Histogram' in plot_df.columns:
                # Flat style histogram
                hist_colors = [up_color if x >= 0 else down_color for x in plot_df['MACD_Histogram']]
                add_plots.append(mpf.make_addplot(plot_df['MACD_Histogram'], type='bar', panel=2, color=hist_colors, alpha=0.4, secondary_y=False))

        # RSI on Panel 3
        if 'RSI_14' in plot_df.columns:
            # RSI Area fill logic is complex in mpf, we'll stick to a clean line + horizontal levels
            add_plots.append(mpf.make_addplot(plot_df['RSI_14'], panel=3, color='#787b86', width=1.2, ylabel='RSI'))
            add_plots.append(mpf.make_addplot([70]*len(plot_df), panel=3, color='#434651', linestyle='--', width=0.8))
            add_plots.append(mpf.make_addplot([30]*len(plot_df), panel=3, color='#434651', linestyle='--', width=0.8))

        # Header Info
        latest_price = plot_df['close'].iloc[-1]
        latest_time = plot_df.index[-1].strftime('%m-%d %H:%M')


        change = latest_price - plot_df['close'].iloc[-2]
        change_pct = (change / plot_df['close'].iloc[-2]) * 100
        color_tag = "▲" if change >= 0 else "▼"
        title_color = up_color if change >= 0 else down_color
        
        title = f"{code} | {latest_time} | Close: {latest_price:.2f} | {color_tag} {change:+.2f} ({change_pct:+.2f}%)"

        # Custom Panel Ratios and spacing
        panel_ratios = [6, 1.5, 2.5, 2.5] 

        # We'll use returnfig=True to manually add advanced visual elements
        fig, axlist = mpf.plot(plot_df, 
                               type='candle', 
                               style=s,
                               ylabel='Price',
                               volume=True, 
                               addplot=add_plots,
                               savefig=dict(fname=filepath, dpi=140, bbox_inches='tight'),
                               datetime_format='%m-%d %H:%M', xrotation=45,
                               tight_layout=False, # Use manual adjustments
                               panel_ratios=panel_ratios,
                               figratio=(16, 12),
                               figscale=1.5,
                               update_width_config=dict(candle_linewidth=0.6, candle_width=0.7),
                               returnfig=True)
        
        # 1. Add RSI Background Band (Panel 3)
        # axlist is organized by elements. Indexing varies by mpf version, 
        # usually [0]=price, [1]=indicators, [2]=volume, etc.
        # Let's find the RSI axis (usually the last one)
        rsi_ax = axlist[-2] # In 4 panel setup with volume=True, usually axlist[-2] is panel 3 (RSI)
        rsi_ax.fill_between(range(len(plot_df)), 30, 70, color='#2a2e39', alpha=0.3, zorder=0)

        # 2. Add Super Title (Headed Outside) - Use platform-compatible font with symbol support
        import matplotlib.font_manager as fm
        available_fonts = set(f.name for f in fm.fontManager.ttflist)
        
        if platform.system() == 'Darwin':
            target_fonts = ['Arial Unicode MS', 'PingFang HK', 'Heiti SC', 'Helvetica']
        else:
            target_fonts = ['DejaVu Sans', 'Liberation Sans', 'Arial', 'sans-serif']
            
        font_name = 'sans-serif'
        for f in target_fonts:
            if f in available_fonts:
                font_name = f
                break
                
        plt.rcParams['font.family'] = font_name
        fig.suptitle(title, color=title_color, fontsize=20, weight='bold', y=0.97, fontname=font_name)
        
        # 3. Add Subtle Branded Watermark
        fig.text(0.5, 0.5, 'QUANTO-BOT AI ANALYSIS', color=text_color, alpha=0.05,
                 fontsize=40, weight='bold', rotation=30, ha='center', va='center')

        # 4. Final Layout Polish
        plt.subplots_adjust(hspace=0.05) # Tighten panel gaps
        
        # Re-save with refinements
        fig.savefig(filepath, dpi=140, bbox_inches='tight', facecolor=bg_color)
        plt.close(fig)
        
        print(f"Chart generated for {code}: {filepath}")
        return filepath
    except Exception as e:
        print(f"Failed to generate chart for {code}: {e}")
        return None

if __name__ == "__main__":
    # Test with dummy data if run directly
    import numpy as np
    dates = pd.date_range('2024-01-01', periods=100)
    data = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 100,
        'high': np.random.randn(100).cumsum() + 105,
        'low': np.random.randn(100).cumsum() + 95,
        'close': np.random.randn(100).cumsum() + 100,
        'volume': np.random.randint(1000, 10000, 100),
        'SMA_5': np.random.randn(100).cumsum() + 100,
        'SMA_20': np.random.randn(100).cumsum() + 100,
        'MACD': np.random.randn(100),
        'MACD_Signal': np.random.randn(100),
        'MACD_Histogram': np.random.randn(100),
        'RSI_14': np.random.randint(20, 80, 100)
    }, index=dates)
    generate_kline_chart(data, "TEST_STOCK")
