"""
agents/earnings_momentum_scanner.py
決算・開示シグナルのモメンタム投資特化スキャンモジュール

【フェーズ1: 前場前スコアリング (pre-market)】
  - EDINET分析済みスコア + momentum_potential で中長期上昇候補をランキング

【フェーズ2A: ザラ場反応スキャン (intraday) ← メイン機能】
  - yfinanceで前場5分足データを取得（当日10:30頃実行・J-Quants Lightに分足なし）
  - 寄り付きギャップ・前場継続力・出来高急増を数値化
  - EDINETスコア × ザラ場反応 = 総合モメンタムスコアで順位付け
  - 「今エントリーすべき銘柄」を強度順に表示

【フェーズ2B: 引け後評価スキャン (end-of-day)】
  - J-Quants日足データで一日の値動きを評価（18:00 JST通知に反映）
  - 終値ポジション・上ヒゲ比率・出来高急増・ローソク足パターンを数値化
  - 「翌朝エントリー候補」を強度順に表示

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

# Claude APIモデル
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ザラ場スキャン実行タイミングの目安 (JST)
SCAN_TIME_NOTE = "10:30 JST（前場中盤・初動確認後）"

# ウォッチリスト保存先（夕方スキャン → 翌朝ザラ場スキャンへのブリッジ）
WATCHLIST_PATH = Path(__file__).parent.parent / "memory" / "earnings_watchlist.json"

# 学習ログ保存先
SIGNAL_LOG_PATH = Path(__file__).parent.parent / "memory" / "earnings_signal_log.json"

# 中長期フォローアップリスト保存先（初動反応良好銘柄を最大20営業日追跡）
FOLLOWUP_LIST_PATH = Path(__file__).parent.parent / "memory" / "earnings_followup_list.json"

# ザラ場スキャン対象の最低EDINETスコア閾値
MIN_EDINET_SCORE = 30

# 学習ループ: 何日後に結果を記録するか
OUTCOME_CHECK_DAYS = [5, 10, 20]

# フォローアップ継続営業日数
FOLLOWUP_MAX_BDAYS = 20

# フォローアップ追加の最低スコア
FOLLOWUP_MIN_TOTAL_SCORE = 30.0


# ========================================
# コメント生成: Claude API（共通）
# ========================================

def generate_ranking_comments_batch(ranked_stocks: list, scan_type: str = "intraday") -> dict:
    """
    ランキング上位銘柄に対し「なぜ今注目すべきか」コメントをClaude APIで一括生成する。

    Args:
        ranked_stocks: スコアソート済みリスト
        scan_type: "intraday"（10:30前場）or "endofday"（引け後）

    Returns:
        dict: {stockCode: comment}
    """
    from agents.utils import get_anthropic_key
    api_key = get_anthropic_key()
    if not api_key or not ranked_stocks:
        return {}

    targets = [r for r in ranked_stocks[:6] if r.get("totalScore", 0) > 0]
    if not targets:
        return {}

    scan_label = "前場10:30時点" if scan_type == "intraday" else "引け後15:30時点"
    stocks_text = ""
    for s in targets:
        code = s.get("stockCode", "")
        name = s.get("companyName", "")
        edinet = s.get("edinetScore", 0)
        catalyst = s.get("catalystType", s.get("catalyst_type", ""))
        summary = (s.get("summary") or "")[:50]

        if scan_type == "intraday":
            intra = s.get("intradayData", {})
            price_str = (
                f"寄り:{intra.get('opening_gap_pct', 0):+.1f}% "
                f"/ 前場継続:{intra.get('intraday_momentum_pct', 0):+.1f}% "
                f"/ 出来高:{intra.get('volume_ratio', 0):.1f}倍"
                f"{'・新高値更新中' if intra.get('is_making_new_highs') else ''}"
            )
        else:
            eod = s.get("eodData", {})
            price_str = (
                f"終日変化:{eod.get('day_return_pct', 0):+.1f}% "
                f"/ 寄り後継続:{eod.get('close_vs_open_pct', 0):+.1f}% "
                f"/ 出来高:{eod.get('volume_ratio', 0):.1f}倍 "
                f"/ {eod.get('candle_pattern', '')}"
            )

        stocks_text += (
            f"【{code} {name}】EDINET:{edinet}点 / {catalyst}\n"
            f"概要: {summary}\n"
            f"{price_str}\n\n"
        )

    prompt = f"""あなたはモメンタム投資の専門家です。以下の銘柄の{scan_label}データを見て、
各銘柄に「モメンタム投資家として今何を注目すべきか」を40文字以内で簡潔にコメントしてください。

{stocks_text}
必ず以下のJSON形式のみで回答してください：
{{
  "comments": [
    {{
      "stockCode": "<銘柄コード>",
      "comment": "<40文字以内のコメント>"
    }}
  ]
}}"""

    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = _re.sub(r'```(?:json)?', '', raw)
        s = raw.find("{"); e = raw.rfind("}") + 1
        if s >= 0 and e > s:
            raw = raw[s:e]
        parsed = json.loads(raw)
        return {str(item["stockCode"]): item.get("comment", "") for item in parsed.get("comments", [])}
    except Exception as ex:
        logger.warning(f"コメント生成エラー: {ex}")
        return {}


# ========================================
# フェーズ2A: ザラ場データ取得（yfinance ※J-Quants Lightに分足なし）
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

        # 張り付き検出: 直近5本のHighがすべて同一 = ストップ高に張り付いている
        is_haritsuki = (len(highs) >= 3 and len(set(round(h, 0) for h in highs)) == 1
                        and opening_gap_pct >= 5)

        # 寄らず検出: 全バーの出来高合計が極小（買い気配で板が刺さらない状態）
        is_yobarazu_intra = (total_volume < 500 and opening_gap_pct >= 10)

        return {
            "open_price": round(open_price, 1),
            "current_price": round(current_price, 1),
            "prev_close": round(prev_close, 1),
            "opening_gap_pct": round(opening_gap_pct, 2),
            "intraday_momentum_pct": round(intraday_momentum_pct, 2),
            "volume_ratio": round(volume_ratio, 2),
            "high_of_day": round(high_of_day, 1),
            "is_making_new_highs": is_making_new_highs,
            "is_haritsuki": is_haritsuki,
            "is_yobarazu": is_yobarazu_intra,
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
    is_haritsuki = intra.get("is_haritsuki", False)
    is_yobarazu = intra.get("is_yobarazu", False)

    # 張り付き・寄らず → 最強シグナル
    if is_yobarazu:
        return 8.0   # 寄らずS高買い気配: 需給の圧倒的優位
    if is_haritsuki:
        return 6.5   # ストップ高張り付き継続中

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

    # Claude APIでランキングコメント生成
    comment_map = generate_ranking_comments_batch(results, scan_type="intraday")
    for r in results:
        r["comment"] = comment_map.get(r.get("stockCode", ""), "")

    # 学習ループ: エントリー検討銘柄をシグナルログに記録
    for r in results:
        if "エントリー検討" in r.get("entryJudgment", ""):
            _record_earnings_signal(r)

    return results


# ========================================
# 東証値幅制限テーブル（ストップ高検出用）
# ========================================

def _estimate_stop_price(prev_close: float) -> float:
    """
    東証の値幅制限（前日終値ベース）からストップ高価格を推定する。
    https://www.jpx.co.jp/markets/domestic/equities/02.html
    """
    limits = [
        (100,    30),
        (200,    50),
        (500,    80),
        (700,   100),
        (1000,  150),
        (1500,  300),
        (2000,  400),
        (3000,  500),
        (5000,  700),
        (7000, 1000),
        (10000, 1500),
        (15000, 3000),
        (20000, 4000),
        (30000, 5000),
        (50000, 7000),
    ]
    for threshold, limit in limits:
        if prev_close < threshold:
            return prev_close + limit
    return prev_close + 10000  # ¥50,000以上


# ========================================
# フェーズ2B: 引け後評価（J-Quants日足使用）
# ========================================

def fetch_endofday_reaction(stock_code: str, df_all: "pd.DataFrame") -> dict:
    """
    J-Quants日足データから当日の引け後評価指標を計算する（yfinance不使用）。

    Args:
        stock_code: 4桁銘柄コード
        df_all: load_latest_quotes()で取得した全銘柄DataFrame

    Returns:
        dict: {
            open_price, close_price, high_price, low_price, prev_close,
            day_return_pct,       # 前日比（%）
            close_vs_open_pct,    # 寄り付き後の値動き（open→close）
            upper_wick_ratio,     # 上ヒゲ比率（上ヒゲ/値幅 0〜1）
            close_position,       # 終値のレンジ内位置（0=底、1=高値）
            volume_ratio,         # 当日出来高 / 直近20日平均
            candle_pattern,       # ローソク足パターン（白マルボーズ等）
        }
    """
    try:
        code_col = "Code" if "Code" in df_all.columns else "code"
        df_stock = (
            df_all[df_all[code_col].astype(str) == str(stock_code)]
            .sort_values("Date")
            .reset_index(drop=True)
        )
        if len(df_stock) < 5:
            logger.debug(f"{stock_code}: J-Quants日足データ不足（{len(df_stock)}行）")
            return {}

        today_row = df_stock.iloc[-1]
        prev_row = df_stock.iloc[-2]

        open_p = float(today_row["Open"])
        high_p = float(today_row["High"])
        low_p = float(today_row["Low"])
        close_p = float(today_row["Close"])
        volume = float(today_row.get("Volume", 0))
        prev_close = float(prev_row["Close"])

        # 直近20日平均出来高（当日を除く）
        hist = df_stock["Volume"].iloc[-21:-1]
        avg_vol = float(hist.mean()) if len(hist) >= 5 else float(df_stock["Volume"].iloc[:-1].mean())

        day_return_pct = (close_p / prev_close - 1) * 100 if prev_close > 0 else 0.0
        close_vs_open_pct = (close_p / open_p - 1) * 100 if open_p > 0 else 0.0

        price_range = high_p - low_p
        upper_wick = high_p - max(open_p, close_p)
        upper_wick_ratio = upper_wick / price_range if price_range > 0 else 0.0
        close_position = (close_p - low_p) / price_range if price_range > 0 else 0.5
        volume_ratio = volume / avg_vol if avg_vol > 0 else 1.0

        # ストップ高・寄らず検出
        stop_price = _estimate_stop_price(prev_close)
        is_stop_high = close_p >= stop_price * 0.999          # 終値がストップ高価格以上
        is_yobarazu = volume < 100 or (avg_vol > 0 and volume / avg_vol < 0.03)  # 出来高ほぼゼロ

        # ローソク足パターン判定
        body = abs(close_p - open_p)
        if is_stop_high and is_yobarazu:
            candle_pattern = "S高買い気配（寄らず）"
        elif is_stop_high:
            candle_pattern = "ストップ高張り付き"
        elif price_range > 0 and body / price_range >= 0.7 and upper_wick_ratio < 0.1 and close_p > open_p:
            candle_pattern = "白マルボーズ（超強気）"
        elif upper_wick_ratio > 0.5 and close_position < 0.3:
            candle_pattern = "上ヒゲ陰線（売り圧強）"
        elif price_range > 0 and body / price_range < 0.1:
            candle_pattern = "十字線（迷い）"
        elif close_p > open_p:
            candle_pattern = "陽線"
        else:
            candle_pattern = "陰線"

        return {
            "open_price": round(open_p, 1),
            "close_price": round(close_p, 1),
            "high_price": round(high_p, 1),
            "low_price": round(low_p, 1),
            "prev_close": round(prev_close, 1),
            "stop_price": round(stop_price, 1),
            "day_return_pct": round(day_return_pct, 2),
            "close_vs_open_pct": round(close_vs_open_pct, 2),
            "upper_wick_ratio": round(upper_wick_ratio, 3),
            "close_position": round(close_position, 3),
            "volume_ratio": round(volume_ratio, 2),
            "candle_pattern": candle_pattern,
            "is_stop_high": is_stop_high,
            "is_yobarazu": is_yobarazu,
        }

    except Exception as e:
        logger.warning(f"{stock_code} 引け後データ取得エラー: {e}")
        return {}


def _calc_endofday_score(eod: dict) -> float:
    """
    引け後の日足データからモメンタムスコアを計算する。

    スコア設計:
      - close_position (終値のレンジ内位置) → 高いほど強い終わり方（最重要）
      - day_return_pct (前日比) → プラスで高いほど良い
      - volume_ratio (出来高) → 信頼性加重
      - upper_wick_ratio (上ヒゲ) → 高いとペナルティ（売り圧）
    """
    if not eod:
        return 0.0

    close_pos = eod.get("close_position", 0.5)
    day_ret = eod.get("day_return_pct", 0)
    vol = eod.get("volume_ratio", 1.0)
    upper_wick = eod.get("upper_wick_ratio", 0)
    close_vs_open = eod.get("close_vs_open_pct", 0)

    # ストップ高・寄らず → 最強シグナル（通常スコアリングをスキップして高得点固定）
    is_stop_high = eod.get("is_stop_high", False)
    is_yobarazu = eod.get("is_yobarazu", False)
    if is_stop_high:
        base = 8.0 if is_yobarazu else 6.0  # 寄らずS高 > 張り付きS高
        # 出来高超過があればさらに加点
        vol_bonus = min(vol - 1.0, 2.0) * 0.3 if not is_yobarazu else 0
        return round(base + vol_bonus, 2)

    # 上ヒゲが強い＆終値が安値圏 → 出尽くし気配
    if upper_wick > 0.5 and close_pos < 0.3:
        return max(0, day_ret * 0.1)

    score = (
        close_pos * 2.0 +                      # 終値がレンジ高値寄りほど高得点（max 2.0）
        max(0, day_ret) * 0.3 +                # 前日比プラス分
        max(0, close_vs_open) * 0.2 +          # 寄り後上昇分
        min(vol - 1.0, 3.0) * 0.3 +            # 出来高超過（上限3倍分）
        (-upper_wick * 1.5)                     # 上ヒゲペナルティ
    )
    return round(max(0.0, score), 2)


def run_endofday_earnings_scan(df_all: "pd.DataFrame") -> list:
    """
    earnings_watchlist.json に保存された銘柄の引け後評価をスキャンする（J-Quants日足使用）。
    EDINETスコア × 引け後スコア で順位付けして返す。

    Args:
        df_all: load_latest_quotes()で取得した全銘柄DataFrame

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

    logger.info(f"引け後スキャン開始: {len(signals)}銘柄（登録日: {watch_date}）")

    results = []
    for s in signals:
        code = s.get("stockCode", "")
        if not code:
            continue

        eod = fetch_endofday_reaction(code, df_all)
        eod_score = _calc_endofday_score(eod)
        edinet_score = s.get("edinetScore", 0)

        if not eod:
            total_score = 0.0
            entry_judgment = "データなし"
        elif eod_score >= 3.0 and edinet_score >= 50:
            entry_judgment = "🔥 翌朝エントリー検討"
        elif eod_score >= 1.5 and edinet_score >= 30:
            entry_judgment = "👀 翌朝様子見"
        elif eod.get("upper_wick_ratio", 0) > 0.5:
            entry_judgment = "⚠️ 上ヒゲ・見送り"
        else:
            entry_judgment = "➡️ 反応薄"

        total_score = round(edinet_score * 0.4 + eod_score * 10 * 0.6, 1)

        results.append({
            **s,
            "eodData": eod,
            "eodScore": eod_score,
            "totalScore": total_score,
            "entryJudgment": entry_judgment,
            "scannedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    results.sort(key=lambda x: x["totalScore"], reverse=True)

    # Claude APIでランキングコメント生成
    comment_map = generate_ranking_comments_batch(results, scan_type="endofday")
    for r in results:
        r["comment"] = comment_map.get(r.get("stockCode", ""), "")

    logger.info(f"引け後スキャン完了: {len(results)}銘柄")
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


def record_earnings_outcomes(df_all: pd.DataFrame) -> tuple[int, list]:
    """
    earnings_signal_log.json の中で「N営業日経過・outcome未記録」のエントリに
    実際のリターンを記録する。

    Returns:
        tuple[int, list]:
          - 更新件数
          - 新規記録したエントリのリスト（Slack通知用）
            各要素: {"stockCode", "companyName", "disclosureDate",
                     "entryPrice", "daysKey", "returnPct", "exitPrice"}
    """
    if not SIGNAL_LOG_PATH.exists():
        return 0, []
    try:
        with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return 0, []

    code_col = "Code" if "Code" in df_all.columns else "code"
    today = pd.Timestamp.now().normalize()
    updated = 0
    newly_recorded = []

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
            newly_recorded.append({
                "stockCode": code,
                "companyName": entry.get("companyName", ""),
                "disclosureDate": disc_date_str,
                "entryPrice": entry_price,
                "daysKey": key,
                "returnPct": ret,
                "exitPrice": exit_price,
                "edinetScore": entry.get("edinetScore", 0),
                "catalystType": entry.get("catalystType", ""),
            })

    if updated > 0:
        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        logger.info(f"決算シグナル結果記録: {updated}件更新")

    return updated, newly_recorded


# ========================================
# 中長期フォローアップ管理
# ========================================

def save_followup_list(scan_results: list, entry_type: str = "intraday") -> None:
    """
    ザラ場/引け後スキャンで良好なスコアを示した銘柄を中長期フォローアップリストに追加する。
    同一銘柄の重複登録は上書き（最新の追加日時を保持）。

    Args:
        scan_results: run_intraday_earnings_scan() or run_endofday_earnings_scan() の戻り値
        entry_type: "intraday" or "endofday"
    """
    targets = [
        r for r in scan_results
        if r.get("totalScore", 0) >= FOLLOWUP_MIN_TOTAL_SCORE
        and "エントリー検討" in r.get("entryJudgment", "")
    ]
    if not targets:
        return

    # 既存リストを読み込む
    followup = _load_followup_list()
    entries = {e["stockCode"]: e for e in followup.get("entries", [])}

    today_str = datetime.now().strftime("%Y-%m-%d")
    for r in targets:
        code = r.get("stockCode", "")
        if not code:
            continue
        # エントリー価格: ザラ場はcurrent_price、引け後はclose_price
        if entry_type == "intraday":
            entry_price = r.get("intradayData", {}).get("current_price", r.get("entryPrice", 0))
        else:
            entry_price = r.get("eodData", {}).get("close_price", r.get("entryPrice", 0))

        entries[code] = {
            "stockCode": code,
            "companyName": r.get("companyName", ""),
            "addedDate": today_str,
            "addedReason": f"決算後{entry_type}急騰（スコア:{r.get('totalScore', 0):.0f}）",
            "edinetScore": r.get("edinetScore", 0),
            "catalystType": r.get("catalystType", ""),
            "summary": r.get("summary", "")[:60],
            "entryPrice": entry_price,
            "entryType": entry_type,
            "followupMaxBdays": FOLLOWUP_MAX_BDAYS,
        }

    followup["entries"] = list(entries.values())
    followup["updatedAt"] = datetime.now().isoformat()

    FOLLOWUP_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FOLLOWUP_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(followup, f, ensure_ascii=False, indent=2)
    logger.info(f"フォローアップリスト更新: {len(entries)}銘柄 → {FOLLOWUP_LIST_PATH}")


def _load_followup_list() -> dict:
    """フォローアップリストを読み込む。"""
    if not FOLLOWUP_LIST_PATH.exists():
        return {"entries": [], "updatedAt": ""}
    try:
        with open(FOLLOWUP_LIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"フォローアップリスト読み込みエラー: {e}")
        return {"entries": [], "updatedAt": ""}


def get_followup_status(df_all: pd.DataFrame) -> list:
    """
    フォローアップリストの銘柄について現在の株価パフォーマンスを返す。
    追加日から FOLLOWUP_MAX_BDAYS 営業日を超えた銘柄はリストから削除する。

    Args:
        df_all: load_latest_quotes()で取得した全銘柄DataFrame

    Returns:
        list: 現在フォロー中の銘柄リスト（return_pct付き）
    """
    followup = _load_followup_list()
    entries = followup.get("entries", [])
    if not entries:
        return []

    code_col = "Code" if "Code" in df_all.columns else "code"
    today = datetime.now().date()
    active = []
    expired_codes = []

    for e in entries:
        code = e.get("stockCode", "")
        added_date_str = e.get("addedDate", "")
        entry_price = e.get("entryPrice", 0)

        if not code or not added_date_str or not entry_price:
            continue

        try:
            added_dt = datetime.strptime(added_date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        # 経過営業日数
        bdays_elapsed = len(pd.bdate_range(
            start=pd.Timestamp(added_dt),
            end=pd.Timestamp(today)
        )) - 1

        if bdays_elapsed > FOLLOWUP_MAX_BDAYS:
            expired_codes.append(code)
            continue

        # 現在株価を取得
        df_stock = (
            df_all[df_all[code_col].astype(str) == code]
            .sort_values("Date")
            .reset_index(drop=True)
        )
        if df_stock.empty:
            current_price = 0.0
            return_pct = None
        else:
            current_price = float(df_stock.iloc[-1]["Close"])
            return_pct = round((current_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else None

        active.append({
            **e,
            "bdays_elapsed": bdays_elapsed,
            "currentPrice": current_price,
            "returnPct": return_pct,
        })

    # 期限切れ銘柄を削除して保存
    if expired_codes:
        followup["entries"] = [e for e in entries if e["stockCode"] not in expired_codes]
        followup["updatedAt"] = datetime.now().isoformat()
        with open(FOLLOWUP_LIST_PATH, "w", encoding="utf-8") as f:
            json.dump(followup, f, ensure_ascii=False, indent=2)
        logger.info(f"フォローアップリスト: {len(expired_codes)}銘柄を期限切れで削除")

    # return_pct の高い順にソート
    active.sort(key=lambda x: (x.get("returnPct") or -999), reverse=True)
    return active


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
