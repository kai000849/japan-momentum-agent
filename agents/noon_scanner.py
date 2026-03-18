"""
agents/noon_scanner.py
正午スキャン（後場エントリー判断）

JST 12:15頃に実行。朝8:00スキャンで検出したSHORT_TERM/MOMENTUM/EARNINGSシグナル銘柄を
yfinanceの当日1時間足で確認し、前場の動きを評価して後場（12:30〜）エントリー可否を判定する。

判定ロジック:
  GO   : 前場で急騰終値の97%以上を維持 かつ 前場終値>前場始値（上昇継続）
  WATCH: 値を維持しているが上昇トレンドが弱い
  SKIP : 前場で急騰終値の97%を割り込んだ（失速）

データソース:
  - 朝スキャン結果: data/processed/scans/scan_YYYYMMDD_*.json
  - 当日前場データ: yfinance (15分遅延・無料)

作者: Japan Momentum Agent
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
SCANS_DIR = BASE_DIR / "data" / "processed" / "scans"
NOON_SCANS_DIR = BASE_DIR / "data" / "processed" / "noon_scans"
NOON_SCANS_DIR.mkdir(parents=True, exist_ok=True)

# 前場維持判定の閾値
HOLD_THRESHOLD = 0.97   # 急騰終値の97%以上をキープしていればOK
GO_TREND_MIN = 0.0      # 前場終値 > 前場始値 (0%以上)


# ========================================
# yfinanceデータ取得
# ========================================

def _to_yf_ticker(stock_code: str) -> str:
    """
    J-Quantsの銘柄コード（5桁）→ yfinanceティッカー（例: "6740.T"）に変換する。
    末尾の "0" は市場コードなので除いて4桁+".T"。
    """
    c = str(stock_code).strip()
    if len(c) == 5 and c.endswith("0"):
        return f"{c[:4]}.T"
    return f"{c}.T"


def fetch_intraday(stock_code: str) -> Optional[dict]:
    """
    yfinanceで当日の1時間足データを取得し、前場の動きを集計して返す。

    Returns:
        dict or None: {
            "ticker":          str,
            "current_price":   float,  # 最新価格（15分遅延）
            "open_price":      float,  # 当日始値
            "morning_high":    float,  # 前場高値
            "morning_low":     float,  # 前場低値
            "morning_return":  float,  # 前場リターン（始値比 %）
            "volume_ratio":    float,  # 当日累計出来高 / 出来高平均（概算）
            "candles":         int,    # 取得できたローソク足本数
        }
        失敗時はNone。
    """
    try:
        import yfinance as yf

        ticker = _to_yf_ticker(stock_code)
        df = yf.download(ticker, period="2d", interval="1h", progress=False, auto_adjust=True)

        if df is None or df.empty:
            logger.warning(f"yfinance: {ticker} データなし")
            return None

        # 当日分のみ絞り込み（Japanはタイムゾーン付き・UTC+9）
        today_str = date.today().isoformat()
        today_rows = df[df.index.strftime("%Y-%m-%d") == today_str]

        if today_rows.empty:
            # タイムゾーンなしの場合はUTC日付で絞り直す
            import pandas as pd
            today_utc = (datetime.utcnow()).strftime("%Y-%m-%d")
            today_rows = df[df.index.strftime("%Y-%m-%d") == today_utc]

        if today_rows.empty:
            # フォールバック: 最後の数行を使う
            today_rows = df.tail(4)

        # 前場は 9:00〜11:30 (UTC 0:00〜2:30)
        # yfinance は JST変換される場合とUTCの場合がある → 最初の4本を前場とみなす
        morning_rows = today_rows.head(4)

        if morning_rows.empty:
            logger.warning(f"yfinance: {ticker} 当日データなし")
            return None

        # Close列がMultiIndexの場合はflatten
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            today_rows.columns = today_rows.columns.droplevel(1)
            morning_rows = today_rows.head(4)

        open_price   = float(morning_rows["Open"].iloc[0])
        morning_high = float(morning_rows["High"].max())
        morning_low  = float(morning_rows["Low"].min())
        current_price = float(today_rows["Close"].iloc[-1])

        morning_return = (current_price - open_price) / open_price * 100 if open_price > 0 else 0.0

        # 出来高比率（概算: 当日累計 / 全データの平均1時間出来高 × 4本換算）
        volume_today = float(today_rows["Volume"].sum())
        volume_avg_1h = float(df["Volume"].mean())
        volume_ratio = volume_today / (volume_avg_1h * 4) if volume_avg_1h > 0 else 1.0

        return {
            "ticker":         ticker,
            "current_price":  current_price,
            "open_price":     open_price,
            "morning_high":   morning_high,
            "morning_low":    morning_low,
            "morning_return": round(morning_return, 2),
            "volume_ratio":   round(volume_ratio, 2),
            "candles":        len(today_rows),
        }

    except Exception as e:
        logger.warning(f"yfinance取得エラー ({stock_code}): {e}")
        return None


# ========================================
# 朝スキャン結果の読み込み
# ========================================

def _load_latest_scan(mode: str, scan_date: Optional[str] = None) -> list:
    """
    指定モードの最新スキャン結果を読み込む。
    scan_date 未指定の場合は最新ファイルを使用。
    """
    if not SCANS_DIR.exists():
        return []

    if scan_date:
        fname = SCANS_DIR / f"scan_{scan_date.replace('-', '')}_{mode}.json"
        if fname.exists():
            files = [fname]
        else:
            files = []
    else:
        files = sorted(SCANS_DIR.glob(f"scan_*_{mode}.json"))

    if not files:
        return []

    try:
        with open(files[-1], "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("results", [])
    except Exception as e:
        logger.warning(f"スキャン結果読み込みエラー ({mode}): {e}")
        return []


def _load_qualify_log_latest() -> list:
    """qualify_logの最新スキャン日分のSTRONG/WATCHエントリを返す。"""
    qualify_log_path = BASE_DIR / "memory" / "qualify_log.json"
    if not qualify_log_path.exists():
        return []
    try:
        with open(qualify_log_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        if not entries:
            return []
        latest_date = max(e.get("scanDate", "") for e in entries)
        return [
            e for e in entries
            if e.get("scanDate") == latest_date
            and e.get("qualifyResult") in ("STRONG", "WATCH")
        ]
    except Exception as e:
        logger.warning(f"qualify_log読み込みエラー: {e}")
        return []


# ========================================
# 前場維持判定
# ========================================

def _judge_holding(intraday: dict, scan_close: float) -> tuple[str, list]:
    """
    前場データと急騰終値を比較してGO/WATCH/SKIPを判定する。

    Returns:
        (judgment: str, reasons: list[str])
    """
    current = intraday["current_price"]
    open_p  = intraday["open_price"]
    mret    = intraday["morning_return"]
    vratio  = intraday["volume_ratio"]

    reasons = []

    if scan_close <= 0:
        return "WATCH", ["前日終値データなし（要確認）"]

    hold_pct = (current - scan_close) / scan_close * 100

    # ---- 維持チェック ----
    is_holding = current >= scan_close * HOLD_THRESHOLD
    is_trending_up = current >= open_p  # 始値以上 = 前場維持または上昇

    if hold_pct >= 0:
        reasons.append(f"✅ 急騰値を維持 ({hold_pct:+.1f}%)")
    elif hold_pct >= -3:
        reasons.append(f"⚠️ 若干軟化 ({hold_pct:+.1f}%)")
    else:
        reasons.append(f"❌ 急騰値を割り込み ({hold_pct:+.1f}%)")

    if mret >= 1.0:
        reasons.append(f"✅ 前場上昇 ({mret:+.1f}%)")
    elif mret >= -1.0:
        reasons.append(f"➡️ 前場横ばい ({mret:+.1f}%)")
    else:
        reasons.append(f"⬇️ 前場下落 ({mret:+.1f}%)")

    if vratio >= 1.5:
        reasons.append(f"✅ 出来高高水準 (×{vratio:.1f})")
    elif vratio >= 0.8:
        reasons.append(f"➡️ 出来高普通 (×{vratio:.1f})")
    else:
        reasons.append(f"⚠️ 出来高低下 (×{vratio:.1f})")

    # ---- 総合判定 ----
    if is_holding and is_trending_up:
        judgment = "GO"
    elif is_holding:
        judgment = "WATCH"
    else:
        judgment = "SKIP"

    return judgment, reasons


# ========================================
# メイン: 正午スキャン実行
# ========================================

def run_noon_scan(scan_date: Optional[str] = None) -> list:
    """
    朝スキャンのSTRONG/WATCH銘柄について yfinance で前場データを取得し、
    後場エントリー可否を判定する。

    Args:
        scan_date: 朝スキャンの日付（省略時は最新ファイル）

    Returns:
        list: [
            {
                "stockCode": str,
                "companyName": str,
                "mode": str,             # SHORT_TERM / MOMENTUM / EARNINGS
                "qualifyResult": str,    # STRONG / WATCH（qualify_logから）
                "scanClose": float,      # 朝スキャン時の終値
                "intradayData": dict,    # yfinanceから取得した前場データ
                "judgment": str,         # GO / WATCH / SKIP
                "reasons": list[str],
            }
        ]
    """
    logger.info("正午スキャン開始")

    # ---- 対象銘柄を収集 ----
    # priority1: qualify_logのSTRONG/WATCH（SHORT_TERMシグナル）
    qualify_entries = _load_qualify_log_latest()
    qualify_map = {
        str(e.get("stockCode", "")).strip(): e
        for e in qualify_entries
    }

    # priority2: MOMMENTUMスキャン結果
    momentum_results = _load_latest_scan("MOMENTUM", scan_date)
    momentum_map = {
        str(r.get("stockCode", "")).strip(): r
        for r in momentum_results
    }

    # priority3: EARNINGSスキャン結果（score>=30）
    earnings_results = _load_latest_scan("EARNINGS", scan_date)
    earnings_map = {
        str(r.get("stockCode", "")).strip(): r
        for r in earnings_results
        if r.get("score", 0) >= 30
    }

    # 対象コードを重複なく収集（STRONG/WATCH → MOMENTUM → EARNINGS の優先度）
    target_codes = list(dict.fromkeys(
        list(qualify_map.keys()) +
        list(momentum_map.keys()) +
        list(earnings_map.keys())
    ))

    if not target_codes:
        logger.info("正午スキャン: 対象銘柄なし（朝スキャン結果が見つかりません）")
        return []

    logger.info(f"正午スキャン対象: {len(target_codes)}銘柄")

    # ---- yfinanceで前場データ取得 → 判定 ----
    results = []
    for code in target_codes:
        # 銘柄情報を取得
        if code in qualify_map:
            entry = qualify_map[code]
            mode = entry.get("mode", "SHORT_TERM")
            company = entry.get("companyName", "")
            scan_close = entry.get("close", 0)
            qualify_result = entry.get("qualifyResult", "WATCH")
        elif code in momentum_map:
            entry = momentum_map[code]
            mode = "MOMENTUM"
            company = entry.get("companyName", "")
            scan_close = entry.get("close", 0)
            qualify_result = "MOMENTUM"
        else:
            entry = earnings_map.get(code, {})
            mode = "EARNINGS"
            company = entry.get("companyName", "")
            scan_close = entry.get("close", 0)
            qualify_result = "EARNINGS"

        # yfinanceで前場データ取得
        intraday = fetch_intraday(code)
        if intraday is None:
            results.append({
                "stockCode": code,
                "companyName": company,
                "mode": mode,
                "qualifyResult": qualify_result,
                "scanClose": scan_close,
                "intradayData": None,
                "judgment": "WATCH",
                "reasons": ["⚠️ 前場データ取得失敗（yfinance）"],
            })
            continue

        judgment, reasons = _judge_holding(intraday, scan_close)

        results.append({
            "stockCode": code,
            "companyName": company,
            "mode": mode,
            "qualifyResult": qualify_result,
            "scanClose": scan_close,
            "intradayData": intraday,
            "judgment": judgment,
            "reasons": reasons,
        })

    # GO → WATCH → SKIP の順にソート
    order = {"GO": 0, "WATCH": 1, "SKIP": 2}
    results.sort(key=lambda x: order.get(x["judgment"], 3))

    # ---- 結果をJSONに保存（GitHub Actionsキャッシュ対象外・参考用） ----
    try:
        out_path = NOON_SCANS_DIR / f"noon_{datetime.now().strftime('%Y%m%d')}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "generatedAt": datetime.now().isoformat(),
                "targetCount": len(results),
                "results": results,
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"正午スキャン結果を保存: {out_path}")
    except Exception as e:
        logger.warning(f"正午スキャン結果の保存失敗: {e}")

    go_count = sum(1 for r in results if r["judgment"] == "GO")
    logger.info(f"正午スキャン完了: GO={go_count} / 計{len(results)}銘柄")
    return results
