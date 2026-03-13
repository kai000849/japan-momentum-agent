# Japan Momentum Agent - プロジェクト引き継ぎメモ

## このファイルの使い方
新しいチャットセッションを開始したら、このファイルをそのままClaudeに貼り付けてください。すぐに作業に入れます。

---

## プロジェクト概要
日本株モメンタム投資の自動化AIエージェント。毎朝7時に銘柄スクリーニングを実行し、Slackに通知する。

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
- Claude API（フェーズ2で使用予定・次回判断）

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
│   └── analyst.py                   # 銘柄分析コメント（フェーズ2・未有効化）
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
| `ANTHROPIC_API_KEY` | 未登録（フェーズ2で必要・次回登録） |

---

## GitHub Actions スケジュール

| 時刻 | 内容 |
|---|---|
| 朝 06:00 JST（UTC 21:00・日曜除く） | スキャン＋シグナル通知（morning-scanジョブ） |
| 夕方 16:00 JST（UTC 07:00・平日） | ポートフォリオ状況レポート（evening-reportジョブ） |

※ GitHub Actionsの遅延を考慮して1時間前倒し設定済み（2026/03/11）。実際の通知は7時・17時を想定。

---

## スクリーニングモード
### 急騰モード（SHORT_TERM）
- 前日比+3%以上
- 出来高が25日平均の2倍以上
- 急騰スコア = 前日比(%) × 出来高倍率

### モメンタムモード（MOMENTUM）
- 5日・25日・75日MA全て上昇中（5日MA必須）
- 現在株価が52週高値の**90%以上**（2026/03/11修正・95%→90%）
- RSI(14日)が**55〜72**の範囲（2026/03/11修正・50-75→55-72）
- **新高値更新スコア**: 直近20日で過去52週高値を更新した回数
- **出来高トレンド比率**: 直近5日平均 ÷ 25日平均（出来高が増加中かを確認）
- スコア = RSI × 高値比率 × 全MA上昇ボーナス × 新高値ボーナス × 出来高トレンドボーナス

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- 書類コード180（決算短信）・130・140・030が対象
- 最新終値を株価データから紐づけて表示

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
- **過去スキャン結果ファイルでバックテストする（修正済み・当日データではPF=0になる問題を解決）**

---

## フェーズ進捗
### ✅ フェーズ1（基盤構築・完了）
**2026/03/04に修正した内容：**
1. `main.py` - スキャン後にバックテストを実行してPFを正しく計算してからSlack通知するよう修正
2. `daily_report.yml` - 通知を朝9時・夕方17時の2回に変更
3. `scanner.py` - モメンタム条件を厳格化（741銘柄→10〜20銘柄に絞り込み）
4. `edinet_fetcher.py` - 決算短信の書類コードを修正（120→180）
5. `japan-stock-agent`リポジトリのDiscord通知ワークフローを無効化

**2026/03/05に修正した内容：**
1. `scanner.py` - MOMENTUMスコアリング強化（新高値更新回数・出来高増加トレンドをスコアに反映）
2. `scanner.py` - 決算シグナルに最新終値を紐づけて表示（¥0表示を解消）
3. `main.py` - PF=0.00問題を修正（過去スキャン結果ファイルでバックテストするよう変更）
4. `slack_notifier.py` - MOMENTUMシグナルに新高値・出来高トレンドを表示追加
5. `slack_notifier.py` - 決算シグナルに書類種別を表示追加
6. `agents/analyst.py` - 新規作成（Claude APIで銘柄分析コメントを生成・未有効化）

**2026/03/10に修正した内容：**
1. `jquants_fetcher.py` - REQUEST_INTERVAL_SEC を0.3秒に短縮
2. `scanner.py` - モメンタム条件を調整（高値比率95%→95%、RSI55-70→50-75）
3. `scanner.py` - `target_idx >= 75` チェックを削除（60日分CSVで全銘柄閾超していたバグ修正）
4. 株価CSV90日分を取得（`quotes_20260310.csv`・3596銘柄・94152レコード）

**2026/03/11に実施した内容：**
1. `python main.py --mode fetch --days 150` を実行→99営業日分（157,873レコード）取得完了
   - 75日MA計算に必要なデータが揃い、モメンタム0銘柄問題を解消
2. `scanner.py` 条件を再調整（高値比率95%→90%、RSI50-75→55-72）
   - 448銘柄 → 207銘柄に絞り込み
3. `daily_report.yml` - GitHub Actionsスケジュールを1時間前倒し（夏時間対策）
   - 朝: UTC 22:00 → UTC 21:00
   - 夕: UTC 08:00 → UTC 07:00
4. 上記をgit push済み

**2026/03/13に実施した内容：**
1. PF=0バグの根本原因を特定・修正
   - **真の原因**: `main.py`の`run_scan_mode`内で過去スキャンファイルを`reverse=True`（新しい順）で読み、20件で打ち切っていたため、将来データが1日分しかない直近ファイルで止まっていた
   - **修正内容**: `reverse=False`（古い順）に変更 + 20件打ち切り（`if len(past_results) >= 20: break`）を削除
2. 3ファイル総点検完了（main.py・backtester.py・slack_notifier.py）→全て正常確認
3. git push済み（コミット: `fix: PF=0バグ修正 古いスキャンファイルを全件使用`）

---

### 🔲 フェーズ2（Claude APIによる解説・次回判断）
**目的：**
シグナルが上がっても「なぜこの銘柄か」の理由がSlack通知に含まれず、毎回手動確認が必要。Claude APIでシグナル理由のコメントを自動生成することで時間を節約。

**やること：**
1. `ANTHROPIC_API_KEY` をGitHub Secretsに登録
2. `agents/analyst.py`（作成済み・未有効化）を有効化
   - SHORT_TERM・MOMENTUM上位銘柄に「なぜこの銘柄か」をClaude APIで解説
   - Slack通知に 🤖 で分析コメントを追加
3. EDINET開示文書をClaude APIで要約（次回可）

**コスト見込み：**
- Haiku 4.5使用時：少数銘柄以上（J-Quants月1,650円と比べてもほぼ誤差レベル）
- Sonnet 4.6使用時：少数銘柄以内
- どちらでも運用上問題ない

---

### 🔲 フェーズ3（過去パターンの蓄積と学習）
**やること：**
- 「このチャートパターン・この値動きは過去何回中何回成功」という統計を自動蓄積
- `memory/pattern_log.json` に記録
- Slack通知に過去の統計を添付

---

### 🔲 フェーズ4（投資判断をエージェントに任せる）

**やること：**
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 「迷ったならこう判断する」というルールをエージェントに覚えさせる

---

## 現在の状況（2026/03/13時点）
- 毎朝7時・夕方17時にSlack自動通知が稼働中
- 株価データ：99営業日分（quotes_20260311.csv・3596銘柄）
- モメンタムスキャン：207銘柄ヒット（条件再調整後）
- **PF=0バグ修正済み・git push済み（2026/03/13）→明朝7時の通知で確認予定**
- バックテスト PF：SHORT_TERM=0.43、MOMENTUM=0.81（過去データ少ないため・修正後に改善見込み）
- ペーパートレード：発注なし（PF<1.2のため）

---

## よく使うコマンド
```bash
# ローカルで手動実行
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・推奨）
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
- J-Quantsライトプラン：最新データが取得可能（2週間遅延なし）
- Windowsのコマンドプロンプトで作業（PowerShellでも可）
- **ファイル編集はパソコンで行い、git pushでGitHubに反映する（GitHub上での直接編集は避ける）**
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**
