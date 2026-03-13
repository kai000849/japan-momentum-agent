import json, glob
import pandas as pd
from pathlib import Path

# 株価CSV読み込み
files = sorted(glob.glob('data/raw/jquants/quotes_*.csv'))
df = pd.read_csv(files[-1])
df['Date'] = pd.to_datetime(df['Date'])

# scan_20251111_SHORT_TERM.json の1銘柄目を取得
f = Path('data/processed/scans/scan_20251111_SHORT_TERM.json')
d = json.loads(f.read_text(encoding='utf-8'))
signal = d['results'][0]
stock_code = signal['stockCode']
scan_date = signal['scanDate']

print(f'=== バックテスト診断 ===')
print(f'対象銘柄: {stock_code}')
print(f'スキャン日: {scan_date}')

# 銘柄データ抽出
code_col = 'Code' if 'Code' in df.columns else df.columns[0]
print(f'コード列名: {code_col}')
print(f'CSVのCode例: {df[code_col].iloc[0]} (type: {type(df[code_col].iloc[0]).__name__})')
print(f'signal stockCode: {stock_code} (type: {type(stock_code).__name__})')

stock_df = df[df[code_col].astype(str) == str(stock_code)].copy()
stock_df = stock_df.sort_values('Date').reset_index(drop=True)
print(f'\n銘柄{stock_code}のデータ件数: {len(stock_df)}')

if not stock_df.empty:
    print(f'日付範囲: {stock_df["Date"].min()} 〜 {stock_df["Date"].max()}')
    signal_dt = pd.to_datetime(scan_date)
    future_df = stock_df[stock_df['Date'] > signal_dt].reset_index(drop=True)
    print(f'signal_dt: {signal_dt}')
    print(f'future_df件数 (scanDate以降): {len(future_df)}')
    if not future_df.empty:
        print(f'future_df先頭:\n{future_df[["Date","Open","High","Low","Close"]].head(3)}')
    else:
        print('→ future_dfが空！ここがPF=0の原因')
        print(f'stock_dfの最後5行:')
        print(stock_df[['Date','Close']].tail(5))
