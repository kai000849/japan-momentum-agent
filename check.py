import json, glob
import pandas as pd
from pathlib import Path

# === スキャンファイルのscanDate確認 ===
print('=== スキャンファイル確認 ===')
f = Path('data/processed/scans/scan_20251111_SHORT_TERM.json')
d = json.loads(f.read_text(encoding='utf-8'))
print('scanDate in file:', d.get('scanDate'))
print('results[0].scanDate:', d['results'][0].get('scanDate') if d.get('results') else 'empty')
print('result count:', len(d.get('results', [])))

# === 株価CSVの日付範囲確認 ===
print('\n=== 株価CSV確認 ===')
files = sorted(glob.glob('data/raw/jquants/quotes_*.csv'))
if not files:
    print('CSVファイルが見つかりません')
else:
    print('CSVファイル:', files[-1])
    df = pd.read_csv(files[-1])
    df['Date'] = pd.to_datetime(df['Date'])
    print('Date範囲:', df['Date'].min(), '〜', df['Date'].max())
    print('総レコード数:', len(df))
    print('2025-11-12のデータ件数:', len(df[df['Date'] == '2025-11-12']))
    print('2025-11-11のデータ件数:', len(df[df['Date'] == '2025-11-11']))
    print('2025-11-28のデータ件数:', len(df[df['Date'] == '2025-11-28']))
    print('2026-03-10のデータ件数:', len(df[df['Date'] == '2026-03-10']))
    print('2026-03-11のデータ件数:', len(df[df['Date'] == '2026-03-11']))
