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
│   ├── edinet_analyzer.py           # 決算PDFをClaude APIで読解・スコアリング
│   ├── momentum_qualifier.py        # モメンタム判定（2段階フィルター）【新規】
│   ├── us_market_scanner.py         # 米国セクターETF17本モメンタム
│   ├── us_theme_extractor.py        # 米財務メディアからキーワード抽出
│   ├── backtester.py                # バックテスト
│   ├── paper_trader.py              # ペーパートレード管理
│   ├── slack_notifier.py            # Slack通知
│   └── analyst.py                   # 銘柄分析コメント（未有効化）
├── data/
│   ├── raw/jquants/                 # 取得済み株価CSV
│   ├── raw/edinet_pdfs/             # 決算短信PDFキャッシュ
│   ├── processed/scans/             # スキャン結果JSON
│   ├── processed/us_scans/          # 米ETFスキャン結果JSON
│   └── processed/us_themes/         # 米テーマ抽出結果JSON
└── memory/
    ├── trade_log.json               # 取引履歴
    ├── disclosure_log.json          # 開示情報履歴
    └── qualify_log.json             # モメンタム判定ログ【新規】
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
| 夕方 18:00 JST（UTC 09:00・月〜金） | evening-scan | スキャン＋シグナル＋保有状況（メイン通知） |
| 朝 06:00 JST（UTC 21:00・月〜金） | morning-scan | 再スキャン＋シグナル＋保有状況＋米市場スキャン |

※ 夕方通知がメイン。夜に翌日の発注を検討し、朝通知で最新情報を確認する運用。

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

## モメンタム判定モジュール（momentum_qualifier.py）【新規・2026/03/13追加】

### 設計思想
出来高を伴った急騰が「短期的な加熱」なのか「構造的変化を伴った中長期モメンタムの始まり」なのかを2段階で判定する。

### ステージ1: 出来高継続パターン判定（数値）
- 急騰後3日間の出来高が急騰日の50%以上を維持しているか
- 急騰後の株価が急騰終値の97%以上を維持しているか（-3%以内の下落は許容）
- 通過 → WATCH or STRONG候補 / 不通過 → NOISE

### ステージ2: Claude APIによる構造的変化判定
- ステージ1通過銘柄のみClaudeに投げる
- web_search付きで最新ニュース・EDINET開示を確認
- 「構造的変化あり/なし」＋理由コメントを出力
- APIキー未設定時はスキップ（ステージ1結果のみでWATCH通知）

### 判定結果
| 結果 | 条件 | 意味 |
|---|---|---|
| STRONG | ステージ1✅ + Claude判定✅ | 構造的変化あり・中長期期待大 |
| WATCH | ステージ1✅ + APIキーなし | 要観察（API登録後に再判定） |
| WEAK | ステージ1✅ + Claude判定❌ | 短期加熱の可能性 |
| NOISE | ステージ1❌ | 出来高・株価が継続せず |

### 動作
- `python main.py --mode scan` 実行時にSHORT_TERMシグナルに対して自動判定
- 結果は `memory/qualify_log.json` に蓄積（最新500件）
- Slack通知: STRONG/WATCHのみ通知

---

## 米市場スキャン
### ETFモメンタムスキャン（us_market_scanner.py）
- 監視ETF: 17本（XLK・SOXX・XLV・XLE・ITA・ARKK・AIQ・ROBO等）
- 5日・20日・60日モメンタム + 出来高トレンドで総合スコア算出
- Claude API（web_search付き）でセクター強弱の理由と日本株波及先を分析

### キーワード抽出スキャン（us_theme_extractor.py）
- 情報ソース: Yahoo Finance・WSJ・MarketWatch・FT・Investing.com（5メディア・約100件/日）
- Claude API（web_search付き）で細分化キーワードを抽出
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

**実装済み内容：**
1. `agents/edinet_analyzer.py` - 決算短信PDFをClaude APIでスコアリング
2. `agents/us_market_scanner.py` - 米国セクターETF17本のモメンタム分析
3. `agents/us_theme_extractor.py` - 米財務メディアからキーワード抽出
4. `agents/momentum_qualifier.py` - 急騰シグナルの2段階モメンタム判定【2026/03/13追加】
5. `agents/slack_notifier.py` - 各種通知対応済み
6. `main.py` - 全モード対応済み
7. **全てgit push済み（最新コミット: `feat: モメンタム判定モジュール追加`）**

**【最優先・次回セッション冒頭】ANTHROPIC_API_KEY登録手順：**
1. https://console.anthropic.com でクレジット購入（$5〜）＋キー確認
   - **注意**: Googleアカウントログインだと請求ページでエラーになる事例あり
   - **解決策**: メールアドレス＋パスワードで直接ログイン（「パスワードを忘れた」でリセット可）
   - それでもエラーの場合はブラウザのキャッシュクリア or シークレットモードで試す
2. https://github.com/kai000849/japan-momentum-agent/settings/secrets/actions で `ANTHROPIC_API_KEY` を登録
3. 登録後 `python main.py --mode scan` で動作確認（STRONGシグナルが出るか確認）

### 🔲 フェーズ3（過去パターンの蓄積と学習）
- qualify_log.jsonにSTRONG/WEAK/NOISEの結果が蓄積される
- 10営業日後の結果（利確/損切り）を自動記録して判定精度を検証
- 「STRONG判定の勝率」「出来高維持率と勝率の相関」などの統計を可視化

### 🔲 フェーズ4（投資判断をエージェントに任せる）
- かいさん自身の投資判断ロジックをプロンプトに組み込む
- 「迷ったならこう判断する」というルールをエージェントに覚えさせる

---

## 現在の状況（2026/03/13夜時点）
- 夕方18時・翌朝6時にSlack自動通知が稼働中（朝はus_scan含む）
- 株価データ：99営業日分（3596銘柄）
- SHORT_TERM PF: 1.24 / MOMENTUM PF: 1.53（発注条件クリア済み）
- **ANTHROPIC_API_KEY未登録のため決算分析・米市場分析・STRONG判定は未稼働（WATCH通知のみ）**
- ペーパートレード：発注あり（PF≥1.2のため）
- **Console請求ページのログイン問題が未解決（次回セッション冒頭で再試行）**

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
- APIキーは絶対にコードに直書きしない
- J-Quantsライトプラン：最新データが取得可能（2週間遅延なし）
- Windowsのコマンドプロンプトで作業
- **ファイル編集はClaudeがFileSystemツールで行い、git操作はかいさんがコマンドプロンプトでコピペ実行する（PowerShellツールはgitでハングするため使わない）**
- **GitHub上での直接編集は避ける**
- **CLAUDE.mdの更新はセッション終了時にかいさんが明示的に依頼したときのみ行う**
