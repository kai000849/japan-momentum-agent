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
- ステージ1: 数値検証（出来高・株価継続性）→ ステージ2: Claude API判定（開示内容最重視）
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
- **3軸パターン学習**: 情報源タグ別・出来高維持率別・Stage2確信度別の勝率を蓄積
- **週次可視化でドリフト防止**: Slackの週次レポートで「エージェントが賢くなっているか」を人間が確認
- **コストは精度向上にだけ**: qualify・edinet分析はSonnet（高精度）、頻度の高い情報収集はHaiku（低コスト）

### 手動アップデートの運用フロー（2026/03/21確定）
```
① 知見を発見（論文・記事・自分の気づき）
② Claude.aiに共有 →「うちのエージェントに使えるか？」
③ Claudeが反映先を振り分け（CLAUDE.md / コード / プロンプト）
④ japan-momentum-agentに反映 → git push
⑤ 学習メモに「日付: 何を → どこに反映」を1行追記
⑥ 1〜2週間運用して効果確認（週次レポートをClaude.aiに共有して分析）
⑦ 効果あり→✅追記 / 効かなかった→❌ロールバック＋理由追記
```
**変更は一度に一つずつ。** 複数同時に変えると効果が分離できない。

---

## プロジェクト構成

- **GitHubリポジトリ**: https://github.com/kai000849/japan-momentum-agent
- **ローカルパス**: `C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent`
- **Slack通知チャンネル**: #all-japan-momentum-agent
- **自動実行**: GitHub Actions（5ジョブ構成）

### 技術スタック
Python / J-Quants API / EDINET API / GitHub Actions / Slack Webhook / Claude API（Sonnet+Haiku） / yfinance / pdfplumber

### ファイル構成（主要ファイルのみ）
```
japan-momentum-agent/
├── main.py                          # エントリーポイント
├── .github/workflows/daily_report.yml
├── agents/
│   ├── scanner.py                   # スクリーニング（SHORT_TERM/MOMENTUM/EARNINGS）
│   ├── jquants_fetcher.py           # 株価データ取得
│   ├── edinet_fetcher.py / edinet_analyzer.py  # 決算取得・分析
│   ├── momentum_qualifier.py        # モメンタム判定（qualify_log管理・ラベル移行・自動クリーンアップ）
│   ├── investment_advisor.py        # 投資判断エージェント
│   ├── noon_scanner.py              # 正午スキャン（後場エントリー判断）
│   ├── earnings_momentum_scanner.py # 決算中長期フォローアップ
│   ├── us_market_scanner.py / us_theme_extractor.py  # 米市場スキャン
│   ├── paper_trader.py              # ペーパートレード＋実売買記録
│   ├── slack_notifier.py            # Slack通知
│   └── utils.py                     # 共通ユーティリティ
├── data/                            # CSV・スキャン結果・キャッシュ（日付キーで管理）
└── memory/                          # trade_log / qualify_log / watchlist / followup_list
```

---

## GitHub Actions スケジュール（5ジョブ構成）

| 時刻（JST） | ジョブ名 | 内容 |
|---|---|---|
| 6:30 | us-market-scan | 米市場スキャン（ETFモメンタム＋テーマ抽出） |
| 6:40〜 | morning-scan | us-market-scan完了後に自動起動。米結果反映済みの朝通知 |
| 10:30 | midmorning-scan | ザラ場決算スキャン |
| 12:15 | noon-scan | 後場エントリー判断（yfinance前場データ） |
| 18:00 | evening-scan | メイン通知＋保有状況＋決算評価＋実売買損益 |
| 金曜18:00 | weekly-report | 週次パフォーマンスレポート |

GitHub Secrets: `JQUANTS_API_KEY` / `EDINET_API_KEY` / `SLACK_WEBHOOK_URL` / `ANTHROPIC_API_KEY`（全登録済み）

---

## Claude API設定

| ファイル | 用途 | モデル | 備考 |
|---|---|---|---|
| edinet_analyzer.py | 決算PDF分析 | Sonnet | pdfplumber抽出・結果キャッシュあり |
| momentum_qualifier.py | 構造的変化判定 | Sonnet | 上位15件バッチ・過去精度フィードバック付 |
| us_theme_extractor.py | RSSキーワード抽出 | Haiku | ヘッドライン40件 |
| us_market_scanner.py | ETFセクター分析 | Haiku | 数値データのみ |

全箇所web_searchツール削除済み。

---

## 主要な設計判断（条件詳細はコード参照）

- **スクリーニング条件**: scanner.py参照（SHORT_TERM=急騰+出来高、MOMENTUM=パーフェクトオーダー+52週高値、EARNINGS=EDINET+Claudeスコア）
- **qualify判定**: momentum_qualifier.py参照（ステージ1=数値、ステージ2=Claude API）。結果ラベルは日本語（継続/様子見/一時的/ノイズ）
- **投資判断**: investment_advisor.py参照（qualify結果+PF+余力+米セクターを統合）。PF閾値はSHORT_TERM/EARNINGS=1.2、MOMENTUM=1.5
- **正午スキャン**: noon_scanner.py参照（前場データで後場GO/様子見/見送りを判定）
- **ペーパートレード**: 仮想資金300万円 / 1銘柄最大50万円 / 最大10銘柄 / 損切-5% / 利確+15%
- **実売買**: tradeType:"actual"でペーパーと区別。余力判定はペーパーのみカウント

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

python main.py --mode fetch           # データ取得（150日分）
python main.py --mode scan            # 日本株スキャン
python main.py --mode noon_scan       # 正午スキャン
python main.py --mode qualify_report  # 判定精度レポート
python main.py --mode us_scan         # 米市場スキャン
python main.py --mode status          # ポートフォリオ確認
python main.py --mode add_trade --code 7203 --price 2500 --shares 100  # 実売買記録
python main.py --mode close_trade --code 7203 --price 2650             # 実売買決済

git add . && git commit -m "メモ" && git push
```

---

## 注意事項
- APIキーは絶対にコードに直書きしない（過去に誤コミットでGitHubブロック経験あり）
- ファイル編集はWindows-MCP:FileSystemツールで直接行う（PowerShellはgitでハングする）
- GitHub上での直接編集は避ける
- Consoleを開くときはブラウザの翻訳をオフにする
- CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ
- **CLAUDE.mdは肥大化させない**: 設計方針と学習メモだけ記載。条件の数値詳細はコードに書く。反映済みの作業ログは削除する

---

## フェーズ進捗
- ✅ フェーズ1〜4完了（スキャン・qualify・投資判断・正午スキャン・実売買記録・トークン最適化）
- ✅ AIエージェント育成ループ構築完了（多軸パターン分析・週次レポート・Stage2フィードバック・Sonnet移行）
- 🔲 フェーズ3継続: qualify_logデータ蓄積中。5件蓄積後にフィードバックループ自動発動を確認
- 🔲 フェーズ5: かいさんの投資判断ロジックをプロンプトに組み込み・精度検証

---

## 学習メモ（知見の反映ログ）
知見を反映したら「日付: 知見の要点 → 反映先と内容」を1行で記録する。
効果検証後に ✅（効果あり）/ ❌（ロールバック＋理由）を追記。

- 2026/03/21: 様子見ラベルの「APIキー未設定」表記が誤解を招くと判明 → `momentum_qualifier.py:1194` を「【様子見】要観察」に修正。実態はClaudeが不確実と判定した結果であり、APIキー自体は正常。

---

## 現在の状況（2026/03/21時点）
- 5ジョブ＋週次レポートで安定稼働中
- CLAUDE.mdスリム化完了（20.7KB→9.6KB）＋肥大化防止方針確定
- 手動アップデート運用フロー確定（知見発見→Claude.ai整理→反映→効果検証）
- qualify_logはデータ蓄積中。outcome記録が始まったら精度レポートで検証
- 次のマイルストーン①: 週次レポート初回確認（3/20金曜）
- 次のマイルストーン②: qualify_log 5件蓄積→フィードバックループ自動発動確認
