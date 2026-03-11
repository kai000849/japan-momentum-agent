# Japan Momentum Agent - プロジェクト引き継ぎメモ

## このファイルの使い方
新しいチャットセッションを開始したら、このファイルをそのままClaudeに貼り付けてください。
すぐに作業に入れます。

---

## プロジェクト概要

日本株モメンタム投資の自動化AIエージェント。
毎朝7時に銘柄スクリーニングを実行し、Slackに通知する。

- **GitHubリポジトリ**: https://github.com/kai000849/japan-momentum-agent
- **ローカルパス**: `C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent`
- **Slack通知チャンネル**: #all-japan-momentum-agent

---

## 技術スタック

- Python（メイン言語）
- J-Quants API（株価データ取得）← ライトプラン契約済み（月1,650円）
- EDINET API（決算情報取得）
- GitHub Actions（自動実行）
- Slack Webhook（通知）
- Claude API（フェーズ2以降で使用予定）

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
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理
│   ├── slack_notifier.py            # Slack通知
│   └── analyst.py                   # Claude API銘柄分析（フェーズ2・未有効化）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   └── processed/scans/             # スキャン結果JSON
└── memory/
    ├── trade_log.json               # 取引履歴
    └── disclosure_log.json          # 開示情報履歴
```

---

## GitHub Secrets（登録済み）

| キー名 | 内容 |
|---|---|
| `JQUANTS_API_KEY` | J-Quants APIキー（2026/02/28登録） |
| `EDINET_API_KEY` | EDINET APIキー |
| `SLACK_WEBHOOK_URL` | Slack Webhook URL |
| `ANTHROPIC_API_KEY` | 未登録（フェーズ2で必要） |

---

## GitHub Actions スケジュール

| 時刻 | 内容 |
|---|---|
| 毎朝 07:00 JST（平日） | スキャン＆シグナル通知（morning-scanジョブ） |
| 毎夕 17:00 JST（平日） | ポートフォリオ状況レポート（evening-reportジョブ） |

---

## スクリーニングモード

### 急騰モード（SHORT_TERM）
- 前日比 +3%以上
- 出来高が25日平均の2倍以上
- 急騰スコア = 前日比(%) × 出来高倍率

### モメンタムモード（MOMENTUM）
- 5日・25日・75日MA全て上昇中（75日MA必須）
- 現在株価が52週高値の**85%以上**（2026/03/10修正：95%→85%）
- RSI(14日)が**50〜75**の範囲（2026/03/10修正：55-70→50-75）
- **【2026/03/05追加】新高値更新スコア**: 直近20日で何回52週高値を更新したか
- **【2026/03/05追加】出来高増加トレンド**: 直近5日平均 ÷ 25日平均（増加中かを評価）
- スコア = RSI × 高値比 × 全MA上昇ボーナス × 新高値ボーナス × 出来高トレンドボーナス

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- 書類コード180（決算短信）・130・140・030が対象
- **【2026/03/05修正】最新終値を株価データから紐付けて表示するよう修正**

---

## ペーパートレード設定

- 仮想資金：300万円
- 1銘柄あたり最大：30万円
- 最大同時保有：10銘柄
- 発注条件：バックテストのPF（プロフィットファクター）≥ 1.2

---

## バックテスト設定

- エントリー：シグナル翌営業日の始値
- 損切り：-5%
- 利確：+15%
- 最大保有：10営業日
- **【2026/03/05修正】過去スキャン結果ファイルでバックテストするよう修正（当日データではPF=0になる問題を解消）**

---

## フェーズ別進捗

### ✅ フェーズ1：基盤構築（完了）

**2026/03/04に修正した内容：**
1. `main.py` - スキャン後にバックテストを実行してPFを正しく計算してからSlack通知するよう修正
2. `daily_report.yml` - 通知を朝9時1回→朝7時・夕方17時の2回に変更
3. `scanner.py` - モメンタム条件を厳格化（741銘柄→20〜50銘柄に絞り込み）
4. `edinet_fetcher.py` - 決算短信の書類コードを修正（120→180）
5. `japan-stock-agent`リポジトリのDiscord通知ワークフローを無効化

**2026/03/05に修正した内容：**
1. `scanner.py` - MOEMENTUMスコアリング強化（新高値更新回数・出来高増加トレンドをスコアに反映）
2. `scanner.py` - 決算シグナルに最新終値を紐付けて表示（¥0問題を解消）
3. `main.py` - PF=0.00問題を修正（過去スキャン結果ファイルでバックテストするよう変更）
4. `slack_notifier.py` - MOEMENTUMシグナルに新高値・出来高トレンドを表示追加
5. `slack_notifier.py` - 決算シグナルに書類種別を表示追加
6. `agents/analyst.py` - 新規作成（Claude APIで銘柄分析コメントを生成・未有効化）

**2026/03/10に修正した内容：**
1. `jquants_fetcher.py` - REQUEST_INTERVAL_SEC を20秒→3秒に短縮（ライトプランで十分）
2. `scanner.py` - モメンタム条件を緩和（高値比95%→85%、RSI55-70→50-75）
3. `scanner.py` - `target_idx >= 75` チェックを削除（90日分CSVで全銘柄脱落していたバグ修正）
4. 株価CSVを90日分再取得（`quotes_20260310.csv`、1596銘柄・94152レコード）

**2026/03/10時点の未解決問題：**
- モメンタム0銘柄問題：CSVが65営業日分しかなく75日MA計算不可
- **次回やること → `python main.py --mode fetch --days 150` を実行してデータを150日分取得する**

---

### ⬜ フェーズ2：Claude APIによる言語化（検討中）

**やること：**
1. `agents/analyst.py`（作成済み・未有効化）
   - SHORT_TERM・MOMENTUM上位10銘柄に「なぜこの銘柄か」をClaude APIで言語化
   - Slack通知に 💬 で分析コメントを追加
2. EDINET開示文書をClaude APIで要約（未着手）

**有効化に必要なもの：**
- `ANTHROPIC_API_KEY` をGitHub Secretsに登録
- `main.py` はすでに analyst.py を呼び出す処理を組み込み済み

**コスト検討中：**
- Sonnet: 10銘柄/日 → 月数百円程度
- Haiku: 約10分の1のコスト（品質はやや落ちるが分析用途には十分）

---

### ⬜ フェーズ3：知見の蓄積と学習

**やること：**
- 「この開示パターン×この値動きは過去◯回中◯回上昇」という統計を自動蓄積
- `memory/pattern_log.json` に記録
- Slack通知に過去の統計を添付

---

### ⬜ フェーズ4：自分の知見をエージェントに学習させる

**やること：**
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 「自分ならこう判断する」というルールをエージェントに覚えさせる

---

## よく使うコマンド

```bash
# ローカルで手動実行
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分推奨）
python main.py --mode fetch --days 150

# スキャンのみ
python main.py --mode scan

# バックテストまで
python main.py --mode backtest

# 全パイプライン
python main.py --mode full

# ポートフォリオ確認
python main.py --mode status

# GitHubに反映（パソコンで編集後）
git add .
git commit -m "変更内容のメモ"
git push

# GitHubの変更をパソコンに取り込む
git pull
```

---

## 注意事項

- `config.yaml` はGitに含めない（.gitignoreで除外済み）
- APIキーは絶対にコードに直書きしない
- J-Quantsライトプラン：最新データが取得可能（12週間遅延なし）
- Windowsのコマンドプロンプトで作業（PowerShellでも可）
- **ファイル編集はパソコンで行い、git pushでGitHubに反映する（GitHub上での直接編集は避ける）**
