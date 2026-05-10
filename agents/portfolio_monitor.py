"""
agents/portfolio_monitor.py
PFモメンタム点検くん - 保有ポジションのモメンタム継続チェック

実売買ポジション（tradeType="actual"）ごとに、
RSI・パーフェクトオーダー・52週高値比・売買代金トレンド・含み損益を算出し
モメンタム継続度（🟢継続/🟡要注意/🔴喪失）を判定する。
"""

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ========================================
# RSI 計算
# ========================================

def _calc_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    try:
        from ta.momentum import RSIIndicator
        rsi_series = RSIIndicator(closes, window=period).rsi()
        val = rsi_series.dropna()
        return float(val.iloc[-1]) if not val.empty else None
    except Exception:
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, float("inf"))
        rsi = (100 - (100 / (1 + rs))).dropna()
        return float(rsi.iloc[-1]) if not rsi.empty else None


# ========================================
# 営業日カウント
# ========================================

def _business_days_since(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        today = date.today()
        count = 0
        d = entry
        while d < today:
            d += timedelta(days=1)
            if d.weekday() < 5:
                count += 1
        return count
    except Exception:
        return 0


# ========================================
# ポジション1件の指標を計算
# ========================================

def _analyze_position(pos: dict, df_all: pd.DataFrame) -> dict:
    stock_code = pos.get("stockCode", "")
    entry_price = float(pos.get("entryPrice", 0))
    shares = int(pos.get("shares", 0))
    entry_date = pos.get("entryDate", "")
    hold_type = pos.get("holdType", "cash")
    company_name = pos.get("companyName", "")

    result = {
        "stockCode": stock_code,
        "companyName": company_name,
        "holdType": hold_type,
        "entryPrice": entry_price,
        "shares": shares,
        "entryDate": entry_date,
        "holdDays": _business_days_since(entry_date),
        "currentClose": None,
        "positionSize": None,
        "unrealizedPnlPct": None,
        "unrealizedPnlYen": None,
        "rsi14": None,
        "perfectOrder": None,
        "ma5": None,
        "ma25": None,
        "ma75": None,
        "high52wRatio": None,
        "turnoverTrendRatio": None,
        "momentumStatus": "🔘不明",
        "warnings": [],
    }

    if df_all.empty:
        return result

    # 銘柄データを抽出（4桁・5桁コードゆれ対応）
    code_col = None
    for c in ["Code", "code", "StockCode"]:
        if c in df_all.columns:
            code_col = c
            break
    if code_col is None:
        return result

    stock_df = df_all[df_all[code_col].astype(str).str[:4] == str(stock_code)[:4]].copy()
    if stock_df.empty:
        return result

    stock_df = stock_df.sort_values("Date").reset_index(drop=True)

    # 最新終値
    close_col = "Close" if "Close" in stock_df.columns else None
    if close_col is None:
        return result

    closes = stock_df[close_col].astype(float)
    if closes.empty:
        return result

    current_close = float(closes.iloc[-1])
    result["currentClose"] = current_close
    result["positionSize"] = round(current_close * shares)
    result["unrealizedPnlYen"] = round((current_close - entry_price) * shares)
    result["unrealizedPnlPct"] = round((current_close - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0

    # RSI
    result["rsi14"] = round(_calc_rsi(closes), 1) if _calc_rsi(closes) is not None else None

    # 移動平均
    for period in [5, 25, 75]:
        ma = closes.rolling(window=period, min_periods=period).mean()
        val = ma.dropna()
        result[f"ma{period}"] = round(float(val.iloc[-1]), 1) if not val.empty else None

    ma5 = result["ma5"]
    ma25 = result["ma25"]
    ma75 = result["ma75"]
    result["perfectOrder"] = (
        ma5 is not None and ma25 is not None and ma75 is not None
        and ma5 > ma25 > ma75
    )

    # 52週高値比
    rows_252 = stock_df.tail(252)
    if "High" in stock_df.columns:
        high_52w = float(rows_252["High"].astype(float).max())
    else:
        high_52w = float(rows_252[close_col].astype(float).max())
    result["high52wRatio"] = round(current_close / high_52w * 100, 1) if high_52w > 0 else None

    # 売買代金トレンド（直近5日平均 / 25日平均）
    if "TurnoverValue" in stock_df.columns:
        turnovers = stock_df["TurnoverValue"].astype(float)
        avg5 = float(turnovers.tail(5).mean()) if len(turnovers) >= 5 else None
        avg25 = float(turnovers.tail(25).mean()) if len(turnovers) >= 25 else None
        if avg5 is not None and avg25 is not None and avg25 > 0:
            result["turnoverTrendRatio"] = round(avg5 / avg25, 2)

    # ========================================
    # モメンタム判定
    # ========================================
    rsi = result["rsi14"]
    pao = result["perfectOrder"]
    ttr = result["turnoverTrendRatio"]
    h52 = result["high52wRatio"]
    warnings = []

    green_count = 0
    red_count = 0

    # RSI評価
    if rsi is not None:
        if 45 <= rsi <= 73:
            green_count += 1
        elif rsi < 40 or rsi > 78:
            red_count += 1
            if rsi < 40:
                warnings.append(f"RSI低下({rsi:.0f})")
            else:
                warnings.append(f"RSI過熱({rsi:.0f})")

    # パーフェクトオーダー
    if pao is True:
        green_count += 1
    elif pao is False:
        red_count += 1
        warnings.append("MA崩れ")

    # 売買代金トレンド
    if ttr is not None:
        if ttr >= 0.9:
            green_count += 1
        elif ttr < 0.6:
            red_count += 1
            warnings.append(f"売買代金縮小({ttr:.2f}x)")

    # 52週高値比
    if h52 is not None:
        if h52 >= 85:
            green_count += 1
        elif h52 < 60:
            red_count += 1
            warnings.append(f"高値から乖離({h52:.0f}%)")

    # 判定（4軸のうち）
    if red_count >= 2:
        result["momentumStatus"] = "🔴喪失"
    elif red_count == 1 or green_count <= 1:
        result["momentumStatus"] = "🟡要注意"
    else:
        result["momentumStatus"] = "🟢継続"

    result["warnings"] = warnings
    return result


# ========================================
# メイン: 全ポジション点検
# ========================================

def check_portfolio_momentum() -> list[dict]:
    """
    実売買ポジション全件のモメンタムを点検して返す。

    Returns:
        list[dict]: ポジションごとの点検結果リスト
    """
    trade_log_path = Path(__file__).parent.parent / "memory" / "trade_log.json"
    if not trade_log_path.exists():
        logger.warning("trade_log.json が見つかりません。")
        return []

    with open(trade_log_path, "r", encoding="utf-8") as f:
        trade_log = json.load(f)

    positions = [
        p for p in trade_log.get("positions", [])
        if p.get("tradeType") == "actual"
    ]
    if not positions:
        logger.info("実売買ポジションなし。")
        return []

    # ローカルCSVから株価データを取得
    from agents.jquants_fetcher import load_latest_quotes
    df_all = load_latest_quotes()

    results = []
    for pos in positions:
        res = _analyze_position(pos, df_all)
        results.append(res)
        status = res["momentumStatus"]
        code = res["stockCode"]
        logger.info(f"PF点検: {code} {res['companyName']} → {status}")

    return results
