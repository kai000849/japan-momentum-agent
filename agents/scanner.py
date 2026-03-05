"""
agents/scanner.py
日本株スクリーニングモジュール

3つのスクリーニングモードを提供する：
1. SHORT_TERM（急騰モード）: 前日比+3%以上 かつ 出来高急増
2. MOMENTUM（モメンタムモード）: 移動平均上昇 + 52週高値付近 + RSI良好
3. EARNINGS（決算開示モード）: 当日の決算発表銘柄を追跡

作者: Japan Momentum Agent
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ta（テクニカル分析ライブラリ）からRSI計算器をインポート
try:
    import ta
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False
    logging.warning("警告: taライブラリが見つかりません。pip install ta でインストールしてください。")

# 同プロジェクト内のモジュールをインポート
from agents.jquants_fetcher import load_latest_quotes, fetch_all_stocks_data, get_listed_stocks
from agents.edinet_fetcher import get_earnings_announcements, save_disclosure_log

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# スクリーニングモード定数
# ========================================

MODE_SHORT_TERM = "SHORT_TERM"   # 急騰モード
MODE_MOMENTUM = "MOMENTUM"       # モメンタムモード
MODE_EARNINGS = "EARNINGS"       # 決算開示モード

VALID_MODES = [MODE_SHORT_TERM, MODE_MOMENTUM, MODE_EARNINGS]


# ========================================
# テクニカル指標計算
# ========================================

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI（相対力指数）を計算する。
    RSIは0〜100の範囲で、70以上が買われすぎ、30以下が売られすぎを示す。

    Args:
        prices (pd.Series): 終値の時系列データ
        period (int): RSIの計算期間（デフォルト: 14日）

    Returns:
        pd.Series: RSI値の時系列
    """
    if TA_AVAILABLE:
        # taライブラリを使用（推奨）
        return ta.momentum.RSIIndicator(prices, window=period).rsi()
    else:
        # taライブラリがない場合は手動計算
        delta = prices.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, float('inf'))
        rsi = 100 - (100 / (1 + rs))
        return rsi


def calculate_moving_averages(prices: pd.Series) -> dict:
    """
    5日・25日・75日移動平均を計算する。

    Args:
        prices (pd.Series): 終値の時系列データ

    Returns:
        dict: 各移動平均の最新値と前日値
              例: {"ma5": {"current": 1500, "prev": 1490}, ...}
    """
    result = {}

    for period in [5, 25, 75]:
        # 移動平均を計算（データが足りない場合はNaN）
        ma = prices.rolling(window=period, min_periods=period).mean()

        if len(ma.dropna()) >= 2:
            result[f"ma{period}"] = {
                "current": float(ma.iloc[-1]),    # 最新の移動平均
                "prev": float(ma.iloc[-2]),         # 前日の移動平均
                "rising": ma.iloc[-1] > ma.iloc[-2]  # 上昇中かどうか
            }
        else:
            result[f"ma{period}"] = {
                "current": None,
                "prev": None,
                "rising": False
            }

    return result


# ========================================
# スクリーニングロジック本体
# ========================================

def _scan_short_term(df_all: pd.DataFrame, scan_date: str) -> list:
    """
    急騰モードのスクリーニングを実行する。

    条件:
    - 前日比 +3% 以上
    - 当日出来高 >= 25日平均出来高 × 2.0倍
    - 急騰スコア = 前日比(%) × 出来高倍率

    Args:
        df_all (pd.DataFrame): 全銘柄の株価データ
        scan_date (str): スキャン日付（YYYY-MM-DD）

    Returns:
        list: スクリーニング結果のリスト（スコア降順でソート済み）
    """
    logger.info("急騰モードでスクリーニング中...")
    results = []

    # 日付でフィルタリング
    scan_dt = pd.to_datetime(scan_date)

    # 銘柄コードごとに処理
    code_column = _detect_code_column(df_all)
    grouped = df_all.groupby(code_column)

    for stock_code, group in grouped:
        # 日付でソート
        group = group.sort_values("Date").reset_index(drop=True)

        # 対象日のデータが存在するか確認
        target_mask = group["Date"] == scan_dt
        if not target_mask.any():
            continue

        # 最低26日分のデータが必要（25日平均計算のため）
        if len(group) < 26:
            continue

        try:
            # 最新行（スキャン日）と前日行を取得
            target_idx = group[target_mask].index[-1]
            if target_idx == 0:  # 前日データがない場合はスキップ
                continue

            today_row = group.loc[target_idx]
            prev_row = group.loc[target_idx - 1]

            # 終値を取得
            today_close = float(today_row.get("Close", 0) or 0)
            prev_close = float(prev_row.get("Close", 0) or 0)

            if prev_close <= 0 or today_close <= 0:
                continue

            # 前日比計算（%）
            price_change_pct = ((today_close - prev_close) / prev_close) * 100

            # 条件1: 前日比 +3% 以上
            if price_change_pct < 3.0:
                continue

            # 出来高を取得
            today_volume = float(today_row.get("Volume", 0) or 0)

            # 25日平均出来高計算（スキャン日を含まない直近25日）
            past_volumes = group.loc[:target_idx - 1, "Volume"].tail(25).astype(float)
            avg_volume_25d = past_volumes.mean()

            if avg_volume_25d <= 0:
                continue

            # 出来高倍率
            volume_ratio = today_volume / avg_volume_25d

            # 条件2: 出来高が25日平均の2倍以上
            if volume_ratio < 2.0:
                continue

            # 急騰スコア計算
            surge_score = price_change_pct * volume_ratio

            # 銘柄名を取得（あれば）
            company_name = str(today_row.get("CompanyName", "")) or ""

            results.append({
                "stockCode": str(stock_code),
                "companyName": company_name,
                "mode": MODE_SHORT_TERM,
                "scanDate": scan_date,
                "close": today_close,
                "prevClose": prev_close,
                "priceChangePct": round(price_change_pct, 2),   # 前日比（%）
                "volume": int(today_volume),
                "avgVolume25d": round(avg_volume_25d, 0),
                "volumeRatio": round(volume_ratio, 2),           # 出来高倍率
                "score": round(surge_score, 2),                  # 急騰スコア
            })

        except Exception as e:
            logger.debug(f"銘柄 {stock_code} のスキャン中にエラー（スキップ）: {e}")
            continue

    # スコア降順でソート
    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"急騰モード: {len(results)}銘柄がヒットしました。")
    return results


def _scan_momentum(df_all: pd.DataFrame, scan_date: str) -> list:
    """
    モメンタムモードのスクリーニングを実行する。

    条件:
    - 5日・25日・75日移動平均が全て前日より上昇
    - 現在株価が52週高値の90%以上
    - RSI(14日)が50〜75の範囲

    Args:
        df_all (pd.DataFrame): 全銘柄の株価データ
        scan_date (str): スキャン日付（YYYY-MM-DD）

    Returns:
        list: スクリーニング結果のリスト（スコア降順でソート済み）
    """
    logger.info("モメンタムモードでスクリーニング中...")
    results = []

    scan_dt = pd.to_datetime(scan_date)
    code_column = _detect_code_column(df_all)
    grouped = df_all.groupby(code_column)

    for stock_code, group in grouped:
        group = group.sort_values("Date").reset_index(drop=True)

        # 対象日のデータが存在するか確認
        target_mask = group["Date"] == scan_dt
        if not target_mask.any():
            continue

        # 最低26日分のデータが必要（25日移動平均のため）
        if len(group) < 26:
            continue

        try:
            # スキャン日のインデックスを取得
            target_idx = group[target_mask].index[-1]
            if target_idx < 25:  # 25日MAに最低26行必要
                continue

            # スキャン日時点のデータをスライス
            group_to_date = group.loc[:target_idx].copy()
            closes = group_to_date["Close"].astype(float)

            # 現在株価
            current_close = float(closes.iloc[-1])
            if current_close <= 0:
                continue

            # ---- 条件1: 移動平均が全て上昇中 ----
            # データ量に応じて使用できる移動平均を柔軟に判断する
            # - 76日以上あれば: 5日・25日・75日MA全て確認
            # - 26日以上75日未満: 5日・25日MAのみ確認（75日MAはスキップ）
            ma_data = calculate_moving_averages(closes)

            has_ma75 = (
                ma_data["ma75"]["current"] is not None
                and target_idx >= 75
            )

            # ---- 条件1: 75日MAが必須 ----
            # データが75日分未満の銘柄はスキップ（条件を緩めない）
            if not has_ma75:
                continue  # 75日MAが計算できない銘柄は除外

            # 5日・25日・75日MA全て上昇中であることを確認
            all_ma_rising = all([
                ma_data["ma5"]["rising"],
                ma_data["ma25"]["rising"],
                ma_data["ma75"]["rising"]
            ])

            if not all_ma_rising:
                continue

            # ---- 条件2: 現在株価が52週高値の95%以上 ----
            price_52w_window = closes.tail(252)
            high_52w = float(price_52w_window.max())

            if high_52w <= 0:
                continue

            price_to_high_ratio = (current_close / high_52w) * 100  # 52週高値比（%）

            if price_to_high_ratio < 95.0:
                continue

            # ---- 条件3: RSI(14日)が55〜70の範囲 ----
            rsi_series = calculate_rsi(closes, period=14)
            current_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

            if current_rsi is None or not (55 <= current_rsi <= 70):
                continue

            # ---- 追加指標1: 新高値更新スコア ----
            # 直近20日間で何回52週高値を更新したか（0〜20）
            # 「高値更新が続いている」銘柄を高く評価する
            recent_closes = closes.tail(20)
            new_high_count = 0
            running_high = float(closes.iloc[-(21)] if len(closes) > 20 else closes.iloc[0])
            for price in recent_closes:
                if price > running_high:
                    new_high_count += 1
                    running_high = price
            # 正規化: 0〜1.0（20日で20回更新 = 1.0）
            new_high_score = new_high_count / 20.0

            # ---- 追加指標2: 出来高増加トレンド ----
            # 直近5日平均出来高 ÷ 直近25日平均出来高
            # 1.0より大きければ出来高が増加トレンド
            volumes = group_to_date["Volume"].astype(float)
            avg_vol_5d = float(volumes.tail(5).mean())
            avg_vol_25d = float(volumes.tail(25).mean())
            volume_trend = (avg_vol_5d / avg_vol_25d) if avg_vol_25d > 0 else 1.0
            # 1.5倍以上は上限キャップ（スコアが支配的にならないよう）
            volume_trend = min(volume_trend, 1.5)

            # ---- モメンタムスコア計算（中長期向け強化版）----
            # 基本スコア: RSI × 52週高値比
            base_score = current_rsi * (price_to_high_ratio / 100)
            # ボーナス乗数:
            #   全MA上昇中       → ×1.2
            #   新高値更新あり   → ×(1.0 + new_high_score × 0.3)  最大×1.3
            #   出来高増加トレンド → ×(0.85 + volume_trend × 0.1)  最大×1.0
            momentum_score = (
                base_score
                * (1.2 if all_ma_rising else 1.0)
                * (1.0 + new_high_score * 0.3)
                * (0.85 + volume_trend * 0.1)
            )

            company_name = str(group.loc[target_idx].get("CompanyName", "")) or ""

            # ma75はデータ不足の場合 None になることがある
            ma75_val = ma_data["ma75"]["current"]
            results.append({
                "stockCode": str(stock_code),
                "companyName": company_name,
                "mode": MODE_MOMENTUM,
                "scanDate": scan_date,
                "close": current_close,
                "high52w": high_52w,
                "priceToHighRatio": round(price_to_high_ratio, 2),   # 52週高値比（%）
                "rsi14": round(current_rsi, 2),
                "ma5": round(ma_data["ma5"]["current"], 2),
                "ma25": round(ma_data["ma25"]["current"], 2),
                "ma75": round(ma75_val, 2) if ma75_val is not None else None,
                "newHighCount": new_high_count,                       # 直近20日の新高値更新回数
                "volumeTrend": round(volume_trend, 2),                # 出来高トレンド比（5日/25日）
                "score": round(momentum_score, 2),
            })

        except Exception as e:
            logger.debug(f"銘柄 {stock_code} のモメンタムスキャン中にエラー（スキップ）: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"モメンタムモード: {len(results)}銘柄がヒットしました。")
    return results


def _scan_earnings(scan_date: str) -> list:
    """
    決算開示モードのスクリーニングを実行する。
    当日のEDINET開示書類から決算発表銘柄を抽出し、翌日の値動き追跡のために記録する。

    Args:
        scan_date (str): スキャン日付（YYYY-MM-DD）

    Returns:
        list: 決算発表銘柄のリスト
    """
    logger.info(f"決算開示モードでスクリーニング中（{scan_date}）...")

    # EDINETから決算発表銘柄を取得
    earnings = get_earnings_announcements(scan_date)

    if not earnings:
        logger.warning(f"{scan_date}の決算発表銘柄が見つかりませんでした。")
        return []

    # ---- 株価データを読み込んで銘柄コードと紐付けるための辞書を作成 ----
    price_map = {}  # {銘柄コード文字列: 最新終値}
    try:
        df_all = load_latest_quotes()
        if not df_all.empty:
            code_col = _detect_code_column(df_all)
            # 各銘柄の最新終値を取得
            latest = (
                df_all.sort_values("Date")
                .groupby(code_col)
                .last()
                .reset_index()
            )
            for _, row in latest.iterrows():
                code = str(row[code_col])
                close = float(row.get("Close", 0) or 0)
                price_map[code] = close
                # 5桁・4桁の両方で登録（コード形式のゆらぎ対応）
                if len(code) == 5 and code.endswith("0"):
                    price_map[code[:4]] = close
                elif len(code) == 4:
                    price_map[code + "0"] = close
        logger.info(f"株価マップ作成完了: {len(price_map)}銘柄")
    except Exception as e:
        logger.warning(f"株価データの読み込みに失敗しました（株価なしで継続）: {e}")

    # モードとスコアを追加
    results = []
    for e in earnings:
        sec_code = str(e.get("secCode", ""))

        # 株価を取得（4桁・5桁どちらでも検索）
        close = price_map.get(sec_code) or price_map.get(sec_code[:4]) or 0.0

        results.append({
            "stockCode": sec_code,
            "companyName": e.get("companyName", ""),
            "mode": MODE_EARNINGS,
            "scanDate": scan_date,
            "close": close,                              # 最新終値（追加）
            "docTypeCode": e.get("docTypeCode", ""),
            "docDescription": e.get("docDescription", ""),
            "submitDateTime": e.get("submitDateTime", ""),
            "score": 1.0,  # 決算スキャンは全銘柄スコア同一
        })

    # 開示ログに保存（翌日の株価確認のため）
    save_disclosure_log(earnings, scan_date)

    logger.info(f"決算開示モード: {len(results)}銘柄を記録しました。")
    return results


def _detect_code_column(df: pd.DataFrame) -> str:
    """
    DataFrameから銘柄コード列を自動検出する（内部関数）。

    Args:
        df (pd.DataFrame): 株価DataFrame

    Returns:
        str: 銘柄コード列名
    """
    # 候補列名を順に試す
    for col in ["Code", "code", "StockCode", "stock_code"]:
        if col in df.columns:
            return col
    raise ValueError(
        "エラー: DataFrameに銘柄コード列（Code, code等）が見つかりません。\n"
        f"利用可能な列: {list(df.columns)}"
    )


# ========================================
# スキャン実行・結果保存
# ========================================

def run_scan(mode: str, date: str = None) -> list:
    """
    指定モードでスクリーニングを実行する。

    Args:
        mode (str): スクリーニングモード
                    "SHORT_TERM" / "MOMENTUM" / "EARNINGS"
        date (str): スキャン日付（YYYY-MM-DD）。
                    Noneの場合はCSVの最終日付を自動使用（データと日付のずれを防ぐ）

    Returns:
        list: スクリーニング結果のリスト（スコア降順ソート済み）

    Raises:
        ValueError: 無効なモードが指定された場合
    """
    # モードの検証
    mode = mode.upper()
    if mode not in VALID_MODES:
        raise ValueError(
            f"エラー: 無効なスキャンモード '{mode}' が指定されました。\n"
            f"有効なモード: {', '.join(VALID_MODES)}"
        )

    # 決算開示モードはEDINETのみ使用（株価データ不要）
    if mode == MODE_EARNINGS:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"スキャン開始: モード={mode}, 日付={date}")
        results = _scan_earnings(date)
    else:
        # 急騰・モメンタムモードは株価データが必要
        logger.info("株価データを読み込み中...")
        df_all = load_latest_quotes()

        if df_all.empty:
            logger.warning(
                "警告: 株価データが見つかりません。\n"
                "fetch_all_stocks_data() を先に実行してデータを取得してください。"
            )
            return []

        logger.info(f"株価データ読み込み完了: {df_all['Code'].nunique() if 'Code' in df_all.columns else '?'}銘柄")

        # 銘柄マスターから銘柄名を結合する
        try:
            master_df = get_listed_stocks()
            if not master_df.empty and "Code" in master_df.columns and "CompanyName" in master_df.columns:
                # マスターのCodeを文字列化（4桁 or 5桁どちらでも対応）
                master_df["Code_str"] = master_df["Code"].astype(str)
                # 5桁で末尾0の場合は4桁に変換してマップ作成
                name_map = {}
                for _, row in master_df.iterrows():
                    c = row["Code_str"]
                    name_map[c] = row["CompanyName"]
                    # 4桁でも登録
                    if len(c) == 5 and c.endswith("0"):
                        name_map[c[:4]] = row["CompanyName"]
                    elif len(c) == 4:
                        name_map[c + "0"] = row["CompanyName"]

                def lookup_name(code):
                    c = str(code)
                    return name_map.get(c) or name_map.get(c[:4]) or name_map.get(c + "0") or ""

                df_all["CompanyName"] = df_all["Code"].apply(lookup_name)
                logger.info("銘柄名の付加完了")
        except Exception as e:
            logger.warning(f"銘柄名の取得に失敗しました（スキップ）: {e}")

        # 日付が未指定の場合はCSVの最終日付を自動使用
        # （今日の日付を使うとデータが存在しないため全銘柄スキップされる）
        if date is None:
            if "Date" in df_all.columns:
                last_date = df_all["Date"].max()
                date = last_date.strftime("%Y-%m-%d")
                logger.info(
                    f"スキャン日付を自動設定: {date}（CSVデータの最終日）\n"
                    "  ※ --date YYYY-MM-DD で別の日付を指定することもできます。"
                )
            else:
                date = datetime.now().strftime("%Y-%m-%d")

        logger.info(f"スキャン開始: モード={mode}, 日付={date}")

        if mode == MODE_SHORT_TERM:
            results = _scan_short_term(df_all, date)
        elif mode == MODE_MOMENTUM:
            results = _scan_momentum(df_all, date)

    # 結果をJSONに保存
    if results:
        _save_scan_results(results, date, mode)

    return results


def _save_scan_results(results: list, date: str, mode: str) -> Path:
    """
    スキャン結果をJSONファイルに保存する（内部関数）。

    Args:
        results (list): スキャン結果
        date (str): スキャン日付
        mode (str): スキャンモード

    Returns:
        Path: 保存ファイルパス
    """
    save_dir = Path(__file__).parent.parent / "data" / "processed" / "scans"
    save_dir.mkdir(parents=True, exist_ok=True)

    # ファイル名: scan_YYYYMMDD_MODE.json
    date_str = date.replace("-", "")
    file_name = f"scan_{date_str}_{mode}.json"
    save_path = save_dir / file_name

    output = {
        "scanDate": date,
        "mode": mode,
        "resultCount": len(results),
        "generatedAt": datetime.now().isoformat(),
        "results": results
    }

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"スキャン結果を保存しました: {save_path}")
    return save_path


# ========================================
# 結果表示
# ========================================

def display_top_results(results: list, top_n: int = 10) -> None:
    """
    スクリーニング結果の上位N銘柄をコンソールに表示する。

    Args:
        results (list): スキャン結果リスト（スコア降順ソート済みを想定）
        top_n (int): 表示する銘柄数（デフォルト: 10）
    """
    if not results:
        print("\n【スキャン結果】ヒット銘柄なし")
        return

    # 上位N件を取得
    top_results = results[:top_n]
    mode = top_results[0].get("mode", "UNKNOWN")
    scan_date = top_results[0].get("scanDate", "")

    print(f"\n{'='*65}")
    print(f"  スキャン結果 [{mode}] - {scan_date}")
    print(f"  ヒット銘柄数: {len(results)}銘柄 (上位{top_n}件を表示)")
    print(f"{'='*65}")

    if mode == MODE_SHORT_TERM:
        # 急騰モードの表示
        print(f"{'順位':>4} {'銘柄コード':>8} {'会社名':<20} {'前日比':>7} {'出来高倍':>7} {'スコア':>8}")
        print("-" * 65)
        for i, r in enumerate(top_results, 1):
            company = r.get("companyName", "")[:18] or "N/A"
            print(
                f"{i:>4} "
                f"{r.get('stockCode', 'N/A'):>8} "
                f"{company:<20} "
                f"{r.get('priceChangePct', 0):>+6.2f}% "
                f"{r.get('volumeRatio', 0):>6.1f}x "
                f"{r.get('score', 0):>8.2f}"
            )

    elif mode == MODE_MOMENTUM:
        # モメンタムモードの表示
        print(f"{'順位':>4} {'銘柄コード':>8} {'会社名':<20} {'RSI':>6} {'高値比':>7} {'スコア':>8}")
        print("-" * 65)
        for i, r in enumerate(top_results, 1):
            company = r.get("companyName", "")[:18] or "N/A"
            print(
                f"{i:>4} "
                f"{r.get('stockCode', 'N/A'):>8} "
                f"{company:<20} "
                f"{r.get('rsi14', 0):>5.1f} "
                f"{r.get('priceToHighRatio', 0):>6.1f}% "
                f"{r.get('score', 0):>8.2f}"
            )

    elif mode == MODE_EARNINGS:
        # 決算開示モードの表示
        print(f"{'順位':>4} {'銘柄コード':>8} {'会社名':<25} {'提出書類':<20}")
        print("-" * 65)
        for i, r in enumerate(top_results, 1):
            company = r.get("companyName", "")[:23] or "N/A"
            doc_desc = r.get("docDescription", "")[:18] or "N/A"
            print(
                f"{i:>4} "
                f"{r.get('stockCode', 'N/A'):>8} "
                f"{company:<25} "
                f"{doc_desc:<20}"
            )

    print(f"{'='*65}")


# ========================================
# メイン（単体テスト用）
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のテスト処理
    使い方: python agents/scanner.py
    """
    import sys

    print("=" * 60)
    print("Scanner - 動作テスト")
    print("=" * 60)

    today = datetime.now().strftime("%Y-%m-%d")

    # コマンドライン引数でモードを指定可能
    test_mode = sys.argv[1].upper() if len(sys.argv) > 1 else MODE_SHORT_TERM

    print(f"\nテストモード: {test_mode}")
    print(f"スキャン日付: {today}")

    try:
        results = run_scan(test_mode, today)
        display_top_results(results, top_n=10)
        print(f"\n✓ スキャン完了: {len(results)}銘柄がヒットしました。")

    except Exception as e:
        print(f"\n✗ エラーが発生しました: {e}")
        print("\n株価データがない場合は、先に以下を実行してください:")
        print("  python agents/jquants_fetcher.py")