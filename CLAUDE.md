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
│   └── slack_notifier.py            # Slack通知
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   └── processed/scans/             # スキャン結果JSON（現在2日分のみ）
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
- 現在株価が52週高値の95%以上
- RSI(14日)が55〜70の範囲

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- 書類コード180（決算短信）・130・140・030が対象

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

---

## フェーズ別進捗

### ✅ フェーズ1：基盤構築（完了）

**完了済み：**
- 全エージェントファイルの実装（scanner / fetcher / backtester / paper_trader / slack_notifier）
- GitHub Actions自動実行（毎朝7時・毎夕17時）
- Slack通知稼働中
- ペーパートレード（300万円）運用中

**2026/03/04に修正した内容：**
1. `main.py` - スキャン後にバックテストを実行してPFを正しく計算してからSlack通知するよう修正（以前はPF=0.0固定だった）
2. `daily_report.yml` - 通知を朝9時1回→朝7時・夕方17時の2回に変更
3. `scanner.py` - モメンタム条件を厳格化（741銘柄→20〜50銘柄に絞り込み）
   - 75日MA必須化
   - 52週高値比：90% → 95%
   - RSI範囲：50〜75 → 55〜70
4. `edinet_fetcher.py` - 決算短信の書類コードを修正（120→180）
5. `japan-stock-agent`リポジトリのDiscord通知ワークフローを無効化

**残課題：**
- スキャン結果ファイルが2日分しかない（バックテストのサンプル数不足）
  → 毎日自動蓄積されるので様子見。過去データの再取得も検討

---

### ⬜ フェーズ2：Claude APIによる言語化（次のフェーズ）

**やること：**
1. `agents/analyst.py` を新規作成
   - スキャン結果の上位銘柄に対して「なぜこの銘柄か」をClaude APIで言語化
   - Slack通知に分析コメントを追加
2. EDINET開示文書をClaude APIで要約
   - 決算短信の内容を自動要約
   - 決算サプライズ（予想比プラス/マイナス）を自動判定
3. `main.py` と `slack_notifier.py` に連携を追加

**使用するAPI：**
- Anthropic Claude API（`claude-sonnet-4-20250514`）
- 必要なSecret：`ANTHROPIC_API_KEY`（GitHub Secretsに追加が必要）

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

# スキャンのみ
python main.py --mode scan

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
```

---

## 注意事項

- `config.yaml` はGitに含めない（.gitignoreで除外済み）
- APIキーは絶対にコードに直書きしない
- J-Quantsライトプラン：最新データが取得可能（12週間遅延なし）
- Windowsのコマンドプロンプトで作業（PowerShellでも可）
