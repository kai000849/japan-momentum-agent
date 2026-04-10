"""
agents/jquants_fetcher.py
J-Quants APIから日本株の株価・出来高データを取得するモジュール

【J-Quants V2 API 認証方式】
config.yaml の jquants.api_key を ClientV2 に渡して認証する（x-api-key ヘッダー方式）。

【レート制限対策】
get_eq_bars_daily_range() は内部で ThreadPoolExecutor（5並列）を使うため 429 が発生する。
このモジュールでは get_eq_bars_daily() を 1日ずつ逐次実行し、
リクエスト間に 1.5秒の待機を入れることで 429 を回避する。

【V2 → V1互換 列名変換について】
V2 の株価列名（O/H/L/C/Vo）は、このモジュール内で V1 互換の列名
（Open/High/Low/Close/Volume）に変換する。
scanner.py・backtester.py はこの変換後の列名を参照するため修正不要。

作者: Japan Momentum Agent
"""

import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import jquantsapi
import pandas as pd
import yaml

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# V2 列名 → V1 互換列名 の変換マップ
# ========================================

# V2 短縮列名 → 読みやすい列名（scanner.py 等の V1 互換名）
V2_COLUMN_RENAME = {
    "O":         "Open",
    "H":         "High",
    "L":         "Low",
    "C":         "Close",
    "Vo":        "Volume",
    "Va":        "TurnoverValue",
    "AdjO":      "AdjustmentOpen",
    "AdjH":      "AdjustmentHigh",
    "AdjL":      "AdjustmentLow",
    "AdjC":      "AdjustmentClose",
    "AdjVo":     "AdjustmentVolume",
    "AdjFactor": "AdjustmentFactor",
}

# equities/master (get_list) の V2列名 → V1互換列名
V2_MASTER_COLUMN_RENAME = {
    "CoName":  "CompanyName",
    "MktNm":   "MarketCodeName",
    "S17Nm":   "Sector17CodeName",
    "S33Nm":   "Sector33CodeName",
}


# ========================================
# 設定ファイルの読み込み
# ========================================

def load_config() -> dict:
    """
    config.yamlから設定を読み込む。

    Returns:
        dict: 設定内容（api_keyなど）

    Raises:
        FileNotFoundError: config.yamlが存在しない場合
    """
    config_path = Path(__file__).parent.parent / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"エラー: 設定ファイルが見つかりません: {config_path}\n"
            "config.yaml を作成し、jquants.api_key を設定してください。"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


def _get_client() -> jquantsapi.ClientV2:
    """
    J-Quants V2 APIクライアントを生成して返す（内部関数）。
    config.yaml から api_key を読み込み、ClientV2 を初期化する。

    Returns:
        jquantsapi.ClientV2: 認証済みAPIクライアント

    Raises:
        Exception: APIキーが未設定の場合
    """
    config = load_config()
    api_key = str(config.get("jquants", {}).get("api_key", "")).strip()

    if not api_key or api_key == "YOUR_JQUANTS_API_KEY":
        raise Exception(
            "エラー: config.yaml の jquants.api_key が設定されていません。\n"
            "J-QuantsのマイページからAPIキーを取得して設定してください。"
        )

    return jquantsapi.ClientV2(api_key=api_key)


# ========================================
# プランのデータ提供期間を検出
# ========================================

def _get_subscription_end_date() -> str:
    """
    プランのデータ提供終了日を検出して返す（内部関数）。
    今日の日付でAPIを呼び出し、400エラー中の範囲情報を解析する。
    検出できない場合はフォールバック値（2025-11-29）を返す。

    Returns:
        str: "YYYY-MM-DD" 形式の最終利用可能日
    """
    import re
    import requests as req

    config = load_config()
    api_key = str(config.get("jquants", {}).get("api_key", "")).strip()
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        # V2エンドポイントで今日の日付をリクエスト（範囲外→400でエラー内容を解析）
        resp = req.get(
            "https://api.jquants.com/v2/equities/bars/daily",
            headers={"x-api-key": api_key},
            params={"code": "72030", "from": today, "to": today},
            timeout=15
        )
        if resp.status_code == 200:
            logger.info(f"本日({today})のデータが取得可能です。")
            return today
        elif resp.status_code == 400:
            # エラーメッセージ例:
            # "Your subscription covers the following dates: 2023-11-29 ~ 2025-11-29."
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", resp.text)
            if m:
                start_date = m.group(1)
                end_date = m.group(2)
                logger.info(f"プランのデータ提供期間: {start_date} 〜 {end_date}")
                return end_date
    except Exception as e:
        logger.warning(f"提供終了日の検出中にエラー発生: {e}")

    # 自動検出に失敗した場合のフォールバック
    logger.warning("提供終了日の自動検出に失敗。フォールバック値(2025-11-29)を使用します。")
    return "2025-11-29"


# ========================================
# 銘柄一覧取得
# ========================================

def get_listed_stocks() -> pd.DataFrame:
    """
    J-Quants V2 から全上場銘柄の一覧を取得する。

    Returns:
        pd.DataFrame: 銘柄一覧
                      主な列: Code, CompanyName, MarketCodeName, Sector17CodeName 等

    Raises:
        Exception: API呼び出し失敗時
    """
    try:
        logger.info("J-Quants V2: 上場銘柄一覧を取得中...")
        client = _get_client()
        df = client.get_list()

        if df.empty:
            logger.warning("警告: 上場銘柄一覧が空でした。")
            return df

        # V2列名 → V1互換列名に変換
        df = df.rename(columns=V2_MASTER_COLUMN_RENAME)

        logger.info(f"J-Quants V2: {len(df)}銘柄の情報を取得しました。")
        return df

    except Exception as e:
        raise Exception(f"エラー: 上場銘柄一覧の取得に失敗しました。\n詳細: {e}")


def get_prime_stocks() -> pd.DataFrame:
    """
    プライム市場に上場している銘柄のみを取得する。
    スクリーニング対象をプライム市場に絞ることで、流動性の高い銘柄に集中する。

    Returns:
        pd.DataFrame: プライム市場銘柄一覧
    """
    df = get_listed_stocks()

    if df.empty:
        return df

    # MarketCodeName列（変換後）でプライム市場を絞り込む
    market_col = None
    for col in ["MarketCodeName", "MktNm", "Market"]:
        if col in df.columns:
            market_col = col
            break

    if market_col:
        prime_df = df[df[market_col].str.contains("プライム", na=False)].copy()
        logger.info(f"プライム市場銘柄数: {len(prime_df)}銘柄")
        return prime_df
    else:
        logger.warning(
            f"警告: 市場区分列が見つかりません。全銘柄を返します。\n"
            f"利用可能な列: {list(df.columns)}"
        )
        return df


# ========================================
# 日次株価データ取得（単一銘柄）
# ========================================

def get_daily_quotes(
    stock_code: str,
    from_date: str,
    to_date: str
) -> pd.DataFrame:
    """
    指定銘柄の日次株価（OHLCV）データを取得する。
    主にバックテスト時の単一銘柄参照に使用する。

    Args:
        stock_code (str): 銘柄コード（例: "7203" または "72030"）
        from_date (str): 取得開始日（形式: "YYYY-MM-DD"）
        to_date (str): 取得終了日（形式: "YYYY-MM-DD"）

    Returns:
        pd.DataFrame: OHLCV データ
                      列: Date, Code, Open, High, Low, Close, Volume 等
    """
    # まずローカルCSVから取得を試みる（API節約）
    local_df = load_latest_quotes()
    if not local_df.empty and "Code" in local_df.columns:
        stock_df = local_df[local_df["Code"].astype(str) == str(stock_code)].copy()

        # 日付範囲でフィルタ
        from_dt = pd.to_datetime(from_date)
        to_dt   = pd.to_datetime(to_date)
        if "Date" in stock_df.columns:
            stock_df = stock_df[
                (stock_df["Date"] >= from_dt) & (stock_df["Date"] <= to_dt)
            ]

        if not stock_df.empty:
            return stock_df.sort_values("Date").reset_index(drop=True)

    # ローカルデータがない場合はAPIから単一銘柄を直接取得
    logger.debug(f"銘柄 {stock_code}: APIから取得します...")
    try:
        client = _get_client()
        from_yyyymmdd = from_date.replace("-", "")
        to_yyyymmdd   = to_date.replace("-", "")
        df = client.get_eq_bars_daily(
            code=stock_code,
            from_yyyymmdd=from_yyyymmdd,
            to_yyyymmdd=to_yyyymmdd
        )
        if df.empty:
            return pd.DataFrame()

        # V2列名 → V1互換列名に変換
        df = df.rename(columns=V2_COLUMN_RENAME)

        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.sort_values("Date").reset_index(drop=True)

        return df

    except Exception as e:
        logger.warning(f"警告: 銘柄 {stock_code} の取得でエラー発生。スキップします。\n詳細: {e}")
        return pd.DataFrame()


# ========================================
# 全銘柄一括データ取得
# ========================================

def fetch_all_stocks_data(days: int = 60) -> pd.DataFrame:
    """
    プライム市場の全銘柄の過去N日分の株価データを一括取得し、CSVに保存する。

    【429レート制限対策】
    get_eq_bars_daily_range() は内部で5並列リクエストを送るため 429 が発生する。
    このメソッドは get_eq_bars_daily(date_yyyymmdd=date) を1日ずつ逐次実行し、
    リクエスト間に 20秒待機することで、レート制限を回避する。
    429 発生時はさらに 300秒（5分）待機してリトライする。

    プランのデータ提供終了日が今日より前の場合（無料プラン等）は、
    提供終了日を基準に過去N日分を自動取得する。

    Args:
        days (int): 取得する過去の日数（デフォルト: 60日）

    Returns:
        pd.DataFrame: プライム市場銘柄の株価データ（縦積み）
    """
    # リクエスト間の基本待機秒数（429対策。短すぎると429が頻発する）
    REQUEST_INTERVAL_SEC = 3
    # 429発生時の追加待機秒数
    RATE_LIMIT_WAIT_SEC = 300

    logger.info(f"全銘柄データ取得開始（過去{days}日分）")
    logger.info(
        f"  設定: リクエスト間隔={REQUEST_INTERVAL_SEC}秒, "
        f"429発生時待機={RATE_LIMIT_WAIT_SEC}秒"
    )

    # ---- 日付範囲の設定（プラン終了日の自動検出） ----
    today_str = datetime.now().strftime("%Y-%m-%d")
    subscription_end = _get_subscription_end_date()

    if subscription_end < today_str:
        to_date = subscription_end
        logger.info(
            f"プランのデータ提供終了日({to_date})を基準に取得します。\n"
            "  ※ 最新データを取得するにはJ-Quantsのプランアップグレードをご検討ください。"
        )
    else:
        to_date = today_str

    from_date = (
        datetime.strptime(to_date, "%Y-%m-%d") - timedelta(days=days)
    ).strftime("%Y-%m-%d")

    logger.info(f"取得期間: {from_date} 〜 {to_date}")

    # ---- 銘柄一覧を1回だけ取得してキャッシュ（API節約） ----
    logger.info("Step 1/3: プライム市場銘柄一覧を取得中（1回のみ）...")
    all_stocks = get_listed_stocks()  # get_list() を1回だけ呼ぶ

    if all_stocks.empty:
        logger.error("エラー: 銘柄一覧の取得に失敗しました。")
        return pd.DataFrame()

    # プライム市場銘柄でフィルタ（キャッシュ済みデータを使用）
    market_col = next(
        (c for c in ["MarketCodeName", "Market"] if c in all_stocks.columns),
        None
    )
    if market_col:
        prime_df = all_stocks[all_stocks[market_col].str.contains("プライム", na=False)]
        prime_codes = set(prime_df["Code"].astype(str).tolist())
        logger.info(f"プライム市場銘柄数: {len(prime_codes)}銘柄")
    else:
        prime_codes = set(all_stocks["Code"].astype(str).tolist())
        logger.warning("市場区分列が見つかりません。全銘柄を対象とします。")

    # ---- 営業日リストを生成（土日を除く） ----
    trading_dates = pd.bdate_range(start=from_date, end=to_date)
    total_days = len(trading_dates)
    estimated_minutes = (total_days * REQUEST_INTERVAL_SEC) // 60 + 1
    logger.info(f"取得対象日数: {total_days}営業日（推定所要時間: 約{estimated_minutes}分）")

    # ---- 日付ごとに逐次取得（長めの待機でレート制限を回避） ----
    logger.info("Step 2/3: 日付ごとに株価データを逐次取得中...")
    logger.info(f"  ※ 並列リクエスト禁止・{REQUEST_INTERVAL_SEC}秒間隔で送信します。")

    client = _get_client()
    all_dfs = []
    consecutive_errors = 0

    for i, trade_date in enumerate(trading_dates):
        date_str = trade_date.strftime("%Y-%m-%d")
        logger.info(f"  [{i + 1}/{total_days}] {date_str} 取得中...")

        # 429発生時のリトライ付き取得
        success = False
        for attempt in range(2):  # 最大2回試行
            try:
                df_day = client.get_eq_bars_daily(date_yyyymmdd=date_str)

                if not df_day.empty:
                    # V2列名 → V1互換列名に変換
                    df_day = df_day.rename(columns=V2_COLUMN_RENAME)
                    all_dfs.append(df_day)
                    logger.info(f"    -> {len(df_day)}件取得")
                else:
                    logger.info(f"    -> データなし（休場日の可能性）")

                consecutive_errors = 0  # 成功したらエラーカウントリセット
                success = True
                break

            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    if attempt == 0:
                        logger.warning(
                            f"    429 レート制限検出。{RATE_LIMIT_WAIT_SEC}秒（{RATE_LIMIT_WAIT_SEC//60}分）待機してリトライします..."
                        )
                        time.sleep(RATE_LIMIT_WAIT_SEC)
                    else:
                        logger.warning(f"    リトライ後も429。この日付はスキップします。")
                        consecutive_errors += 1
                else:
                    logger.warning(f"    -> スキップ: {e}")
                    consecutive_errors += 1
                    break

        # 連続エラーが3回以上続いたら中断（取得済みデータを保存）
        if consecutive_errors >= 3:
            logger.error(
                f"エラーが{consecutive_errors}回連続して発生したため取得を中断します。\n"
                "  確認事項:\n"
                "  1. 本日の API 利用枠が上限に達した可能性があります。\n"
                "     → 翌日（日本時間0時リセット）に再試行してください。\n"
                "  2. config.yaml の jquants.api_key が正しいか確認してください。\n"
                "  取得済みデータは保存します。"
            )
            break

        # 最後のリクエスト以外はインターバル待機（レート制限対策）
        if i < total_days - 1:
            time.sleep(REQUEST_INTERVAL_SEC)

    if not all_dfs:
        logger.error("エラー: 株価データが1件も取得できませんでした。")
        return pd.DataFrame()

    # ---- データを結合 ----
    df = pd.concat(all_dfs, ignore_index=True)
    logger.info(f"取得完了: {len(df)}レコード（全市場, {len(all_dfs)}日分）")

    # 日付列を datetime 型に変換
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"])

    # ---- プライム市場銘柄のみ残す ----
    logger.info("Step 3/3: プライム市場銘柄でフィルタ中...")
    if "Code" in df.columns:
        before = len(df)
        df = df[df["Code"].astype(str).isin(prime_codes)].reset_index(drop=True)
        logger.info(f"フィルタ後: {len(df)}レコード（除外: {before - len(df)}レコード）")

    # ---- CSV保存 ----
    save_path = _save_quotes_csv(df)
    logger.info(f"データを保存しました: {save_path}")
    logger.info(f"取得銘柄数: {df['Code'].nunique() if 'Code' in df.columns else '不明'}銘柄")

    return df


def _save_quotes_csv(df: pd.DataFrame) -> Path:
    """
    株価データをCSVファイルに保存する（内部関数）。

    Args:
        df (pd.DataFrame): 保存する株価データ

    Returns:
        Path: 保存したファイルのパス
    """
    save_dir = Path(__file__).parent.parent / "data" / "raw" / "jquants"
    save_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    save_path = save_dir / f"quotes_{today}.csv"

    # UTF-8 BOM付きで保存（Excelで開いても文字化けしない）
    df.to_csv(save_path, index=False, encoding="utf-8-sig")

    return save_path


def load_latest_quotes() -> pd.DataFrame:
    """
    最新のCSVファイルからローカルに保存済みの株価データを読み込む。

    Returns:
        pd.DataFrame: 株価データ。ファイルが存在しない場合は空のDataFrame。
    """
    save_dir = Path(__file__).parent.parent / "data" / "raw" / "jquants"

    if not save_dir.exists():
        logger.warning("警告: データディレクトリが存在しません。先にfetch_all_stocks_data()を実行してください。")
        return pd.DataFrame()

    csv_files = sorted(save_dir.glob("quotes_*.csv"), reverse=True)

    if not csv_files:
        logger.warning("警告: 株価CSVファイルが見つかりません。先にfetch_all_stocks_data()を実行してください。")
        return pd.DataFrame()

    latest_file = csv_files[0]
    logger.info(f"株価データを読み込み中: {latest_file}")

    try:
        df = pd.read_csv(latest_file, encoding="utf-8-sig")

        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])

        logger.info(f"読み込み完了: {len(df)}レコード")
        return df

    except Exception as e:
        logger.error(f"エラー: CSVファイルの読み込みに失敗しました。\n詳細: {e}")
        return pd.DataFrame()


# ========================================
# メイン（単体テスト用）
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のテスト処理
    使い方: python agents/jquants_fetcher.py
    """
    print("=" * 50)
    print("J-Quants Fetcher V2 - 動作テスト")
    print("=" * 50)

    try:
        # クライアント生成テスト
        print("\n[テスト1] APIクライアント生成...")
        client = _get_client()
        print("  生成成功")

        # 銘柄一覧取得テスト
        print("\n[テスト2] 上場銘柄一覧取得...")
        stocks = get_listed_stocks()
        print(f"  取得成功: {len(stocks)}銘柄")

        # プライム市場フィルタテスト
        print("\n[テスト3] プライム市場銘柄フィルタ...")
        prime = get_prime_stocks()
        print(f"  プライム銘柄: {len(prime)}銘柄")

        # 1日分のデータ取得テスト（レート制限を確認）
        print("\n[テスト4] 株価データ取得テスト（1日分: 2025-11-28）...")
        df_test = client.get_eq_bars_daily(date_yyyymmdd="2025-11-28")
        df_test = df_test.rename(columns=V2_COLUMN_RENAME)
        print(f"  取得成功: {len(df_test)}件")
        if not df_test.empty:
            print(f"  列名: {list(df_test.columns[:8])}")

        print("\n全テスト完了！J-Quants V2 APIは正常に動作しています。")

    except Exception as e:
        print(f"\nエラーが発生しました: {e}")


# ========================================
# 決算速報データ取得（/fins/summary）
# ========================================

def get_todays_earnings(date_str: str) -> pd.DataFrame:
    """
    J-Quants /fins/summary から指定日の決算速報データを取得する。

    J-Quantsは当日18:00頃に速報（1Q/2Q/3Q/通期）を配信する。
    EDINETより大幅に早く、構造化データとして取得できる。

    Args:
        date_str (str): "YYYY-MM-DD" 形式の日付

    Returns:
        pd.DataFrame: 決算速報データ。空の場合は空DataFrame。
                      主要列: Code, DiscDate, DiscTime, DocType, CurPerType,
                              Sales, OP, NP, NCSales, NCOP, NCNP, FSales, FOP
    """
    try:
        client = _get_client()
        date_compact = date_str.replace("-", "")
        df = client.get_fin_summary(date_yyyymmdd=date_compact)
        if df is None or df.empty:
            logger.info(f"決算速報: {date_str} のデータなし（決算発表なし）")
            return pd.DataFrame()
        logger.info(f"決算速報取得完了: {date_str} → {len(df)}件")
        return df
    except Exception as e:
        logger.warning(f"決算速報取得エラー ({date_str}): {e}")
        return pd.DataFrame()
        print("\nconfig.yaml の jquants.api_key を確認してください。")
