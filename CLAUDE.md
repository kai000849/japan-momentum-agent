# Japan Momentum Agent - プロジェクト引き継ぎメモ

## このファイルの使い方
新しいチャットセッションを開始したら、このファイルをそのままClaudeに貼り付けてください。すぐに作業に入れます。

---

## プロジェクト概要
日本株モメンタム投資の自動化AIエージェント。毎朝6:30米市場スキャン・8:00日本株スキャン・夕方18:00メイン通知の3ジョブ構成でSlackに通知する。

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
- pdfplumber（決算PDF→テキスト抽出・トークン節約用）

---

## ファイル構成

```
japan-momentum-agent/
├── main.py                          # エントリーポイント
├── requirements.txt
├── scheduler_setup.py
├── .github/workflows/
│   └── daily_report.yml             # GitHub Actions自動実行設定
├── agents/
│   ├── scanner.py                   # スクリーニング（3モード）
│   ├── jquants_fetcher.py           # 株価データ取得（J-Quants V2 API）
│   ├── edinet_fetcher.py            # 開示情報取得
│   ├── edinet_analyzer.py           # 決算PDFをClaude APIで読解・スコアリング
│   ├── momentum_qualifier.py        # モメンタム判定（重複防止・フェーズ3結果記録対応済み）
│   ├── investment_advisor.py        # フェーズ4: 投資判断エージェント
│   ├── us_market_scanner.py         # 米国セクターETF17本モメンタム
│   ├── us_theme_extractor.py        # 米財務メディアからキーワード抽出
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理
│   ├── slack_notifier.py            # Slack通知
│   └── utils.py                     # 共通ユーティリティ（get_anthropic_key等）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   ├── raw/edinet_pdfs/             # 決算短信PDFキャッシュ
│   ├── processed/scans/             # スキャン結果JSON（GitHub Actionsでキャッシュ済み）
│   ├── processed/us_scans/          # 米ETFスキャン結果JSON
│   ├── processed/us_themes/         # 米テーマ抽出結果JSON
│   └── processed/edinet_analysis_cache.json  # EDINET分析結果キャッシュ（7日間・GitHub Actionsで永続化）
└── memory/
    ├── trade_log.json               # 取引履歴（GitHub Actionsでキャッシュ済み）
    ├── disclosure_log.json          # 開示情報履歴
    └── qualify_log.json             # モメンタム判定ログ（GitHub Actionsでキャッシュ済み）
```

---

## GitHub Secrets
| キー名 | 内容 |
|---|---|
| `JQUANTS_API_KEY` | J-Quants APIキー（登録済み） |
| `EDINET_API_KEY` | EDINET APIキー（登録済み） |
| `SLACK_WEBHOOK_URL` | Slack Webhook URL（登録済み） |
| `ANTHROPIC_API_KEY` | 登録済み・動作確認済み（2026/03/16更新） |

---

## GitHub Actions スケジュール（3ジョブ構成）

| 時刻（JST） | ジョブ名 | cron（UTC） | 内容 |
|---|---|---|---|
| 朝 6:30 | us-market-scan | `30 21 * * 0-5` | 米市場スキャン専用（ETFモメンタム＋テーマ抽出）。米国市場終了直後に単独実行。 |
| 朝 8:00 | morning-scan | `0 23 * * 0-5` | 日本株スキャン＋保有状況通知。米市場結果反映済みの状態で実行。 |
| 夕方 18:00 | evening-scan | `0 9 * * 1-5` | メイン通知。シグナル＋保有状況（米市場スキャンなし・朝の結果を参照）。 |

**キャッシュ対象（朝2・夕方ジョブ）：**
- `memory/trade_log.json`
- `memory/qualify_log.json`
- `data/processed/scans/`
- `data/processed/edinet_analysis_cache.json` ★追加

---

## Claude API呼び出し箇所と設定（全箇所・web_searchなし）

| ファイル | 用途 | モデル | max_tokens | 備考 |
|---|---|---|---|---|
| `edinet_analyzer.py` | 決算PDF分析・スコアリング | Haiku | 1500 | pdfplumber抽出テキストのみ渡す・結果キャッシュあり |
| `us_theme_extractor.py` | RSSキーワード抽出 | Haiku | 1500 | ヘッドライン40件のみ |
| `us_market_scanner.py` | ETFセクター分析 | Haiku | 1200 | ETF数値データのみ渡す |
| `momentum_qualifier.py` | 構造的変化判定 | Haiku | 800 | ステージ1通過銘柄を一括1回呼び出し |

**全箇所でweb_searchツールを削除済み（2026/03/17）。コスト削減を優先。**

---

## トークン節約施策（2026/03/17実施）

| 施策 | 対象 | 効果 |
|---|---|---|
| web_search削除（全箇所） | 全4ファイル | ▲50〜80%/回 |
| PDF base64渡し→pdfplumberテキスト抽出（先頭15P・6000文字） | edinet_analyzer | ▲80〜90%/銘柄 |
| RSSヘッドライン80→40件 | us_theme_extractor | ▲50% input |
| EDINET分析結果キャッシュ（7日間・docID単位） | edinet_analyzer | 朝夕重複ゼロ |
| max_tokens削減（4000→1500、2000→800〜1200） | 全4ファイル | output上限削減 |

---

## スクリーニングモード
### 急騰モード（SHORT_TERM）
- 前日比+3%以上
- 出来高が25日平均の2倍以上

### モメンタムモード（MOMENTUM）
- 5日・25日・75日MA全て上昇中
- 現在株価が52週高値の90%以上
- RSI(14日)が55〜72の範囲

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出（銘柄数上限なし・全件カバー）
- Claude APIで決算PDFを読解してポジ/ネガスコアリング

---

## モメンタム判定モジュール（momentum_qualifier.py）

### ステージ1: 出来高継続パターン判定
- 急騰後3日間の出来高が急騰日の50%以上を維持しているか
- 急騰後の株価が急騰終値の97%以上を維持しているか

### ステージ2: Claude APIによる構造的変化判定（バッチ処理・web_searchなし）
- ステージ1通過銘柄を全てまとめて1回のAPI呼び出しで判定
- 学習済み知識のみで判断（銘柄名・事業内容から推定）

### 判定結果
| 結果 | 条件 |
|---|---|
| STRONG | ステージ1✅ + Claude判定✅ |
| WATCH | ステージ1✅ + APIキーなし |
| WEAK | ステージ1✅ + Claude判定❌ |
| NOISE | ステージ1❌ |

### フェーズ3: 10営業日後の結果自動記録
- `qualify_signals()`実行のたびに`record_outcomes()`が自動呼び出し
- 記録が5件以上溜まるとSlack通知に精度サマリーが追加表示

---

## フェーズ4: 投資判断エージェント（investment_advisor.py）

qualify結果・PF・ポートフォリオ余力・米市場シグナルを統合して推奨を出す。

| 推奨 | 条件 |
|---|---|
| ENTRY（エントリー推奨） | qualifyResult==STRONG かつ PF≥1.2 かつ ポートフォリオ余力あり |
| WATCH（様子見） | STRONG だが PF不足 or 余力なし |
| SKIP（見送り） | WEAK / NOISE |

---

## ペーパートレード設定
- 仮想資金：300万円 / 1銘柄最大50万円 / 最大同時保有10銘柄

## バックテスト設定
- エントリー：翌営業日始値 / 損切-5% / 利確+15% / 最大10営業日保有

---

## フェーズ進捗

### ✅ フェーズ1〜4（完了）
### ✅ トークン節約・スケジュール最適化（2026/03/17完了）
- 全Claude API呼び出しからweb_search削除
- PDF base64渡し廃止・pdfplumber事前抽出に変更
- EDINET分析結果キャッシュ実装（7日間）
- 米市場スキャンを独立ジョブ化（JST6:30・米国市場終了直後）
- EDINETキャッシュをGitHub Actions永続化

### 🔲 フェーズ3継続（データ蓄積後）
- qualify_log outcome が5件以上溜まったら精度レポートで検証

### 🔲 フェーズ5（今後）
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 実際のエントリー判断の精度を検証・改善

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・推奨）
python main.py --mode fetch --days 150

# 日本株スキャン
python main.py --mode scan

# 判定精度レポート確認
python main.py --mode qualify_report

# 米市場スキャン
python main.py --mode us_scan

# ポートフォリオ確認
python main.py --mode status

# GitHubに反映
git add .
git commit -m "変更内容のメモ"
git push
```

---

## 注意事項
- `config.yaml` / `.env` はGitに含めない（.gitignoreで除外済み）← **過去に誤コミットしてGitHubにブロックされた経験あり**
- APIキーは絶対にコードに直書きしない
- **ファイル編集はWindows-MCP:FileSystemツールで直接行う（PowerShellはgitでハングするため使わない）**
- **GitHub上での直接編集は避ける**
- **Consoleを開くときは必ずブラウザの翻訳をオフに！**（翻訳がHTMLを壊してエラーになる）
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**

---

## 現在の状況（2026/03/17深夜時点）
- 3ジョブ構成（朝6:30米市場・朝8:00日本株・夕方18:00メイン）で稼働中
- 全Claude API呼び出しのweb_search削除・トークン大幅節約済み
- EDINETキャッシュのGitHub Actions永続化済み
- qualify_logは8件蓄積中。3/25以降にoutcome自動記録が始まる予定
- 次のマイルストーン: outcome 5件以上溜まったら精度レポートで検証
