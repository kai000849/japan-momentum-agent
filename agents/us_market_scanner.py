"""
agents/us_market_scanner.py
米国市場のセクター・テーマモメンタムを検出して日本株への波及を分析するモジュール

【処理フロー】
1. yfinanceで主要セクターETF・テーマETFの価格データを取得
2. 5日・20日モメンタムでランキング化
3. 強いセクター上位をClaude API（web_searchなし）で分析
   - ETF数値データをもとに、なぜそのセクターが強いか
   - 関連する日本株テーマ・具体的銘柄
4. Slackにランキング＋分析コメントを通知

作者: Japan Momentum Agent
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ========================================
# 監視対象ETF定義
# ========================================

SECTOR_ETFS = {
    "XLK":  {"name": "テクノロジー",       "japan_theme": "半導体・IT・ソフトウェア"},
    "SOXX": {"name": "半導体",             "japan_theme": "東京エレク・アドバンテスト・レーザーテク"},
    "XLV":  {"name": "ヘルスケア",         "japan_theme": "医薬品・医療機器・CRO"},
    "XLE":  {"name": "エネルギー",         "japan_theme": "石油・資源・商社"},
    "XLF":  {"name": "金融",              "japan_theme": "銀行・保険・証券"},
    "XLI":  {"name": "資本財",            "japan_theme": "機械・プラント・建設機械"},
    "XLB":  {"name": "素材",              "japan_theme": "化学・鉄鋼・非鉄金属"},
    "XLU":  {"name": "公益",              "japan_theme": "電力・ガス"},
    "XLRE": {"name": "不動産(REIT)",       "japan_theme": "J-REIT・不動産"},
    "XLP":  {"name": "生活必需品",         "japan_theme": "食品・飲料・小売"},
    "XLY":  {"name": "一般消費財",         "japan_theme": "自動車・小売・外食"},
    "ITA":  {"name": "防衛・航空宇宙",      "japan_theme": "三菱重工・IHI・川崎重工"},
    "XBI":  {"name": "バイオテク",         "japan_theme": "バイオ・創薬・医療"},
    "ARKK": {"name": "破壊的イノベーション", "japan_theme": "AI・ロボット・フィンテック"},
    "ICLN": {"name": "クリーンエネルギー",  "japan_theme": "再エネ・水素・蓄電池"},
    "AIQ":  {"name": "AI・ビッグデータ",    "japan_theme": "AI関連・データセンター・GPU"},
    "ROBO": {"name": "ロボティクス",        "japan_theme": "ファナック・安川電機・キーエンス"},
}

MACRO_INDICES = {
    "SPY":  "S&P500",
    "QQQ":  "NASDAQ100",
    "IWM":  "Russell2000(小型株)",
    "VIX":  "恐怖指数(VIX)",
}

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ========================================
# APIキー取得
# ========================================

from agents.utils import get_anthropic_key as _get_anthropic_key


# ========================================
# ETFデータ取得・モメンタム計算
# ========================================

def fetch_etf_momentum(tickers: dict, period: str = "3mo") -> list:
    """
    ETFリストの価格データを取得してモメンタムを計算する。
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinanceがインストールされていません。pip install yfinance を実行してください。")
        return []

    results = []

    for ticker_sym, info in tickers.items():
        try:
            ticker = yf.Ticker(ticker_sym)
            hist = ticker.history(period=period)

            if hist.empty or len(hist) < 6:
                logger.warning(f"{ticker_sym}: データ不足のためスキップ")
                continue

            close = hist["Close"]
            latest = float(close.iloc[-1])

            mom5d  = (latest / float(close.iloc[-6])  - 1) * 100 if len(close) >= 6  else 0.0
            mom20d = (latest / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0.0
            mom60d = (latest / float(close.iloc[-61]) - 1) * 100 if len(close) >= 61 else 0.0

            vol = hist["Volume"]
            vol_trend = (
                float(vol.iloc[-5:].mean()) / float(vol.iloc[-20:].mean())
                if len(vol) >= 20 else 1.0
            )

            score = mom5d * 0.5 + mom20d * 0.3 + mom60d * 0.2
            if vol_trend >= 1.3:
                score *= 1.1

            results.append({
                "ticker": ticker_sym,
                "name": info["name"],
                "japan_theme": info["japan_theme"],
                "latest_price": latest,
                "mom5d": round(mom5d, 2),
                "mom20d": round(mom20d, 2),
                "mom60d": round(mom60d, 2),
                "vol_trend": round(vol_trend, 2),
                "score": round(score, 2),
            })

            time.sleep(0.3)

        except Exception as e:
            logger.warning(f"{ticker_sym} データ取得エラー: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"ETFモメンタム計算完了: {len(results)}本")
    return results


def fetch_macro_indices() -> dict:
    """S&P500・NASDAQ・VIXなどマクロ指数を取得する。"""
    try:
        import yfinance as yf
    except ImportError:
        return {}

    macro = {}
    for ticker_sym, name in MACRO_INDICES.items():
        try:
            ticker = yf.Ticker(ticker_sym)
            hist = ticker.history(period="1mo")
            if hist.empty or len(hist) < 2:
                continue
            close = hist["Close"]
            latest = float(close.iloc[-1])
            prev5 = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
            change5d = (latest / prev5 - 1) * 100
            macro[ticker_sym] = {
                "name": name,
                "latest": round(latest, 2),
                "change5d": round(change5d, 2),
            }
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"{ticker_sym} マクロ取得エラー: {e}")

    return macro


# ========================================
# Claude APIで米市場テーマを分析
# ========================================

def analyze_us_market_theme(top_sectors: list, macro: dict) -> dict:
    """
    強いセクター上位をClaude APIで分析する。
    ETFの数値データのみを渡す（web_searchなし・コスト削減）。
    """
    api_key = _get_anthropic_key()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY未設定"}

    macro_text = "\n".join([
        f"- {v['name']}({k}): {v['latest']:.1f} ({'+' if v['change5d'] >= 0 else ''}{v['change5d']:.1f}%/5日)"
        for k, v in macro.items()
    ])

    top_text = "\n".join([
        f"- {s['name']}({s['ticker']}): スコア{s['score']:.1f} "
        f"[5日:{'+' if s['mom5d']>=0 else ''}{s['mom5d']:.1f}% / "
        f"20日:{'+' if s['mom20d']>=0 else ''}{s['mom20d']:.1f}%] "
        f"出来高:{s['vol_trend']:.2f}x / 日本株関連:{s['japan_theme']}"
        for s in top_sectors[:5]
    ])

    today = datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""あなたは米国株式市場と日本株市場に精通した投資アナリストです。
{today}時点の米国市場ETFデータを分析し、日本株モメンタム投資家に有益な情報を提供してください。

【米国マクロ指数（5日騰落）】
{macro_text}

【強いセクターETF上位5本（モメンタムスコア順）】
{top_text}

【出力形式】
必ず以下のJSON形式のみで回答してください。前置き不要。

{{
  "market_summary": "<米市場全体の状況を2文で。リスクオン/オフ・主要テーマを含める>",
  "theme_analysis": [
    {{
      "sector": "<セクター名>",
      "reason": "<なぜ強いか・背景を1文で>",
      "sustainability": "<short/medium/long>",
      "japan_stocks": "<具体的な日本株銘柄名を2〜3社>"
    }}
  ],
  "japan_opportunities": ["<日本株で注目すべきテーマ1>", "<テーマ2>", "<テーマ3>"],
  "risk_factors": ["<注意すべきリスク1>", "<リスク2>"]
}}

theme_analysisは強いセクター上位3つのみ記載してください。"""

    try:
        import anthropic

        # web_searchなし・1回呼び出し（コスト削減）
        logger.info("Claude APIで米市場テーマ分析中（web_searchなし）...")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,  # 2000→1200に削減
            messages=[{"role": "user", "content": prompt}]
        )

        import re
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text
        raw_text = raw_text.strip()
        raw_text = re.sub(r'```(?:json)?', '', raw_text)

        depth = 0
        best_s = best_e = cur_s = 0
        in_str = esc = False
        for k, ch in enumerate(raw_text):
            if esc:
                esc = False; continue
            if ch == '\\' and in_str:
                esc = True; continue
            if ch == '"':
                in_str = not in_str; continue
            if in_str:
                continue
            if ch == '{':
                if depth == 0:
                    cur_s = k
                depth += 1
            elif ch == '}' and depth > 0:
                depth -= 1
                if depth == 0 and k + 1 - cur_s > best_e - best_s:
                    best_s, best_e = cur_s, k + 1
        if best_e > best_s:
            raw_text = raw_text[best_s:best_e]

        result = json.loads(raw_text)
        logger.info("米市場テーマ分析完了")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSONパースエラー: {e}\n生テキスト: {raw_text[:300]}")
        return {"error": f"JSONパースエラー: {e}", "market_summary": raw_text[:200]}
    except Exception as e:
        logger.error(f"Claude API呼び出しエラー: {e}")
        return {"error": str(e)}


# ========================================
# メイン：米市場スキャン実行
# ========================================

def run_us_market_scan() -> dict:
    """米市場モメンタムスキャンを実行してSlack通知用データを返す。"""
    scan_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("米市場スキャン開始...")

    logger.info("セクターETFデータ取得中...")
    sector_ranking = fetch_etf_momentum(SECTOR_ETFS)

    logger.info("マクロ指数取得中...")
    macro = fetch_macro_indices()

    analysis = {}
    api_key = _get_anthropic_key()
    if api_key and sector_ranking:
        logger.info("Claude APIでテーマ分析中...")
        analysis = analyze_us_market_theme(sector_ranking, macro)
    else:
        logger.info("APIキー未設定のためETFデータのみ返します")

    result = {
        "sector_ranking": sector_ranking,
        "macro": macro,
        "analysis": analysis,
        "scan_date": scan_date,
    }

    _save_us_scan_result(result)
    return result


def _save_us_scan_result(result: dict) -> None:
    """スキャン結果をJSONファイルに保存する。"""
    save_dir = Path(__file__).parent.parent / "data" / "processed" / "us_scans"
    save_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    save_path = save_dir / f"us_scan_{date_str}.json"
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"米市場スキャン結果を保存: {save_path}")
