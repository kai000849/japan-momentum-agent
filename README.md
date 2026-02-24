# Japan Momentum Agent

日本株モメンタムトレードに特化したAIエージェントです。
J-Quants APIとEDINET APIを使って、急騰銘柄やモメンタム銘柄をスクリーニングし、バックテスト・ペーパートレードができます。

---

## 必要なもの

- **Python 3.9以上**（[ダウンロード](https://www.python.org/downloads/)）
- **J-Quantsアカウント**（無料プランあり）
- **EDINETアカウント**（無料）

---

## セットアップ手順

### Step 1: 各APIのアカウント登録

#### J-Quants（株価データ）
1. [https://jpx-jquants.com/](https://jpx-jquants.com/) にアクセス
2. 「新規登録」でアカウント作成
3. メールアドレスとパスワードをメモしておく
4. フリープランでも動作しますが、プレミアムプランの方が取得できるデータが多い

#### EDINET（開示書類）
1. [https://api.edinet-fsa.go.jp/](https://api.edinet-fsa.go.jp/) にアクセス
2. APIキーを申請・取得
3. APIキーをメモしておく

---

### Step 2: リポジトリをダウンロード

```bash
# このフォルダをダウンロードするか、GitHubからクローンする
cd japan-momentum-agent
```

---

### Step 3: Pythonの仮想環境を作成

**Windowsの場合：**
```bash
python -m venv venv
venv\Scripts\activate
```

**Mac/Linuxの場合：**
```bash
python3 -m venv venv
source venv/bin/activate
```

---

### Step 4: 必要なライブラリをインストール

```bash
pip install -r requirements.txt
```

インストールが完了したら以下でバージョン確認：
```bash
pip list
```

---

### Step 5: APIキーを設定

`config.yaml` ファイルを開き、取得したAPIキーを入力します：

```yaml
jquants:
  email: "登録したメールアドレス"
  password: "パスワード"

edinet:
  api_key: "取得したEDINET APIキー"
```

> **重要**: `config.yaml` は絶対にGitHubにアップロードしないこと！
> `.gitignore` で除外設定済みです。

---

### Step 6: 動作確認

```bash
# ペーパートレードの状況確認（一番軽い動作確認）
python main.py --mode status

# 急騰銘柄スキャン
python main.py --mode scan --type short

# モメンタム銘柄スキャン
python main.py --mode scan --type momentum

# 決算銘柄スキャン
python main.py --mode scan --type earnings

# スキャン＋バックテスト
python main.py --mode backtest

# 全パイプライン実行（スキャン→バックテスト→ペーパートレード）
python main.py --mode full
```

---

## ディレクトリ構成

```
japan-momentum-agent/
├── data/
│   ├── raw/
│   │   ├── jquants/        # J-Quantsから取得した生データ（CSV）
│   │   └── edinet/         # EDINETから取得した開示情報（JSON）
│   └── processed/
│       ├── scans/           # スクリーニング結果（JSON）
│       ├── backtests/       # バックテスト結果（JSON）
│       └── disclosures/     # 加工済み開示情報（JSON）
├── agents/
│   ├── jquants_fetcher.py   # J-Quants APIデータ取得
│   ├── edinet_fetcher.py    # EDINET API開示情報取得
│   ├── scanner.py           # スクリーニングロジック
│   ├── backtester.py        # バックテスト
│   └── paper_trader.py      # ペーパートレード管理
├── memory/
│   ├── trade_log.json       # 取引履歴（自動更新）
│   └── disclosure_log.json  # 開示情報履歴（自動更新）
├── notebooks/               # Jupyter Notebook用フォルダ
├── config.yaml              # APIキー設定（Gitに含めないこと！）
├── requirements.txt         # 必要ライブラリ一覧
├── .gitignore               # Git除外設定
└── main.py                  # メインエントリーポイント
```

---

## スクリーニングモードの説明

### 急騰モード（short）
- 前日比 **+3%以上** の銘柄を検出
- **出来高が25日平均の2倍以上**であることが必須条件
- スコア = 前日比(%) × 出来高倍率で上位銘柄を抽出

### モメンタムモード（momentum）
- **5日・25日・75日移動平均が全て上昇中**
- 現在株価が **52週高値の90%以上**
- **RSI(14日)が50〜75**（買われすぎでも売られすぎでもない良好な範囲）

### 決算開示モード（earnings）
- EDINETから当日の **決算発表銘柄** を抽出
- 翌日の株価変動を追跡・記録

---

## 注意事項

- このツールは**投資の参考情報提供ツール**です。実際の投資判断は自己責任で行ってください。
- APIの利用規約を必ず確認してください。
- `config.yaml` にAPIキーを書いたら、GitHubなどにアップロードしないよう十分注意してください。

---

## トラブルシューティング

### `ModuleNotFoundError: No module named 'jquantsapi'`
→ `pip install -r requirements.txt` を実行してください

### `設定ファイルが見つかりません`
→ `config.yaml` が存在するか確認してください

### `認証エラー: J-Quantsのアクセストークン取得に失敗しました`
→ `config.yaml` のメールアドレスとパスワードを確認してください

### `EDINET APIエラー`
→ `config.yaml` の `edinet.api_key` を確認してください

---

## ライセンス

個人利用のみ。商用利用は各APIの利用規約に従ってください。
