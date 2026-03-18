"""
agents/earnings_momentum_scanner.py
決算・開示シグナルのモメンタム投資特化スキャンモジュール

【フェーズ1: 前場前スコアリング (pre-market)】
  - EDINET分析済みスコア + momentum_potential で中長期上昇候補をランキング

【フェーズ2: ザラ場反応スキャン (intraday) ← メイン機能】
  - yfinanceで前場5分足データを取得（当日10:30頃実行）
  - 寄り付きギャップ・前場継続力・出来高急増を数値化
  - EDINETスコア × ザラ場反応 = 総合モメンタムスコアで順位付け
  - 「今エントリーすべき銘柄」を強度順に表示

【フェーズ3: 学習ループ】
  - earnings_signal_log.jsonで予測 → 実績を追跡
  - 5/10/20日後リターンを自動記録（記録5件以上で精度レポート表示）

作者: Japan Momentum Agent
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

# ザラ場スキャン実行タイミングの目安 (JST)
SCAN_TIME_NOTE = "10:30 JST（前場中盤・初動確認後）"

# ウォッチリスト保存先（夕方スキャン → 翌朝ザラ場スキャンへのブリッジ）
WATCHLIST_PATH = Path(__file__).parent.parent / "memory" / "earnings_watchlist.json"

# 学習ログ保存先
SIGNAL_LOG_PATH = Path(__file__).parent.parent / "memory" / "earnings_signal_log.json"

# ザラ場スキャン対象の最低EDINETスコア閾値
MIN_EDINET_SCORE = 30

# 学習ループ: 何日後に結果を記録するか
OUTCOME_CHECK_DAYS = [5, 10, 20]


# ========================================
# フェーズ2: ザラ場データ取得
# ========================================

def fetch_intraday_reaction(stock_code: str) -> dict:
    """
    yfinanceで当日前場の5分足データを取得し、反応強度指標を計算する。

    Returns:
        dict: {
            open_price, current_price, prev_close,
            opening_gap_pct,       # 寄り付きギャップ（前日比）
            intraday_momentum_pct, # 寄り付き後の継続（open比）
            volume_ratio,          # 当日出来高 / 直近5日平均
            is_making_new_highs,   # 直近高値を更新し続けているか
            bars_count,
        }
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinanceが未インストールです。pip install yfinance を実行してください。")
        return {}

    ticker_sym = f"{stock_code}.T"
    try:
        ticker = yf.Ticker(ticker_sym)

        # 5分足（当日）
        intra = ticker.history(period="1d", interval="5m")
        if intra.empty or len(intra) < 2:
            logger.debug(f"{stock_code}: 前場データなし（未取引または時間外）")
            return {}

        # 日足（直近10日）→ 前日終値・平均出来高取得
        daily = ticker.history(period="10d", interval="1d")
        if len(daily) < 2:
            return {}

        prev_close = float(daily["Close"].iloc[-2])
        avg_daily_volume = float(daily["Volume"].iloc[:-1].mean()) if len(daily) > 1 else 1.0

        open_price = float(intra["Open"].iloc[0])
        current_price = float(intra["Close"].iloc[-1])
        high_of_day = float(intra["High"].max())
        total_volume = int(intra["Volume"].sum())

        opening_gap_pct = (open_price / prev_close - 1) * 100 if prev_close > 0 else 0.0
        intraday_momentum_pct = (current_price / open_price - 1) * 100 if open_price > 0 else 0.0
        volume_ratio = (total_volume / avg_daily_volume) if avg_daily_volume > 0 else 1.0

        # 直近5本のバーが高値更新しているか（モメンタム継続の最重要指標）
        recent = intra.tail(5)
        highs = list(recent["High"])
        is_making_new_highs = all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))

        return {
            "open_price": round(open_price, 1),
            "current_price": round(current_price, 1),
            "prev_close": round(prev_close, 1),
            "opening_gap_pct": round(opening_gap_pct, 2),
            "intraday_momentum_pct": round(intraday_momentum_pct, 2),
            "volume_ratio": round(volume_ratio, 2),
            "high_of_day": round(high_of_day, 1),
            "is_making_new_highs": is_making_new_highs,
            "bars_count": len(intra),
        }

    except Exception as e:
        logger.warning(f"{stock_code} 前場データ取得エラー: {e}")
        return {}


def _calc_intraday_score(intra: dict) -> float:
    """
    ザラ場反応からモメンタムスコアを計算する。

    スコア設計:
      - intraday_momentum_pct (寄り後継続) に最大ウェイト → モメンタム投資の核心
      - opening_gap_pct (ギャップ) はポジティブだが出尽くしリスクも
      - volume_ratio は信頼性加重
      - is_making_new_highs はボーナス
    """
    if not intra:
        return 0.0

    gap = intra.get("opening_gap_pct", 0)
    momentum = intra.get("intraday_momentum_pct", 0)
    vol = intra.get("volume_ratio", 1.0)
    new_highs = intra.get("is_making_new_highs", False)

    # ネガティブな動き（ギャップ後反落）は強いペナルティ
    if gap > 0 and momentum < -gap * 0.5:
        return max(0, gap * 0.2)   # 出尽くし気配 → 低スコア

    score = (
        max(0, gap) * 0.25 +
        momentum * 0.50 +
        min(vol - 1.0, 2.0) * 0.5 +  # 出来高超過分をスコアに（上限2倍分）
        (1.0 if new_highs else 0.0)
    )
    return round(score, 2)


# ========================================
# メイン: ザラ場反応スキャン実行
# ========================================

def run_intraday_earnings_scan() -> list:
    """
    earnings_watchlist.json に保存された銘柄のザラ場反応をスキャンする。
    EDINETスコア × ザラ場反応スコア で順位付けして返す。

    Returns:
        list of dict: 総合スコア降順ソート済みの結果リスト
    """
    watchlist = _load_watchlist()
    if not watchlist:
        logger.info("ウォッチリストが空です。前日の決算スキャンで銘柄が登録されるまでお待ちください。")
        return []

    signals = watchlist.get("signals", [])
    watch_date = watchlist.get("date", "")
    if not signals:
        logger.info(f"ウォッチリスト（{watch_date}）に対象銘柄がありません。")
        return []

    logger.info(f"ザラ場スキャン開始: {len(signals)}銘柄（登録日: {watch_date}）")

    results = []
    for s in signals:
        code = s.get("stockCode", "")
        if not code:
            continue

        intra = fetch_intraday_reaction(code)
        intraday_score = _calc_intraday_score(intra)
        edinet_score = s.get("edinetScore", 0)

        # 総合スコア: EDINET基礎力 × ザラ場反応の加速度
        # EDINETスコアが高い銘柄でもザラ場反応がないと低スコア
        if not intra:
            total_score = 0.0
            entry_judgment = "データなし"
        elif intraday_score >= 4.0 and edinet_score >= 50:
            entry_judgment = "🔥 エントリー検討"
        elif intraday_score >= 2.0 and edinet_score >= 30:
            entry_judgment = "👀 様子見"
        elif intra.get("opening_gap_pct", 0) > 3 and intraday_score < 0:
            entry_judgment = "⚠️ ギャップ後反落・見送り"
        else:
            entry_judgment = "➡️ 反応薄"

        total_score = round(edinet_score * 0.4 + intraday_score * 10 * 0.6, 1)

        results.append({
            **s,
            "intradayData": intra,
            "intradayScore": intraday_score,
            "totalScore": total_score,
            "entryJudgment": entry_judgment,
            "scannedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        time.sleep(0.5)

    results.sort(key=lambda x: x["totalScore"], reverse=True)
    logger.info(f"ザラ場スキャン完了: {len(results)}銘柄")

    # 学習ループ: エントリー検討銘柄をシグナルログに記録
    for r in results:
        if "エントリー検討" in r.get("entryJudgment", ""):
            _record_earnings_signal(r)

    return results


# ========================================
# ウォッチリスト管理
# ========================================

def save_watchlist(analyzed_results: list) -> None:
    """
    EDINET分析済み結果からウォッチリストを作成・保存する。
    翌朝のザラ場スキャンで使用される。
    スコアが MIN_EDINET_SCORE 以上の銘柄のみ保存。

    Args:
        analyzed_results: edinet_analyzer.analyze_earnings_batch()の戻り値
    """
    targets = [
        r for r in analyzed_results
        if r.get("analyzed", False) and r.get("score", 0) >= MIN_EDINET_SCORE
    ]

    if not targets:
        logger.info("ウォッチリスト: スコア閾値以上の銘柄なし → 保存スキップ")
        return

    watchlist = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "createdAt": datetime.now().isoformat(),
        "signals": [
            {
                "stockCode": r.get("stockCode", ""),
                "companyName": r.get("companyName", ""),
                "edinetScore": r.get("score", 0),
                "momentumPotential": r.get("momentum_potential", "medium"),
                "entryTiming": r.get("entry_timing", ""),
                "catalystType": r.get("catalyst_type", ""),
                "summary": r.get("summary", ""),
                "positivePoints": r.get("positive_points", []),
                "revenueYoy": r.get("revenue_yoy", "不明"),
                "profitYoy": r.get("profit_yoy", "不明"),
                "vsForecast": r.get("vs_forecast", "不明"),
                "disclosureDate": datetime.now().strftime("%Y-%m-%d"),
            }
            for r in targets
        ],
    }

    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)
    logger.info(f"ウォッチリスト保存: {len(targets)}銘柄 → {WATCHLIST_PATH}")


def _load_watchlist() -> dict:
    """ウォッチリストを読み込む。"""
    if not WATCHLIST_PATH.exists():
        return {}
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"ウォッチリスト読み込みエラー: {e}")
        return {}


# ========================================
# フェーズ3: 学習ループ
# ========================================

def _record_earnings_signal(scan_result: dict) -> None:
    """
    エントリー検討シグナルを学習ログに記録する（重複防止付き）。
    """
    SIGNAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = []
    if SIGNAL_LOG_PATH.exists():
        try:
            with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []

    code = scan_result.get("stockCode", "")
    scan_date = scan_result.get("disclosureDate", datetime.now().strftime("%Y-%m-%d"))
    key = (code, scan_date)

    # 重複チェック
    if any((e.get("stockCode"), e.get("disclosureDate")) == key for e in log):
        return

    entry_price = scan_result.get("intradayData", {}).get("current_price", 0)
    log.append({
        "stockCode": code,
        "companyName": scan_result.get("companyName", ""),
        "disclosureDate": scan_date,
        "edinetScore": scan_result.get("edinetScore", 0),
        "momentumPotential": scan_result.get("momentumPotential", ""),
        "catalystType": scan_result.get("catalystType", ""),
        "intradayScore": scan_result.get("intradayScore", 0),
        "totalScore": scan_result.get("totalScore", 0),
        "entryPrice": entry_price,
        "openingGapPct": scan_result.get("intradayData", {}).get("opening_gap_pct", 0),
        "intradayMomentumPct": scan_result.get("intradayData", {}).get("intraday_momentum_pct", 0),
        "recordedAt": datetime.now().isoformat(),
        "outcome5d": None,
        "outcome10d": None,
        "outcome20d": None,
    })

    log = log[-200:]  # 最大200件
    with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def record_earnings_outcomes(df_all: pd.DataFrame) -> int:
    """
    earnings_signal_log.json の中で「N営業日経過・outcome未記録」のエントリに
    実際のリターンを記録する。

    Returns:
        int: 更新件数
    """
    if not SIGNAL_LOG_PATH.exists():
        return 0
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return 0

    code_col = "Code" if "Code" in df_all.columns else "code"
    today = pd.Timestamp.now().normalize()
    updated = 0

    for entry in log:
        code = entry.get("stockCode", "")
        entry_price = entry.get("entryPrice", 0)
        disc_date_str = entry.get("disclosureDate", "")
        if not code or not entry_price or not disc_date_str:
            continue

        try:
            disc_dt = pd.to_datetime(disc_date_str)
        except Exception:
            continue

        df_stock = df_all[df_all[code_col].astype(str) == code].sort_values("Date").reset_index(drop=True)
        future_df = df_stock[df_stock["Date"] > disc_dt].reset_index(drop=True)

        for days in OUTCOME_CHECK_DAYS:
            key = f"outcome{days}d"
            if entry.get(key) is not None:
                continue
            bdays_elapsed = len(pd.bdate_range(start=disc_dt, end=today)) - 1
            if bdays_elapsed < days:
                continue
            if len(future_df) < days:
                continue
            exit_price = float(future_df.iloc[days - 1]["Close"])
            ret = round((exit_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0
            entry[key] = {"returnPct": ret, "exitPrice": exit_price, "recordedAt": datetime.now().isoformat()}
            updated += 1

    if updated > 0:
        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        logger.info(f"決算シグナル結果記録: {updated}件更新")

    return updated


def get_earnings_accuracy_stats() -> dict:
    """
    学習ログから精度統計を集計する。
    """
    if not SIGNAL_LOG_PATH.exists():
        return {}
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return {}

    stats = {}
    for days in OUTCOME_CHECK_DAYS:
        key = f"outcome{days}d"
        returns = [e[key]["returnPct"] for e in log if e.get(key) and e[key].get("returnPct") is not None]
        if returns:
            wins = sum(1 for r in returns if r > 0)
            stats[key] = {
                "count": len(returns),
                "win_rate": round(wins / len(returns) * 100, 1),
                "avg_return": round(sum(returns) / len(returns), 2),
                "median_return": round(sorted(returns)[len(returns) // 2], 2),
            }

    stats["total_signals"] = len(log)
    return stats
