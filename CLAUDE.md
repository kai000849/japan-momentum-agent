# Japan Momentum Agent - プロジェクト引き継ぎメモ

## このファイルの使い方
新しいチャットセッションを開始したら、このファイルをそのままClaudeに貼り付けてください。すぐに作業に入れます。

---

## プロジェクト概要
日本株モメンタム投資の自動化AIエージェント。平日夕方18時・翌朝6時の2回、銘柄スクリーニングを実行しSlackに通知する。

- **GitHubリポジトリ**: https://github.com/kai000849/japan-momentum-agent
- **ローカルパス**: `C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent`
- **Slack通知チャンネル**: #all-japan-momentum-agent

---

## 技術スタック

- Python（メイン言語）
- J-Quants API（株価データ取得・ライトプラン・月1,650円）
- EDINET API（決算情報取得）
- GitHub Actions（自動実行）
- Slack Webhook（通知）
- Claude API（Haiku 4.5・anthropic SDK使用・APIキー登録済み・動作確認済み）
- yfinance（米国ETFデータ取得・無料）

---

## ファイル構成

```
japan-momentum-agent/
├── main.py                          # エントリーポイント
├── requirements.txt                 # python-dotenv・anthropic・yfinance追加済み
├── scheduler_setup.py
├── .github/workflows/
│   └── daily_report.yml             # GitHub Actions自動実行設定
├── agents/
│   ├── scanner.py                   # スクリーニング（3モード）
│   ├── jquants_fetcher.py           # 株価データ取得（J-Quants V2 API）
│   ├── edinet_fetcher.py            # 開示情報取得
│   ├── edinet_analyzer.py           # 決算PDFをClaude APIで読解・スコアリング
│   ├── momentum_qualifier.py        # モメンタム判定（バッチ処理・フェーズ3結果記録対応済み）
│   ├── us_market_scanner.py         # 米国セクターETF17本モメンタム
│   ├── us_theme_extractor.py        # 米財務メディアからキーワード抽出
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理
│   ├── slack_notifier.py            # Slack通知
│   ├── utils.py                     # 共通ユーティリティ（get_anthropic_key等）
│   └── analyst.py                   # 銘柄分析コメント（未有効化）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   ├── raw/edinet_pdfs/             # 決算短信PDFキャッシュ
│   ├── processed/scans/             # スキャン結果JSON（GitHub Actionsでキャッシュ済み）
│   ├── processed/us_scans/          # 米ETFスキャン結果JSON
│   └── processed/us_themes/         # 米テーマ抽出結果JSON
└── memory/
    ├── trade_log.json               # 取引履歴（GitHub Actionsでキャッシュ済み）
    ├── disclosure_log.json          # 開示情報履歴
    └── qualify_log.json             # モメンタム判定ログ（10営業日後結果も自動記録）
```

---

## GitHub Secrets
| キー名 | 内容 |
|---|---|
| `JQUANTS_API_KEY` | J-Quants APIキー（登録済み） |
| `EDINET_API_KEY` | EDINET APIキー（登録済み） |
| `SLACK_WEBHOOK_URL` | Slack Webhook URL（登録済み） |
| `ANTHROPIC_API_KEY` | **登録済み・動作確認済み**（2026/03/16更新） |

---

## GitHub Actions スケジュール

| 時刻 | ジョブ | 内容 |
|---|---|---|
| 夕方 18:00 JST（UTC 09:00・月〜金） | evening-scan | スキャン＋シグナル＋保有状況（メイン通知） |
| 朝 06:00 JST（UTC 21:00・月〜金） | morning-scan | 再スキャン＋シグナル＋保有状況＋米市場スキャン |

**キャッシュ対象：** `memory/trade_log.json` と `data/processed/scans/` の両方をActions間で引き継ぎ済み。

**エラー監視：** 各ステップ失敗時にSlackへcurlで直接通知（fetch/scan/status/us_scanの4ステップを監視）。

---

## スクリーニングモード
### 急騰モード（SHORT_TERM）
- 前日比+3%以上
- 出来高が25日平均の2倍以上
- 急騰スコア = 前日比(%) × 出来高倍率

### モメンタムモード（MOMENTUM）
- 5日・25日・75日MA全て上昇中（5日MA必須）
- 現在株価が52週高値の**90%以上**
- RSI(14日)が**55〜72**の範囲
- スコア = RSI × 高値比率 × 全MA上昇ボーナス × 新高値ボーナス × 出来高トレンドボーナス

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- Claude APIで決算PDFを読解してポジ/ネガスコアリング（動作確認済み）

---

## PF計算ロジック（main.py）

`_calc_pf_for_mode(mode)`関数でPFを計算する。

- ローカルCSVの最終日から**15営業日前**の日付でスキャン→バックテスト→PF算出
- ファイルキャッシュ依存を廃止（旧：過去scanファイルを参照 → PF=0になっていた）
- PF=0の根本原因：GitHub Actionsがクリーン環境のためscansディレクトリが毎回空だった

---

## モメンタム判定モジュール（momentum_qualifier.py）

### 設計思想
出来高を伴った急騰が「短期的な加熱」なのか「構造的変化を伴った中長期モメンタムの始まり」なのかを2段階で判定する。

### ステージ1: 出来高継続パターン判定（数値）
- 急騰後3日間の出来高が急騰日の50%以上を維持しているか
- 急騰後の株価が急騰終値の97%以上を維持しているか

### ステージ2: Claude APIによる構造的変化判定（バッチ処理）
- ステージ1通過銘柄を全てまとめて1回のAPI呼び出しで判定
- web_search付きで最新ニュース・EDINET開示を確認
- 「構造的変化あり/なし」＋理由コメントを出力
- **トークン消費：旧~120,000 → 新~20,000（約1/6に削減済み）**

### 判定結果
| 結果 | 条件 |
|---|---|
| STRONG | ステージ1✅ + Claude判定✅ |
| WATCH | ステージ1✅ + APIキーなし |
| WEAK | ステージ1✅ + Claude判定❌ |
| NOISE | ステージ1❌ |

### フェーズ3: 10営業日後の結果自動記録（実装済み）
- `qualify_signals()`実行のたびに`record_outcomes()`が自動呼び出される
- `outcome=null`のエントリで10営業日以上経過しているものに株価リターンを記録
- `get_outcome_stats()`でSTRONG/WEAK/NOISEごとの勝率・平均リターンを集計
- 記録が5件以上溜まるとSlack通知に精度サマリーが追加表示される

---

## 米市場スキャン
### ETFモメンタムスキャン（us_market_scanner.py）
- 監視ETF: 17本（XLK・SOXX・XLV・XLE・ITA・ARKK・AIQ・ROBO等）
- anthropic SDK + web_searchマルチターンループ対応済み

### キーワード抽出スキャン（us_theme_extractor.py）
- 情報ソース: Yahoo Finance・WSJ・MarketWatch・FT・Investing.com（5メディア・約100件/日）
- anthropic SDK + web_searchマルチターンループ対応済み

---

## 共通ユーティリティ（agents/utils.py）
- `get_anthropic_key()` — 環境変数 → config.yaml の順でAPIキーを取得
- edinet_analyzer・us_market_scanner・us_theme_extractor・momentum_qualifierから参照

---

## ペーパートレード設定
- 仮想資金：300万円
- 1銘柄あたり最大50万円
- 最大同時保有：10銘柄
- 発注条件：バックテストのPF（プロフィットファクター）≥ 1.2

---

## バックテスト設定
- エントリー：シグナル翌営業日の始値
- 損切り：-5%
- 利確：+15%
- 最大保有：10営業日

---

## フェーズ進捗

### ✅ フェーズ1（基盤構築・完了）
（省略・過去の修正履歴はGitログ参照）

### ✅ フェーズ2（Claude API統合・完全動作確認済み）
全機能実装・動作確認済み。詳細はGitログ参照。

### ✅ フェーズ2.5（品質改善・2026/03/17完了）
1. **PF=0再発修正** — `_calc_pf_for_mode()`でCSV最終日-15営業日ベースに統一
2. **`agents/utils.py`共通化** — `_get_anthropic_key()`を4ファイルから削除してutils.pyに集約
3. **GitHub Actionsエラー監視** — 失敗ステップをSlack通知（curlで直接送信）
4. **フェーズ3入口実装** — qualify_logへの10営業日後結果自動記録（`record_outcomes()`）

### 🔲 フェーズ3（過去パターンの蓄積と学習）
- qualify_log.jsonへの蓄積・結果記録は実装済み
- 今後：蓄積データを使った判定ロジック改善・精度レポート自動化

### 🔲 フェーズ4（投資判断をエージェントに任せる）
- かいさん自身の投資判断ロジックをプロンプトに組み込む

---

## 現在の状況（2026/03/17夜時点）
- 夕方18時・翌朝6時にSlack自動通知が稼働中（朝はus_scan含む）
- **ANTHROPIC_API_KEY：登録済み・全機能動作確認済み**
- **Consoleはブラウザの翻訳をオフにすれば正常動作する**（翻訳がHTMLを壊すバグ）
- PF計算ロジック修正済み（次回実行から反映）
- フェーズ3の結果蓄積が自動で始まっている

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・推奨）
python main.py --mode fetch --days 150

# 日本株スキャン（モメンタム判定も自動実行）
python main.py --mode scan

# 米市場スキャン（ETF＋キーワード）
python main.py --mode us_scan

# バックテストまで
python main.py --mode backtest

# 全パイプライン
python main.py --mode full

# ポートフォリオ確認
python main.py --mode status

# GitHubに反映
git add .
git commit -m "変更内容のメモ"
git push

# GitHubの変更をパソコンに取り込む
git pull
```

---

## 注意事項
- `config.yaml` はGitに含めない（.gitignoreで除外済み）
- `.env`はGitに含めない（.gitignoreで除外済み）← **重要：過去に誤コミットしてGitHubにブロックされた経験あり**
- APIキーは絶対にコードに直書きしない
- J-Quantsライトプラン：最新データが取得可能
- Windowsのコマンドプロンプトで作業
- **ファイル編集はWindows-MCP:FileSystemツールで直接行う（PowerShellはgitでハングするため複雑なコマンドは使わない）**
- **GitHub上での直接編集は避ける**
- **Consoleを開くときは必ずブラウザの翻訳をオフに！**（翻訳がHTMLを壊してエラーになる）
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**
