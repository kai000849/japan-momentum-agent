"""
agents/momentum_log_manager.py
MOメンタムシグナルの学習ループ管理

- log_momentum_signals():    MOシグナルを momentum_log.json に記録
- record_momentum_outcomes(): 20営業日後のアウトカム（リターン・DD・トレンド継続）を自動記録
- get_momentum_patterns():   MAギャップ / 高値比 / 出来高トレンド別の勝率パターン分析
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

MOMENTUM_LOG_PATH = Path(__file__).parent.parent / "memory" / "momentum_log.json"
OUTCOME_DAYS = 20  # 20営業日後をアウトカム基準


# ========================================
# 内部ユーティリティ
# ========================================

def _load_log() -> list:
    if not MOMENTUM_LOG_PATH.exists():
        return []
    try:
        with open(MOMENTUM_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"momentum_log.json 読み込みエラー: {e}")
        return []


def _save_log(log: list) -> None:
    MOMENTUM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MOMENTUM_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def _detect_code_col(df: pd.DataFrame) -> str:
    for col in ["Code", "code", "StockCode", "stock_code"]:
        if col in df.columns:
            return col
    raise ValueError(f"銘柄コード列が見つかりません。列: {list(df.columns)}")


def _get_stock_df(df_all: pd.DataFrame, code_col: str, stock_code: str) -> pd.DataFrame:
    """銘柄コードで株価データを取得（4桁/5桁の揺れを吸収）"""
    code_str = str(stock_code)
    df = df_all[df_all[code_col].astype(str) == code_str]
    if df.empty:
        alt = code_str[:4] if (len(code_str) == 5 and code_str.endswith("0")) else code_str + "0"
        df = df_all[df_all[code_col].astype(str) == alt]
    return df.sort_values("Date").reset_index(drop=True)


# ========================================
# シグナル記録
# ========================================

def log_momentum_signals(signals: list) -> int:
    """
    MOメンタムスキャン結果を momentum_log.json に記録する。
    同日・同銘柄の重複は上書きしない（既存エントリを保持）。

    Returns:
        int: 新規追加件数
    """
    if not signals:
        return 0

    log = _load_log()
    existing_ids = {e.get("id") for e in log}

    added = 0
    for sig in signals:
        code = str(sig.get("stockCode", ""))
        date = sig.get("scanDate", "")
        if not code or not date:
            continue

        entry_id = f"{code}_{date.replace('-', '')}"
        if entry_id in existing_ids:
            continue

        entry = {
            "id": entry_id,
            "stockCode": code,
            "companyName": sig.get("companyName", ""),
            "signalDate": date,
            "close": sig.get("close"),
            # A: MAギャップ（パーフェクトオーダーの厚み）
            "ma_gap_5_25": sig.get("ma_gap_5_25"),
            "ma_gap_25_75": sig.get("ma_gap_25_75"),
            # B: 52週高値まわり
            "high52w_ratio": sig.get("high52w_ratio") or sig.get("priceToHighRatio"),
            "new_high_days": sig.get("new_high_days") or sig.get("newHighCount"),
            # C: 出来高トレンド
            "volume_trend_ratio": sig.get("volume_trend_ratio") or sig.get("volumeTrend"),
            # 参考指標
            "rsi14": sig.get("rsi14"),
            "return_20d_signal": sig.get("return_20d_signal"),
            "score": sig.get("score"),
            # アウトカム（20営業日後に記録）
            "outcome": None,
            "loggedAt": datetime.now().isoformat(),
        }
        log.append(entry)
        existing_ids.add(entry_id)
        added += 1

    if added > 0:
        _save_log(log)
        logger.info(f"momentum_log: {added}件追加（合計{len(log)}件）")

    return added


# ========================================
# アウトカム記録（20営業日後）
# ========================================

def record_momentum_outcomes(df_all: pd.DataFrame) -> int:
    """
    20営業日が経過したMOシグナルのアウトカムを記録する。

    アウトカム内容:
    - return_20d:       翌営業日始値→20営業日後終値のリターン(%)
    - max_drawdown_20d: 保有期間中の最大ドローダウン(%)
    - trend_maintained: 20日後時点でMA5>MA25>MA75継続か(bool)

    Returns:
        int: 更新件数
    """
    log = _load_log()
    if not log:
        return 0

    try:
        code_col = _detect_code_col(df_all)
    except ValueError as e:
        logger.warning(f"record_momentum_outcomes: {e}")
        return 0

    updated = 0

    for entry in log:
        if entry.get("outcome") is not None:
            continue

        signal_date = entry.get("signalDate", "")
        stock_code = entry.get("stockCode", "")
        if not signal_date or not stock_code:
            continue

        stock_df = _get_stock_df(df_all, code_col, stock_code)
        if stock_df.empty:
            continue

        signal_dt = pd.to_datetime(signal_date)
        future_df = stock_df[stock_df["Date"] > signal_dt].reset_index(drop=True)

        # 20営業日分のデータが揃っていない場合はスキップ
        if len(future_df) < OUTCOME_DAYS:
            continue

        # ---- エントリー価格: 翌営業日の始値（なければ終値） ----
        entry_row = future_df.iloc[0]
        entry_price = float(entry_row.get("Open") or 0)
        if entry_price <= 0:
            entry_price = float(entry_row.get("Close") or 0)
        if entry_price <= 0:
            continue
        entry_date = str(entry_row["Date"])[:10]

        # ---- 出口価格: 20営業日目の終値 ----
        exit_row = future_df.iloc[OUTCOME_DAYS - 1]
        exit_price = float(exit_row.get("Close") or 0)
        if exit_price <= 0:
            continue
        exit_date = str(exit_row["Date"])[:10]

        # ---- リターン ----
        return_20d = (exit_price - entry_price) / entry_price * 100

        # ---- 最大ドローダウン: 保有期間20日のLow基準 ----
        period = future_df.iloc[:OUTCOME_DAYS]
        if "Low" in period.columns:
            period_lows = period["Low"].astype(float)
        else:
            period_lows = period["Close"].astype(float)
        drawdowns = (period_lows - entry_price) / entry_price * 100
        max_drawdown = float(drawdowns.min())

        # ---- トレンド継続: 出口日時点のMA5>MA25>MA75 ----
        exit_dt = exit_row["Date"]
        hist_df = stock_df[stock_df["Date"] <= exit_dt]
        trend_maintained = None
        if len(hist_df) >= 75:
            closes_hist = hist_df["Close"].astype(float)
            ma5 = float(closes_hist.tail(5).mean())
            ma25 = float(closes_hist.tail(25).mean())
            ma75 = float(closes_hist.tail(75).mean())
            trend_maintained = bool(ma5 > ma25 > ma75)

        entry["outcome"] = {
            "status": "recorded",
            "recordedAt": datetime.now().isoformat(),
            "signalDate": signal_date,
            "entryDate": entry_date,
            "entryPrice": round(entry_price, 2),
            "exitDate": exit_date,
            "exitPrice": round(exit_price, 2),
            "return_20d": round(return_20d, 2),
            "max_drawdown_20d": round(max_drawdown, 2),
            "trend_maintained": trend_maintained,
        }
        updated += 1

    if updated > 0:
        _save_log(log)
        logger.info(f"momentum_log: outcome {updated}件記録")

    return updated


# ========================================
# パターン分析
# ========================================

def get_momentum_patterns() -> dict:
    """
    MOシグナルのアウトカムをパターン分析する。

    分析軸:
    - A by_ma_gap:       MAギャップ5-25別勝率（小/<2% / 中/2-5% / 大/>5%）
    - B by_high52w_ratio: 52週高値比別勝率（95-97% / 97-99% / 99%以上）
    - C by_volume_trend: 出来高トレンド別勝率（1.0-1.1x / 1.1-1.2x / 1.2x以上）

    勝利条件: return_20d > 0

    Returns:
        dict: パターン分析結果（件数<5の場合は空dict）
    """
    log = _load_log()
    recorded = [
        e for e in log
        if e.get("outcome") and e["outcome"].get("status") == "recorded"
    ]

    if len(recorded) < 5:
        return {"total": len(recorded), "insufficient": True}

    def _stats(entries: list) -> dict:
        if not entries:
            return None
        wins = [e for e in entries if e["outcome"]["return_20d"] > 0]
        returns = [e["outcome"]["return_20d"] for e in entries]
        trend_ok = [e for e in entries if e["outcome"].get("trend_maintained") is True]
        return {
            "count": len(entries),
            "win_rate": round(len(wins) / len(entries) * 100, 1),
            "avg_return": round(sum(returns) / len(returns), 2),
            "trend_maintained_rate": round(len(trend_ok) / len(entries) * 100, 1),
        }

    # A: MAギャップ5-25
    by_ma_gap: dict[str, list] = {}
    for e in recorded:
        g = e.get("ma_gap_5_25") or 0
        if g < 2:
            k = "小(<2%)"
        elif g < 5:
            k = "中(2-5%)"
        else:
            k = "大(>5%)"
        by_ma_gap.setdefault(k, []).append(e)

    # B: 52週高値比
    by_high52w: dict[str, list] = {}
    for e in recorded:
        r = e.get("high52w_ratio") or 0
        if r < 97:
            k = "95-97%"
        elif r < 99:
            k = "97-99%"
        else:
            k = "99%以上"
        by_high52w.setdefault(k, []).append(e)

    # C: 出来高トレンド比
    by_vol_trend: dict[str, list] = {}
    for e in recorded:
        v = e.get("volume_trend_ratio") or 1.0
        if v < 1.1:
            k = "1.0-1.1x"
        elif v < 1.2:
            k = "1.1-1.2x"
        else:
            k = "1.2x以上"
        by_vol_trend.setdefault(k, []).append(e)

    return {
        "total": len(recorded),
        "overall": _stats(recorded),
        "by_ma_gap": {k: _stats(v) for k, v in by_ma_gap.items()},
        "by_high52w_ratio": {k: _stats(v) for k, v in by_high52w.items()},
        "by_volume_trend": {k: _stats(v) for k, v in by_vol_trend.items()},
    }


def get_momentum_log_summary() -> dict:
    """週次レポート用のサマリー情報を返す"""
    log = _load_log()
    total = len(log)
    recorded = [e for e in log if e.get("outcome") and e["outcome"].get("status") == "recorded"]
    pending = [e for e in log if e.get("outcome") is None]
    return {
        "total_logged": total,
        "total_recorded": len(recorded),
        "total_pending": len(pending),
    }
