import pandas as pd
from agents.jquants_fetcher import load_latest_quotes
from agents.scanner import calculate_rsi, calculate_moving_averages

df = load_latest_quotes()
scan_dt = pd.to_datetime('2026-03-10')
grouped = df.groupby('Code')

ma_fail = 0
high_fail = 0
rsi_fail = 0
pass_count = 0
data75_fail = 0

for code, group in grouped:
    group = group.sort_values('Date').reset_index(drop=True)
    mask = group['Date'] == scan_dt
    if not mask.any():
        continue
    idx = group[mask].index[-1]
    if idx < 25:
        continue
    g = group.loc[:idx].copy()
    closes = g['Close'].astype(float)
    if closes.iloc[-1] <= 0:
        continue
    ma = calculate_moving_averages(closes)
    if ma['ma75']['current'] is None:
        data75_fail += 1
        continue
    if not all([ma['ma5']['rising'], ma['ma25']['rising'], ma['ma75']['rising']]):
        ma_fail += 1
        continue
    high = float(closes.tail(252).max())
    ratio = closes.iloc[-1] / high * 100
    if ratio < 85:
        high_fail += 1
        continue
    rsi = calculate_rsi(closes, 14).iloc[-1]
    if not (50 <= rsi <= 75):
        rsi_fail += 1
        continue
    pass_count += 1

print(f"75日MA計算不可（データ不足）: {data75_fail}銘柄")
print(f"MA条件で脱落:                {ma_fail}銘柄")
print(f"高値比条件で脱落:            {high_fail}銘柄")
print(f"RSI条件で脱落:               {rsi_fail}銘柄")
print(f"全条件通過:                  {pass_count}銘柄")
