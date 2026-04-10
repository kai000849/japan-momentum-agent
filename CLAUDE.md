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
| **中期ループ（週次）** | 週次レポートで7日間の勝率・平均リターン・パターン＋戦略有効性モニターを可視化 | 金曜自動通知（2026/03/19〜） |
| **長期ループ（データ蓄積後）** | `get_outcome_patterns()` の多軸分析をStage2プロンプトにフィードバック。判定精度を自己改善 | 5件蓄積後に自動発動 |

### 育成の設計思想
- **記録→分析→フィードバックの一貫性**: 判定を記録するだけでなく、実績パターンを次回の判定に還元する
- **3軸パターン学習（SHORT_TERM）**: 情報源タグ別・出来高維持率別・Stage2確信度別の勝率を蓄積
- **3軸パターン学習（MOMENTUM）**: MAギャップ別・52週高値比別・出来高トレンド別の勝率を蓄積（momentum_log.json）
- **週次可視化でドリフト防止**: Slackの週次レポートで「エージェントが賢くなっているか」を人間が確認
- **戦略有効性モニター**: 4週推移でモメンタム戦略自体の有効性を監視（シグナル頻度・勝率・リターン推移＋自動アラート）
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
│   ├── jquants_fetcher.py           # 株価データ取得・決算速報取得（get_todays_earnings）
│   ├── jquants_earnings_analyzer.py # J-Quants決算速報Haiku分析（YoY・進捗率・正サプライズ判定）
│   ├── edinet_fetcher.py / edinet_analyzer.py  # 決算取得・分析（翌朝照合用・遅延あり）
│   ├── momentum_qualifier.py        # モメンタム判定（qualify_log管理・ラベル移行・自動クリーンアップ）
│   ├── momentum_log_manager.py      # MOメンタム学習ループ（momentum_log.json管理・20日アウトカム記録）
│   ├── investment_advisor.py        # 投資判断エージェント
│   ├── noon_scanner.py              # 正午スキャン（後場エントリー判断）
│   ├── earnings_momentum_scanner.py # 決算中長期フォローアップ
│   ├── us_market_scanner.py / us_theme_extractor.py  # 米市場スキャン
│   ├── paper_trader.py              # 実売買記録専用（ペーパー機能は無効化済み）
│   ├── slack_notifier.py            # Slack通知
│   └── utils.py                     # 共通ユーティリティ
├── data/                            # CSV・スキャン結果・キャッシュ（日付キーで管理）
└── memory/                          # trade_log / qualify_log / momentum_log / watchlist / followup_list
```

---

## GitHub Actions スケジュール（5ジョブ構成）

| 時刻（JST） | ジョブ名 | 内容 |
|---|---|---|
| 6:30 | us-market-scan | 米市場スキャン（ETFモメンタム＋テーマ抽出） |
| 6:40〜 | morning-scan | us-market-scan完了後に自動起動。米結果反映済みの朝通知 |
| 10:30 | midmorning-scan | ザラ場決算スキャン |
| 12:15 | noon-scan | 後場エントリー判断（yfinance前場データ） |
| 18:30 | evening-scan | メイン通知＋J-Quants決算速報＋保有状況＋実売買損益（18:00速報を30分待って取り込む） |
| 金曜18:30 | weekly-report | 週次パフォーマンスレポート |

GitHub Secrets: `JQUANTS_API_KEY` / `EDINET_API_KEY` / `SLACK_WEBHOOK_URL` / `ANTHROPIC_API_KEY`（全登録済み）

---

## Claude API設定

| ファイル | 用途 | モデル | 備考 |
|---|---|---|---|
| edinet_analyzer.py | 決算PDF分析 | Sonnet | pdfplumber抽出・結果キャッシュあり |
| momentum_qualifier.py | 構造的変化判定 | Sonnet | 上位15件バッチ・過去精度フィードバック付 |
| jquants_earnings_analyzer.py | 決算速報正サプライズ判定 | Haiku | 数値データ（YoY・進捗率）で定量フィルタ後に判定 |
| us_theme_extractor.py | RSSキーワード抽出 | Haiku | ヘッドライン40件 |
| us_market_scanner.py | ETFセクター分析 | Haiku | 数値データのみ |

全箇所web_searchツール削除済み。

---

## 主要な設計判断（条件詳細はコード参照）

- **スクリーニング条件**: scanner.py参照（SHORT_TERM=急騰+出来高、MOMENTUM=パーフェクトオーダー+52週高値、EARNINGS=EDINET+Claudeスコア）
- **qualify判定**: momentum_qualifier.py参照（ステージ1=数値、ステージ2=Claude API）。結果ラベルは日本語（継続/様子見/一時的/ノイズ）。「様子見」=Stage2未実行のみ（翌日再判定対象）
- **MOメンタム学習**: momentum_log_manager.py参照。MOシグナルを毎スキャン記録し、20営業日後にリターン/最大DD/トレンド継続を自動記録。アウトカム期間が10日（SHORT_TERM）と異なるため別ファイル管理
- **投資判断**: investment_advisor.py参照（qualify結果+PF+余力+米セクターを統合）。PF閾値はSHORT_TERM/EARNINGS=1.2、MOMENTUM=1.5
- **正午スキャン**: noon_scanner.py参照（前場データで後場GO/様子見/見送りを判定）
- **ペーパートレード**: 無効（学習ループには不要と判断・2026/04/05無効化）。`paper_trader.py`は実売買記録専用として残存
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
- 2026/03/21: 週次レポートに戦略有効性モニター追加 → `slack_notifier.py` に4週推移（シグナル頻度・勝率・リターン）＋自動アラート（3週連続ゼロ/勝率低下/リターン悪化）を実装。
- 2026/03/21: 論文「LLMフィードバック設計」（SIG-FIN-036-31）→ Stage2への情報追加は1軸ずつ効果検証する。情報量≠精度。Sonnet継続が最適。フィードバックに「何を見せるか」で判定の方向性が変わる。
- 2026/03/22: Lee & Swaminathan(2000)の出来高パターン研究 → `qualify_log`に`volume_pattern`フィールド追加（late=バズ型/early=ジワジワ型）。Slack通知にも絵文字タグ表示。判定基準: シグナル日出来高≥20日MA×2倍 かつ 直近5日平均≥1.5倍でlate。利確ルール変更はデータ蓄積後に別途実施。
- 2026/03/31: 米市場テーマ通知のキーワードが英語・日本語混在していた → `us_theme_extractor.py`プロンプトの例示を日本語に統一し「日本語で出力」の明示指示を追加。
- 2026/03/31: qualify_log読み込み失敗時に週次レポートが無言でスキップしていた → `slack_notifier.py`にFileNotFoundError/Exception別のSlack警告通知を追加。
- 2026/03/31: 米市場テーマ通知のボリュームが大きすぎた → キーワードTOP8→5件・セクター5→3件・日本株波及5→3件・リスクワード3→2件に削減。プロンプト側の指定件数も同様に変更。
- 2026/04/01: 米市場テーマ通知にセクター強弱速報を追加 → `notify_us_theme_extraction`に`sector_ranking`引数追加。テーマ通知末尾に強いセクターTOP3・弱いセクターTOP2（当日・5日騰落率）を表示。ETFスキャン結果を流用するため追加APIコストなし。
- 2026/04/01: 朝夕スキャンの重複通知を排除 → `main.py`のスキャン通知前に`scan_{今日}_{MODE}.json`を読み込んで既通知セットを構築。SHORT_TERM/MOMENTUM=stockCode一致で重複判定、EARNINGS=stockCode::docID一致（同日新規開示は別物として通知）。qualifyサマリーも同様フィルター。クロスシグナル・投資判断・watchlist保存はフルリスト維持。
- 2026/04/04: 学習ループ品質改善（問題1〜4）→ outcome記録をシグナルなしの日も毎スキャン実行。by_volume_rateのゼロ件除外。週次レポートにby_volume_pattern表示追加。find_cross_signalsを「継続」のみに絞る（様子見除外）。
- 2026/04/04: MOメンタム学習ループ実装 → `momentum_log_manager.py`新規作成。MOシグナルを毎スキャン記録し20営業日後のリターン/最大DD/トレンド継続を自動記録。MAギャップ/高値比/出来高トレンド別の勝率分析を週次レポートに追加。qualify_logと別ファイル管理（アウトカム期間が異なるため）。
- 2026/04/04: 米テーマ抽出のJSONパースエラー対策 → `us_theme_extractor.py`のmax_tokens 3000→4000。JSONDecodeError時に1回自動リトライを追加。
- 2026/04/04: surgeReasonタグ全件「不明」問題を修正 → `_generate_surge_reasons_batch`のmax_tokens固定600→銘柄数×120で動的調整（13銘柄で切断されていた）。`_get_surge_tag`でClaudeの出力ブレ（[]なし・先頭空白等）を吸収するよう強化。
- 2026/04/04: 米テーマ通知のセクター当日騰落を再設計 → ソート基準を中長期スコアから`mom1d`（当日騰落率）に変更。件数をベスト5/ワースト3に統一（中長期セクションと合わせる）。見出しを「🔥 強いセクター TOP5（当日）/🔻 弱いセクター（当日）」に変更。
- 2026/04/05: ペーパートレード無効化 → 自動追加ブロック（`main.py`）を削除。学習ループに不要と判断。`investment_advisor.py`の`normalize_qualify_label`NameErrorも同時修正（毎回サイレントスキップしていた）。
- 2026/04/05: MOフィードバックループ接続 → `momentum_log_manager.py`に`score_signals_by_patterns()`追加。MOシグナルに期待勝率・パターン注を付与してソート。`momentum_qualifier.py`のStage2プロンプトに過去パターン（MAギャップ/高値比/出来高トレンド別勝率）を注入。
- 2026/04/05: 決算フィードバックループ接続 → `earnings_momentum_scanner.py`に`get_earnings_patterns()`/`score_earnings_signal_by_patterns()`追加（3軸: catalyst_type/momentum_potential/edinet_score_band）。`edinet_analyzer.py`のプロンプトにmomentum_potential実績を注入（Claudeの自己改善ループ）。`main.py`でスコアリングを呼び出し。
- 2026/04/05: EDINET日次サマリー通知を追加 → `slack_notifier.py`に`notify_edinet_daily_summary()`追加。毎スキャン時に全書類数・決算関連シグナル数・Claude分析完了数・PDF失敗数をSlack通知。閑散期か否かが一目で分かる。
- 2026/04/08: Slack通知を全面整理 → ①週次レポートのペーパートレードセクション削除 ②EDINET日次サマリーを閑散期はサイレントに（夕方17時以降はforce_send=Trueで常時送信） ③米市場通知を2通→1通に統合（`notify_us_combined`新規追加。セクター強弱を当日/中長期2軸表示、両軸一致セクターにUS・日本銘柄ピック追加、注目テーマTOP5） ④夕方シグナル通知に重複除外件数を末尾表示 ⑤決算低スコア表示を件数のみに簡略化 ⑥`notify_daily_report`（ペーパー300万円ベース）を`notify_actual_positions`に差し替え ⑦表記統一（「MOメンタム」→「モメンタム」「中長期MO」→「中長期モメンタム」）
- 2026/04/09: Claudeモデル廃止対応 → `claude-sonnet-4-5-20241022`が廃止済みのため`claude-sonnet-4-6`に更新（`momentum_qualifier.py` / `edinet_analyzer.py`）。Stage2が全件「様子見」になっていた問題・EDINET分析0件問題が解消。
- 2026/04/09: 正午スキャン通知の時刻表示バグ修正 → `slack_notifier.py`の`datetime.now()`をJST対応に（UTC表示「05:22現在」→「14:22現在」）。
- 2026/04/09: 米市場通知を改善 → セクター強弱（当日・中長期）の各セクターに米国・日本代表銘柄3つずつ表示。両軸一致セクターセクションを削除（意義薄）。注目テーマTOP5にも米国・日本代表銘柄3つずつ追加。`us_theme_extractor.py`プロンプトに`us_stocks`/`jp_stocks`フィールドを追加。セクターラベル「強/弱」→「TOP/WORST」に変更。
- 2026/04/09: 米市場セクターの日本銘柄に証券コードを追加 → `us_market_scanner.py`のSECTOR_ETFSの全17セクターの`japan_theme`を「銘柄名(4桁コード)」形式に統一。カテゴリ説明（「半導体・IT・ソフトウェア」等）から具体的な銘柄+コードに変更。
- 2026/04/10: momentum_log.jsonのキャッシュ配線を修正 → evening-scanのキャッシュ復元ステップにmomentum_logが欠落していた。`daily_report.yml`に追加。合わせて`memory/momentum_log.json`を`[]`で初期化してgitにコミット。
- 2026/04/10: 週次レポートの実売買ポジション表示バグ修正 → `slack_notifier.py`の`_generate_weekly_observations`が`[]`ハードコードだったため「現在保有なし」常時表示。`get_actual_positions()`を呼ぶように修正。
- 2026/04/10: J-Quants決算速報（/fins/summary）をHaikuで分析する仕組みを追加 → `jquants_earnings_analyzer.py`新規作成。夕方18:30スキャンに統合（EDINETは数日遅延のため同日検知に不向きと判明）。定量フィルタ（OP YoY +20%以上 or 通期進捗70%以上）→ Haiku判定 → Slack通知「📊 決算速報 正サプライズ」の流れ。EDINETベースの翌朝照合+Sonnet qualifyパイプラインは維持。夕方cronを18:00→18:30に変更（速報配信30分待ち）。
- 2026/04/11: TDnet適時開示タイトルスキャンをHaikuで分類する仕組みを追加 → `tdnet_fetcher.py`に`analyze_disclosures_with_haiku()`追加。全開示をキーワードフィルタ後Haikuで STRONG/WATCH/SKIP に分類し18:30通知。J-Quantsサプライズと同一銘柄が一致した場合は🚨ダブルシグナルとして別途通知（`notify_double_signals()`）。TDnetのSTRONGのみダブルシグナル対象（WATCHはノイズ除去）。18:30の通知順序: ①J-Quants速報 → ②TDnet開示 → ③ダブルシグナル。翌日モメンタム候補の事前スクリーニングが目的。

---

## 現在の状況（2026/04/11時点）
- 5ジョブ＋週次レポート（戦略有効性モニター付き）で安定稼働中
- **3つの学習ループがすべて記録→フィードバックまで接続済み**
  - SHORT_TERM: qualify_log → 10日後outcome → Stage2プロンプトへ注入
  - MOMENTUM: momentum_log → 20日後outcome → score_signals_by_patterns → Stage2プロンプトへ注入
  - EARNINGS: earnings_signal_log → 10日後outcome → score_earnings_signal_by_patterns → edinet_analyzerプロンプトへ注入
- **決算速報パイプライン（2026/04/10〜）**: J-Quants 18:00速報 → Haiku分析 → 18:30通知。EDINETは翌朝照合用として継続。
- **TDnet適時開示スキャン（2026/04/11〜）**: 全開示タイトルをHaikuで分類 → 18:30通知。J-Quantsとのダブルシグナル検出で翌日モメンタム候補を事前スクリーニング。
- EDINET日次サマリー通知: 閑散期はサイレント（夕方のみ常時送信）
- ペーパートレード: 無効化済み（trade_logは実売買記録専用）
- 米市場通知: セクター強弱（当日/中長期）＋各セクターに米国・日本代表銘柄3つずつ表示。注目テーマTOP5にも同様。両軸一致セクションは廃止。
- Claudeモデル: Sonnet系は`claude-sonnet-4-6`、Haiku系は`claude-haiku-4-5-20251001`を使用
- 次のマイルストーン①: 各ログ5件蓄積 → パターン分析自動発動確認（5月決算シーズンが本番）
- 次のマイルストーン②: J-Quants速報シグナルと翌朝急騰の照合精度を確認（2〜3週後）
- 次のマイルストーン③: TDnetダブルシグナルと翌日モメンタムの照合精度を確認（2〜3週後）
- 次のマイルストーン④: フェーズ5 - かいさんの投資判断ロジックをプロンプトに組み込み・精度検証
