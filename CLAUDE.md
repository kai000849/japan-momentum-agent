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
- Claude API（Haiku 4.5・フェーズ2実装中・APIキー未登録）
- yfinance（米国ETFデータ取得・無料）

---

## ファイル構成

```
japan-momentum-agent/
├── main.py                          # エントリーポイント
├── requirements.txt                 # yfinance追加済み
├── scheduler_setup.py
├── .github/workflows/
│   └── daily_report.yml             # GitHub Actions自動実行設定
├── agents/
│   ├── scanner.py                   # スクリーニング（3モード）
│   ├── jquants_fetcher.py           # 株価データ取得（J-Quants V2 API）
│   ├── edinet_fetcher.py            # 開示情報取得
│   ├── edinet_analyzer.py           # 決算PDFをClaude APIで読解・スコアリング（新規）
│   ├── us_market_scanner.py         # 米国セクターETF17本モメンタム（新規）
│   ├── us_theme_extractor.py        # 米財務メディアからキーワード抽出（新規）
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理
│   ├── slack_notifier.py            # Slack通知（決算・米市場通知追加済み）
│   └── analyst.py                   # 銘柄分析コメント（未有効化）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   ├── raw/edinet_pdfs/             # 決算短信PDFキャッシュ
│   ├── processed/scans/             # スキャン結果JSON
│   ├── processed/us_scans/          # 米ETFスキャン結果JSON
│   └── processed/us_themes/         # 米テーマ抽出結果JSON
└── memory/
    ├── trade_log.json               # 取引履歴
    └── disclosure_log.json          # 開示情報履歴
```

---

## GitHub Secrets
| キー名 | 内容 |
|---|---|
| `JQUANTS_API_KEY` | J-Quants APIキー（登録済み） |
| `EDINET_API_KEY` | EDINET APIキー（登録済み） |
| `SLACK_WEBHOOK_URL` | Slack Webhook URL（登録済み） |
| `ANTHROPIC_API_KEY` | **未登録・要対応**（Console復旧後にキー取得→登録） |

---

## GitHub Actions スケジュール

| 時刻 | ジョブ | 内容 |
|---|---|---|
| 朝 06:00 JST（UTC 21:00・日曜除く） | morning-scan | 日本株スキャン＋シグナル通知 |
| 朝 06:00 JST（UTC 21:00・日曜除く） | us-market-scan | 米ETFモメンタム＋キーワード通知 |
| 夕方 16:00 JST（UTC 07:00・平日） | evening-report | ポートフォリオ状況レポート |

※ GitHub Actionsの遅延を考慮して1時間前倒し設定済み。実際の通知は7時・17時を想定。

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
- **新高値更新スコア**: 直近20日で過去52週高値を更新した回数
- **出来高トレンド比率**: 直近5日平均 ÷ 25日平均
- スコア = RSI × 高値比率 × 全MA上昇ボーナス × 新高値ボーナス × 出来高トレンドボーナス

### 決算開示モード（EARNINGS）
- EDINETから当日の決算発表銘柄を抽出
- 書類コード180（決算短信）・130・140・030が対象
- **Claude APIで決算PDFを読解してポジ/ネガスコアリング（APIキー登録後に有効化）**
- ベスト/ワースト各10件をSlackに通知

---

## 米市場スキャン（新機能）
### ETFモメンタムスキャン（us_market_scanner.py）
- 監視ETF: 17本（XLK・SOXX・XLV・XLE・ITA・ARKK・AIQ・ROBO等）
- 5日・20日・60日モメンタム + 出来高トレンドで総合スコア算出
- Claude API（web_search付き）でセクター強弱の理由と日本株波及先を分析

### キーワード抽出スキャン（us_theme_extractor.py）
- 情報ソース: Yahoo Finance・WSJ・MarketWatch・FT・Investing.com（5メディア・約100件/日）
- Claude API（web_search付き）で細分化キーワードを抽出
  - 例：「AI」→「光インターコネクト」「HBM4メモリ」「原子力データセンター」
- セクター別サブテーマ・日本株波及先・リスクワードをSlackに通知

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
- **過去スキャン結果ファイルでバックテストする（当日データではPF=0になる問題を解決済み）**

---

## フェーズ進捗

### ✅ フェーズ1（基盤構築・完了）
（省略・過去の修正履歴はGitログ参照）

### 🔄 フェーズ2（Claude API統合・実装済み・APIキー待ち）

**2026/03/13に実装した内容：**
1. `agents/edinet_analyzer.py` 新規作成
   - EDINET APIから決算短信PDFをダウンロード
   - Claude Haiku APIで売上/利益前年比・予想比・ポジ/ネガスコア（-100〜+100）・理由コメントを抽出
   - `main.py`のEARNINGSモードに組み込み済み
2. `agents/us_market_scanner.py` 新規作成
   - yfinanceで米国セクターETF17本のモメンタムを毎朝取得
   - Claude API（web_search付き）でセクター強弱の背景と日本株波及先を分析
3. `agents/us_theme_extractor.py` 新規作成
   - 米財務メディア5社RSSから約100件のニュースを収集
   - Claude API（web_search付き）で「光インターコネクト」「HBM4」等の細分化キーワードを抽出
4. `agents/slack_notifier.py` 更新
   - 決算スコアリング（ベスト/ワーストランキング・株価表示削除）
   - 米ETFモメンタム通知
   - 米キーワード通知
5. `main.py` 更新 - `--mode us_scan` 追加
6. `daily_report.yml` 更新 - ANTHROPIC_API_KEY追加・us-market-scanジョブ追加
7. `requirements.txt` 更新 - yfinance追加
8. **全てgit push済み（コミット: `feat: 米市場スキャン・キーワード抽出追加 + yfinance追加`）**

**【最優先・次回セッション冒頭】ANTHROPIC_API_KEY登録手順：**
1. https://console.anthropic.com でクレジット購入（$5〜）＋キー確認
   - **注意**: Googleアカウントログインだと請求ページでエラーになる事例あり
   - **解決策**: メールアドレス＋パスワードで直接ログインする（「パスワードを忘れた」でリセット可）
   - それでもエラーの場合はブラウザのキャッシュクリア or シークレットモードで試す
2. https://github.com/kai000849/japan-momentum-agent/settings/secrets/actions で `ANTHROPIC_API_KEY` を登録

### 🔲 フェーズ3（過去パターンの蓄積と学習）
- 「このチャートパターン・この値動きは過去何回中何回成功」という統計を自動蓄積
- `memory/pattern_log.json` に記録・Slack通知に添付

### 🔲 フェーズ4（投資判断をエージェントに任せる）
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 「迷ったならこう判断する」というルールをエージェントに覚えさせる

---

## 現在の状況（2026/03/13夜時点）
- 毎朝7時・夕方17時にSlack自動通知が稼働中
- 株価データ：99営業日分（3596銘柄）
- モメンタムスキャン：207銘柄ヒット
- PF=0バグ修正済み・稼働確認中
- **ANTHROPIC_API_KEY未登録のため決算分析・米市場キーワード分析は未稼働**
- ペーパートレード：発注なし（PF<1.2のため）
- **Console請求ページのログイン問題が未解決（次回セッション冒頭で再試行）**

---

## よく使うコマンド
```bash
cd C:\Users\dgwrt\OneDrive\Desktop\japan-momentum-agent

# データ取得（150日分・推奨）
python main.py --mode fetch --days 150

# 日本株スキャン
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
- APIキーは絶対にコードに直書きしない
- J-Quantsライトプラン：最新データが取得可能（2週間遅延なし）
- Windowsのコマンドプロンプトで作業（PowerShellでも可）
- **ファイル編集はパソコンで行い、git pushでGitHubに反映する（GitHub上での直接編集は避ける）**
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**
