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
│   ├── investment_advisor.py        # ★フェーズ4: 投資判断エージェント（新規）
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
    └── qualify_log.json             # モメンタム判定ログ（★GitHub Actionsでキャッシュ済み）
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

## GitHub Actions スケジュール

| 時刻 | ジョブ | 内容 |
|---|---|---|
| 夕方 18:00 JST（UTC 09:00・月〜金） | evening-scan | fetch → scan（＋qualify判定＋**投資判断**） → status通知 |
| 朝 06:00 JST（UTC 21:00・月〜土） | morning-scan | fetch → scan（＋qualify判定＋**投資判断**） → status通知 → us_scan |

**キャッシュ対象：**
- `memory/trade_log.json`
- `memory/qualify_log.json` ★新規追加
- `data/processed/scans/`

**エラー監視：** 各ステップ失敗時にSlackへcurlで直接通知。

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

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- Claude APIで決算PDFを読解してポジ/ネガスコアリング

---

## モメンタム判定モジュール（momentum_qualifier.py）

### ステージ1: 出来高継続パターン判定
- 急騰後3日間の出来高が急騰日の50%以上を維持しているか
- 急騰後の株価が急騰終値の97%以上を維持しているか

### ステージ2: Claude APIによる構造的変化判定（バッチ処理）
- ステージ1通過銘柄を全てまとめて1回のAPI呼び出しで判定

### 判定結果
| 結果 | 条件 |
|---|---|
| STRONG | ステージ1✅ + Claude判定✅ |
| WATCH | ステージ1✅ + APIキーなし |
| WEAK | ステージ1✅ + Claude判定❌ |
| NOISE | ステージ1❌ |

### 重複防止（2026/03/17追加）
- `_save_qualify_log()` で同一(stockCode, scanDate)は上書き更新（重複追記しない）

### フェーズ3: 10営業日後の結果自動記録
- `qualify_signals()`実行のたびに`record_outcomes()`が自動呼び出し
- `outcome=null`のエントリで10営業日以上経過しているものに株価リターンを記録
- `get_outcome_stats()`でSTRONG/WEAK/NOISEごとの勝率・平均リターンを集計
- 記録が5件以上溜まるとSlack通知に精度サマリーが追加表示

---

## フェーズ4: 投資判断エージェント（investment_advisor.py）★新規

### 概要
qualify結果・PF・ポートフォリオ余力・米市場シグナルを統合して
「エントリー推奨 / 様子見 / 見送り」をSlackに通知する。
夕方18時通知の最後に自動で付加される。

### 推奨判定ロジック（ルールベース）
| 推奨 | 条件 |
|---|---|
| ENTRY（エントリー推奨） | qualifyResult==STRONG かつ PF≥1.2 かつ ポートフォリオ余力あり |
| WATCH（様子見） | STRONG だが PF不足 or 余力なし |
| SKIP（見送り） | WEAK / NOISE |

### 加点要素（Slackに表示）
- 関連セクターETFが上昇中（us_market_scanner結果）
- STRONG過去勝率（qualify_log蓄積後・5件以上で表示）

### Slack通知イメージ
```
🎯 投資判断サマリー
━━ エントリー推奨 ━━
96780 カナモト
  ✅ STRONG判定（4期連続増収...）
  ✅ PF 1.8（バックテスト良好）
  ✅ PF余力あり（3/10枠使用中）
  📌 翌朝始値目安: ¥4,357  損切: ¥4,139  利確: ¥5,011
  💴 投資額目安: 50万円
━━ 見送り ━━
67400 ジャパンディスプレイ — ❌ WEAK（仕手的急騰...）
```

---

## qualify_log.json の現状（2026/03/17時点）

- **8件**（2026-03-11スキャン分・重複クリーンアップ済み）
- 全件 `outcome=null`（3/25以降に自動記録予定）
- 内訳: STRONG 5件 / WEAK 3件

| 銘柄 | 結果 |
|---|---|
| カナモト(96780) | STRONG |
| コーエーテクモ(36350) | STRONG |
| テスHD(50740) | STRONG |
| 任天堂(79740) | STRONG |
| 日本新薬(45160) | STRONG |
| ビューティガレージ(31800) | WEAK |
| KLab(36560) | WEAK |
| ジャパンディスプレイ(67400) | WEAK |

---

## PF計算ロジック（main.py）

`_calc_pf_for_mode(mode)`関数でPFを計算する。
- ローカルCSVの最終日から**15営業日前**の日付でスキャン→バックテスト→PF算出
- GitHub Actionsのクリーン環境でも毎回正しく計算される

---

## ペーパートレード設定
- 仮想資金：300万円
- 1銘柄あたり最大50万円
- 最大同時保有：10銘柄
- 発注条件：PF ≥ 1.2

---

## バックテスト設定
- エントリー：シグナル翌営業日の始値
- 損切り：-5%
- 利確：+15%
- 最大保有：10営業日

---

## フェーズ進捗

### ✅ フェーズ1（基盤構築・完了）
### ✅ フェーズ2（Claude API統合・完全動作確認済み）
### ✅ フェーズ2.5（品質改善・2026/03/17完了）
1. PF=0再発修正
2. agents/utils.py共通化
3. GitHub Actionsエラー監視
4. フェーズ3入口実装（record_outcomes）

### ✅ フェーズ3（蓄積基盤・2026/03/17完了）
1. qualify_log重複防止（同一stockCode+scanDateは上書き）
2. qualify_log既存重複クリーンアップ（64件→8件）
3. `--mode qualify_report` コマンド追加
4. qualify_log.jsonをGitHub Actionsキャッシュに追加

### ✅ フェーズ4（投資判断エージェント・2026/03/17完了）
1. `agents/investment_advisor.py` 新規作成
2. run_scan_mode に統合（夕方スキャン後に自動でSlack通知）

### 🔲 フェーズ3継続（データ蓄積後）
- qualify_log outcome が5件以上溜まったら精度レポートを確認
- STRONGの実績勝率をinvestment_advisorの判断に反映

### 🔲 フェーズ5（今後）
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 実際のエントリー判断の精度を検証・改善

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・推奨）
python main.py --mode fetch --days 150

# 日本株スキャン（qualify判定＋投資判断も自動実行）
python main.py --mode scan

# 判定精度レポート確認
python main.py --mode qualify_report

# 米市場スキャン
python main.py --mode us_scan

# バックテスト
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
- **ファイル編集はWindows-MCP:FileSystemツールで直接行う（PowerShellはgitでハングするため使わない）**
- **GitHub上での直接編集は避ける**
- **Consoleを開くときは必ずブラウザの翻訳をオフに！**（翻訳がHTMLを壊してエラーになる）
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**

---

## 現在の状況（2026/03/17夜時点）
- 夕方18時・翌朝6時にSlack自動通知が稼働中
- **フェーズ4まで実装完了** — 次回スキャンから投資判断がSlackに届く
- qualify_logは8件蓄積中。3/25以降にoutcome自動記録が始まる予定
- 次のマイルストーン: outcome 5件以上溜まったら精度レポートで検証
