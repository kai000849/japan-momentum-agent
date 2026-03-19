# Japan Momentum Agent - プロジェクト引き継ぎメモ

## このファイルの使い方
新しいチャットセッションを開始したら、このファイルをそのままClaudeに貼り付けてください。すぐに作業に入れます。

---

## オーナーの投資哲学（全機能設計の基礎）

**この哲学はコードの設計判断・Claude APIプロンプト・通知フォーマットすべての基準になる。修正時は必ずここに立ち返ること。**

### 3シグナルの役割分担

| シグナル | 目的 | 重視するもの |
|---|---|---|
| **急騰シグナル（SHORT_TERM）** | 評価が変化する瞬間の**初動を捕捉**する | スピード最優先。材料が出た直後の最初の動き |
| **モメンタムシグナル（MOMENTUM）** | 中長期的な上昇トレンドを**できる限り早く・正確に**把握する | 早さと正確さは反比例するが、学習で両立を目指す |
| **決算シグナル（EARNINGS）** | 決算・開示を起点とした**評価の変化や中長期モメンタムの始まり**を補足する | 業績の質・継続性・モメンタムへの移行可能性 |

### 構造的モメンタム判定（qualify）の位置づけ
急騰・決算シグナルから「**中長期的な上昇トレンドの初動かどうか**」を見極めるフィルター。
- ステージ1: 急騰後の出来高・株価継続性を数値で検証
- ステージ2: TDnet開示内容＋会社名からClaudeが判定（**開示内容を最重視**）
- 学習ループ: 判定結果と10営業日後の実績を照合して精度向上

### 設計思想
- **早さと正確さの両立はAI学習で実現する**。今は精度が不十分でも、データを蓄積して継続改善する
- Claude APIコストは**必要な精度向上にだけ**使う。無駄なトークンは使わない
- 全シグナルは最終的に「今エントリーすべきか」という1点に集約される

---

## AIエージェント育成の原則（最重要）

**このプロジェクトの目標は「動く自動化ツール」ではなく「成長するAIエージェント」を作ることにある。**

### 育成の3ループ

| ループ | 仕組み | 現状 |
|---|---|---|
| **短期ループ（毎日）** | qualify_log にシグナル・判定結果を記録。10営業日後にoutcomeを自動追記 | 蓄積中 |
| **中期ループ（週次）** | 週次レポートで7日間の勝率・平均リターン・パターンを可視化 | 金曜自動通知（2026/03/19〜） |
| **長期ループ（データ蓄積後）** | `get_outcome_patterns()` の多軸分析をStage2プロンプトにフィードバック。判定精度を自己改善 | 5件蓄積後に自動発動 |

### 育成の設計思想
- **記録→分析→フィードバックの一貫性**: 判定を記録するだけでなく、実績パターンを次回の判定に還元する
- **3軸パターン学習**: 情報源タグ別・出来高維持率別・Stage2確信度別の勝率を蓄積し、何が「本物のモメンタム」かを学習
- **週次可視化でドリフト防止**: Slackの週次レポートで「エージェントが賢くなっているか」を人間が確認できる仕組み
- **コストは精度向上にだけ**: qualify・edinet分析はSonnet（高精度）、頻度の高い情報収集はHaiku（低コスト）と使い分け

---

## プロジェクト概要
日本株モメンタム投資の自動化AIエージェント。毎朝6:30米市場スキャン→朝スキャン（米結果反映済み）・夕方18:00メイン通知の構成でSlackに通知する。

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
- yfinance（米国ETFデータ・日本株イントラデイ価格取得・無料）
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
│   ├── momentum_qualifier.py        # モメンタム判定（qualify_log自動クリーンアップ・ラベル移行対応）
│   ├── investment_advisor.py        # フェーズ4: 投資判断エージェント（yfinance最新価格・セクターマッチ）
│   ├── noon_scanner.py              # 正午スキャン（yfinance前場データ→後場エントリー判断）
│   ├── earnings_momentum_scanner.py # 決算モメンタムスキャン（中長期フォローアップ対応）
│   ├── us_market_scanner.py         # 米国セクターETF17本モメンタム
│   ├── us_theme_extractor.py        # 米財務メディアからキーワード抽出
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理 + 実売買記録（tradeType:"actual"）
│   ├── slack_notifier.py            # Slack通知
│   └── utils.py                     # 共通ユーティリティ（get_anthropic_key等）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   ├── raw/edinet_pdfs/             # 決算短信PDFキャッシュ
│   ├── processed/scans/             # スキャン結果JSON（日付キーでキャッシュ・noon-scan参照）
│   ├── processed/us_scans/          # 米ETFスキャン結果JSON（日付キーでキャッシュ・morning-scan参照）
│   ├── processed/us_themes/         # 米テーマ抽出結果JSON（日付キーでキャッシュ・morning-scan参照）
│   └── processed/edinet_analysis_cache.json  # EDINET分析結果キャッシュ（7日間・GitHub Actionsで永続化）
└── memory/
    ├── trade_log.json               # ペーパー＋実売買履歴（GitHub Actionsでキャッシュ済み）
    ├── qualify_log.json             # モメンタム判定ログ（90日超outcomeあり分を自動クリーンアップ）
    ├── earnings_watchlist.json      # 当日決算ウォッチリスト（日付キー・midmorning-scan参照）
    ├── earnings_followup_list.json  # 決算銘柄中長期追跡リスト（20営業日）
    └── disclosure_log.json          # 開示情報履歴
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

## GitHub Actions スケジュール（5ジョブ構成）

| 時刻（JST） | ジョブ名 | トリガー | 内容 |
|---|---|---|---|
| 朝 6:30 | us-market-scan | `30 21 * * 0-5` | 米市場スキャン専用（ETFモメンタム＋テーマ抽出）。結果を日付キーでキャッシュ保存。 |
| 朝 6:40〜 | morning-scan | `needs: us-market-scan` | **us-market-scan完了後に自動起動**。当日の米セクター結果を朝の投資判断に反映済みで通知。 |
| 前場 10:30 | midmorning-scan | `30 1 * * 1-5` | ザラ場決算スキャン。morning-scanが保存した earnings_watchlist（日付キー）を参照。 |
| 正午 12:15 | noon-scan | `15 3 * * 1-5` | 後場エントリー判断（yfinance前場データ）。scans（日付キー）と qualify_log を参照。 |
| 夕方 18:00 | evening-scan | `0 9 * * 1-5` | メイン通知。シグナル＋保有状況＋決算引け後評価＋実売買ポジション損益。 |

**クロスジョブキャッシュ（日付キー方式で同日内の確実な受け渡しを保証）：**
- `data/processed/us_scans/` : us-market-scan → morning-scan
- `data/processed/us_themes/` : us-market-scan → morning-scan
- `memory/earnings_watchlist.json` : morning-scan → midmorning-scan
- `data/processed/scans/` : morning-scan → noon-scan

**通常キャッシュ（run_number + restore-keys方式）：**
- `memory/trade_log.json`
- `memory/qualify_log.json`
- `data/processed/edinet_analysis_cache.json`

---

## Claude API呼び出し箇所と設定（全箇所・web_searchなし）

| ファイル | 用途 | モデル | max_tokens | 備考 |
|---|---|---|---|---|
| `edinet_analyzer.py` | 決算PDF分析・スコアリング | **Sonnet** | 1500 | pdfplumber抽出テキストのみ渡す・結果キャッシュあり |
| `us_theme_extractor.py` | RSSキーワード抽出 | Haiku | **3000** | ヘッドライン40件のみ |
| `us_market_scanner.py` | ETFセクター分析 | Haiku | 1200 | ETF数値データのみ渡す |
| `momentum_qualifier.py` | 構造的変化判定 | **Sonnet** | 1500 | ステージ1通過銘柄を上位15件まとめて1回・過去精度フィードバック付 |

**全箇所でweb_searchツールを削除済み（2026/03/17）。コスト削減を優先。**
**精度重視タスク（qualify・edinet）はSonnet、頻度重視タスク（US系）はHaiku（2026/03/19〜）。**

---

## トークン節約施策（2026/03/17実施）

| 施策 | 対象 | 効果 |
|---|---|---|
| web_search削除（全箇所） | 全4ファイル | ▲50〜80%/回 |
| PDF base64渡し→pdfplumberテキスト抽出（先頭15P） | edinet_analyzer | ▲80〜90%/銘柄 |
| RSSヘッドライン80→40件 | us_theme_extractor | ▲50% input |
| EDINET分析結果キャッシュ（7日間・docID単位） | edinet_analyzer | 朝夕重複ゼロ |
| max_tokens削減（4000→1500、2000→800〜1200） | 全4ファイル | output上限削減 |

---

## スクリーニングモードと役割分担

| モード | 役割 | PF閾値 | エントリータイミング |
|---|---|---|---|
| SHORT_TERM | **早期エントリー候補**。急騰初日を捕捉 | PF≥1.2 | 翌朝始値（または後場12:30） |
| MOMENTUM | **保有継続確認**。3ヶ月超のトレンド確認型。新規エントリーは遅れ気味 | PF≥1.5 | 慎重に判断（デフォルト様子見） |
| EARNINGS | **業績裏付けあり急騰**。決算発表当日の業績確認型 | PF≥1.2 | 翌朝始値 |

### 急騰モード（SHORT_TERM）
- 前日比+3%以上 / 出来高が25日平均の**1.5倍以上**

### モメンタムモード（MOMENTUM）
- 5日・25日・75日MA全て上昇中 かつ MA5 > MA25 > MA75（パーフェクトオーダー）/ 52週高値の**95%以上** / RSI(14日) 55〜72 / 直近5日出来高平均 >= 直近25日出来高平均

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を全件抽出
- Claude APIで決算PDFを読解してポジ/ネガスコアリング（スコア30以上が対象）

---

## モメンタム判定モジュール（momentum_qualifier.py）

### ステージ1: 出来高継続パターン判定
- 急騰後3日間の出来高が急騰日の50%以上を維持しているか
- 急騰後の株価が急騰終値の97%以上を維持しているか

### ステージ2: Claude APIによる構造的変化判定（バッチ処理・web_searchなし）
- ステージ1通過銘柄を全てまとめて1回のAPI呼び出しで判定

### 判定結果（日本語ラベル）
| 結果 | 条件 |
|---|---|
| **継続** | ステージ1✅ + Claude判定✅ |
| **様子見** | ステージ1✅ + APIキーなし |
| **一時的** | ステージ1✅ + Claude判定❌ |
| **ノイズ** | ステージ1❌ |

※旧英語ラベル（STRONG/WATCH/WEAK/NOISE）はqualify_signals()実行時に自動で日本語に変換される。

### フェーズ3: 10営業日後の結果自動記録
- `qualify_signals()`実行のたびに`record_outcomes()`が自動呼び出し
- 記録が5件以上溜まるとSlack通知に精度サマリーが追加表示

### qualify_log 自動メンテナンス
qualify_signals()実行時に以下が自動実行される：
- **ラベル移行**: 旧英語ラベルを日本語に一括変換
- **自動クリーンアップ**: scanDateから90日超 かつ outcome記録済みのエントリを削除（outcome未記録は期間に関わらず保持）

---

## フェーズ4: 投資判断エージェント（investment_advisor.py）

qualify結果・PF・ポートフォリオ余力・米市場シグナルを統合して推奨を出す。

| 推奨 | SHORT_TERM / EARNINGS 条件 | MOMENTUM 条件 |
|---|---|---|
| エントリー推奨 | 継続 かつ PF≥1.2 かつ 余力あり | 継続 かつ PF≥1.5 かつ 余力あり |
| 様子見 | 継続 だが PF不足 or 余力なし | 継続 だが PF<1.5 or 余力なし（デフォルト） |
| 見送り | 一時的 / ノイズ | 一時的 / ノイズ |

- **エントリー価格**: yfinanceで最新価格を取得（失敗時はJ-Quants終値にフォールバック）
- **余力判定**: ペーパートレードポジションのみカウント（実売買は除外・独立した戦略判断）
- **米セクター一致**: morning-scanでus_scansがキャッシュ済みのため朝から「✅ 関連セクター上昇中」が表示される

---

## 正午スキャン（noon_scanner.py）

JST 12:15に実行。朝スキャンで検出したシグナル銘柄をyfinanceの前場データで確認し、後場エントリー可否を判定。

| 判定 | 条件 |
|---|---|
| **後場GO** | 急騰終値の97%以上を維持 かつ 前場終値 > 前場始値 |
| **様子見** | 値は維持しているが上昇トレンドが弱い |
| **見送り** | 急騰終値の97%を割り込んだ（失速） |

損切-5% / 利確+15%（investment_advisorと統一）

---

## 実売買記録（paper_trader.py）

`tradeType: "actual"` フィールドでペーパートレードと区別して同一 trade_log.json に保存。

```bash
# エントリー記録
python main.py --mode add_trade --code 7203 --price 2500 --shares 100 --company トヨタ

# 決済記録
python main.py --mode close_trade --code 7203 --price 2650

# 確認（yfinanceで最新価格取得・損益表示）
python main.py --mode status
```

夕方スキャン（`--mode scan`）実行後、実売買ポジションがあれば自動でSlack通知。

---

## ペーパートレード設定
- 仮想資金：300万円 / 1銘柄最大50万円 / 最大同時保有10銘柄

## バックテスト設定
- エントリー：翌営業日始値 / 損切-5% / 利確+15% / 最大10営業日保有

---

## フェーズ進捗

### ✅ フェーズ1〜4（完了）
### ✅ トークン節約・スケジュール最適化（2026/03/17完了）
### ✅ PF=0バグ 恒久対策（2026/03/17完了）
### ✅ 大規模改修（2026/03/18完了）
- **判定ラベル日本語化**: STRONG→継続 / WATCH→様子見 / WEAK→一時的 / NOISE→ノイズ / ENTRY→エントリー / GO→後場GO
- **noon_scanner.py 新規追加**: yfinance前場データで後場エントリー判断（JST 12:15）
- **決算中長期フォローアップ**: earnings_followup_list で20営業日追跡・Slack通知
- **シグナル役割分担明確化**: SHORT_TERM=早期・MOMENTUM=長期確認（PF閾値1.5）
- **morning-scanをus-market-scan完了後に実行**: 米セクター結果を朝通知に反映
- **クロスジョブキャッシュ安定化**: 日付ベースキーで同日内の確実な受け渡しを保証
- **投資判断価格をyfinance最新価格に変更**: 翌日推定×1.005を廃止
- **実売買記録機能追加**: add_trade / close_trade コマンド・Slack損益通知
- **qualify_log自動クリーンアップ**: 90日超outcomeあり分を自動削除
- **利確ライン統一**: noon/advisor 共に+15%
- **余力判定をペーパーのみに限定**: 実売買記録が戦略判断に影響しない設計

### ✅ バグ修正・品質改善（2026/03/18 セッション2完了）
- **Slack通知キー名不一致修正**: priceChangePct/volumeRatio（camelCase）をslack_notifier・momentum_qualifierで統一
- **Stage2バッチ上限追加**: 上位15件に絞り込み・max_tokens 800→1500（JSON切れ防止）
- **qualify_log上書き防止**: 朝の「継続」「一時的」を夕方の「様子見」で上書きしないよう保護
- **qualifyラベル表示統一**: slack_notifier・investment_advisorの表示をnormalize_qualify_label経由に統一（旧英語ラベル残存対策）
- **noon_scannerモード別閾値**: SHORT_TERM=97% / MOMENTUM=95% / EARNINGS=97%
- **MOメンタムシグナル条件厳格化**: MAパーフェクトオーダー（MA5>MA25>MA75）追加・52週高値比率90%→95%・出来高フィルター追加（件数・PF改善目的）

### ✅ AIエージェント育成強化（2026/03/19完了）
- **Haiku→Sonnet移行**: momentum_qualifier + edinet_analyzer（精度重視）
- **多軸パターン分析追加**: `get_outcome_patterns()`で情報源別・出来高維持率別・確信度別の勝率集計
- **Stage2フィードバックループ**: 過去判定精度をプロンプトに自動付与（5件蓄積後）
- **週次パフォーマンスレポート**: 毎週金曜18:00にSlack通知（7日統計・outcome・ペーパー損益・考察）
- **Slack通知バグ3件修正**: JSONパースエラー・ペーパー未追加・二重表記

### 🔲 フェーズ3継続（データ蓄積後）
- qualify_logはデータ蓄積中。outcome自動記録が始まったら精度レポートで検証

### 🔲 フェーズ5（今後）
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 実際のエントリー判断の精度を検証・改善

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・デフォルト・推奨）
python main.py --mode fetch

# 日本株スキャン（米セクター結果があれば反映）
python main.py --mode scan

# 正午スキャン（後場エントリー判断）
python main.py --mode noon_scan

# 判定精度レポート確認
python main.py --mode qualify_report

# 米市場スキャン
python main.py --mode us_scan

# ポートフォリオ確認（実売買＋ペーパー）
python main.py --mode status

# 実売買エントリー記録
python main.py --mode add_trade --code 7203 --price 2500 --shares 100

# 実売買決済記録
python main.py --mode close_trade --code 7203 --price 2650

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

## 現在の状況（2026/03/19時点）
- 5ジョブ構成（us-market-scan→morning-scan直列・前場10:30・正午12:15・夕方18:00・金曜週次）で稼働中
- AIエージェント育成ループ構築完了（多軸パターン分析・週次レポート・Stage2フィードバック）
- qualify・edinet分析をSonnetに移行（精度向上）、US系はHaiku継続（コスト最適）
- qualify_logはデータ蓄積中。outcome記録が始まったら精度レポートで検証
- 次のマイルストーン①: 週次レポートの初回確認（最初の金曜）
- 次のマイルストーン②: qualify_log 5件蓄積→フィードバックループ自動発動確認
