"""
agents/us_market_scanner.py
米国市場のセクター・テーマモメンタムを検出して日本株への波及を分析するモジュール

【処理フロー】
1. yfinanceで主要セクターETF・テーマETFの価格データを取得
2. 5日・20日モメンタムでランキング化
3. 強いセクター上位をClaude API（web_search付き）で分析
   - なぜそのセクターが強いか
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

# セクターETF（SPDRシリーズ中心）
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

# 主要指数（マクロ把握用）
MACRO_INDICES = {
    "SPY":  "S&P500",
    "QQQ":  "NASDAQ100",
    "IWM":  "Russell2000(小型株)",
    "VIX":  "恐怖指数(VIX)",
}

# Claude API設定
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ========================================
# APIキー取得
# ========================================

def _get_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    try:
        import yaml
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            key = config.get("anthropic", {}).get("api_key", "")
            if key and key != "YOUR_ANTHROPIC_API_KEY":
                return key
    except Exception:
        pass
    return ""


# ========================================
# ETFデータ取得・モメンタム計算
# ========================================

def fetch_etf_momentum(tickers: dict, period: str = "3mo") -> list:
    """
    ETFリストの価格データを取得してモメンタムを計算する。

    Args:
        tickers (dict): {ticker: {name, japan_theme}} の辞書
        period (str): 取得期間（yfinance形式）

    Returns:
        list: モメンタムスコア付きETF情報のリスト（スコア降順）
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

            # モメンタム計算
            mom5d  = (latest / float(close.iloc[-6])  - 1) * 100 if len(close) >= 6  else 0.0
            mom20d = (latest / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0.0
            mom60d = (latest / float(close.iloc[-61]) - 1) * 100 if len(close) >= 61 else 0.0

            # 出来高トレンド（5日平均 ÷ 20日平均）
            vol = hist["Volume"]
            vol_trend = (
                float(vol.iloc[-5:].mean()) / float(vol.iloc[-20:].mean())
                if len(vol) >= 20 else 1.0
            )

            # 総合モメンタムスコア（5日重視・出来高加味）
            score = mom5d * 0.5 + mom20d * 0.3 + mom60d * 0.2
            if vol_trend >= 1.3:
                score *= 1.1  # 出来高増加ボーナス

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

            time.sleep(0.3)  # Yahoo Finance レート制限対策

        except Exception as e:
            logger.warning(f"{ticker_sym} データ取得エラー: {e}")
            continue

    # スコア降順でソート
    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"ETFモメンタム計算完了: {len(results)}本")
    return results


def fetch_macro_indices() -> dict:
    """
    S&P500・NASDAQ・VIXなどマクロ指数を取得する。

    Returns:
        dict: {ticker: {name, change5d, latest}} の辞書
    """
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
    強いセクター上位をClaude API（web_search付き）で分析する。

    Args:
        top_sectors (list): モメンタム上位セクターETFリスト
        macro (dict): マクロ指数データ

    Returns:
        dict: {
            "market_summary": str,      # 米市場全体の状況サマリー
            "theme_analysis": list,     # セクターごとの分析
            "japan_opportunities": list, # 日本株への波及テーマ
            "risk_factors": list,       # 注意すべきリスク
        }
    """
    api_key = _get_anthropic_key()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY未設定"}

    # マクロ状況テキスト
    macro_text = "\n".join([
        f"- {v['name']}({k}): {v['latest']:.1f} ({'+' if v['change5d'] >= 0 else ''}{v['change5d']:.1f}%/5日)"
        for k, v in macro.items()
    ])

    # 上位セクターテキスト
    top_text = "\n".join([
        f"- {s['name']}({s['ticker']}): スコア{s['score']:.1f} "
        f"[5日:{'+' if s['mom5d']>=0 else ''}{s['mom5d']:.1f}% / "
        f"20日:{'+' if s['mom20d']>=0 else ''}{s['mom20d']:.1f}%] "
        f"出来高トレンド:{s['vol_trend']:.2f}x"
        for s in top_sectors[:5]
    ])

    today = datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""
あなたは米国株式市場と日本株市場に精通した投資アナリストです。
{today}時点の米国市場データを分析し、日本株モメンタム投資家に有益な情報を提供してください。

【米国マクロ指数（5日騰落）】
{macro_text}

【強いセクターETF上位5本（モメンタムスコア順）】
{top_text}

【出力形式】
必ず以下のJSON形式のみで回答してください。

{{
  "market_summary": "<米市場全体の状況を2〜3文で。リスクオン/オフ、主要テーマを含める>",
  "theme_analysis": [
    {{
      "sector": "<セクター名>",
      "reason": "<なぜ強いか・背景にある材料を1〜2文で>",
      "sustainability": "<short/medium/long・このトレンドの持続性>",
      "japan_stocks": "<具体的な日本株銘柄名を2〜3社>"
    }}
  ],
  "japan_opportunities": ["<日本株で注目すべきテーマ1>", "<テーマ2>", "<テーマ3>"],
  "risk_factors": ["<注意すべきリスク1>", "<リスク2>"]
}}

theme_analysisは強いセクター上位3つのみ記載してください。
"""

    # web_searchツール付きで呼び出す
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search"
            }
        ],
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05"
    }

    try:
        resp = requests.post(CLAUDE_API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()

        data = resp.json()

        # text typeのコンテンツを抽出
        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        raw_text = raw_text.strip()

        # JSON抽出
        if "```" in raw_text:
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        # JSON部分だけ抜き出す
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start >= 0 and end > start:
            raw_text = raw_text[start:end]

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
    """
    米市場モメンタムスキャンを実行してSlack通知用データを返す。

    Returns:
        dict: {
            "sector_ranking": list,  # セクターETFランキング
            "macro": dict,           # マクロ指数
            "analysis": dict,        # Claude分析結果
            "scan_date": str,
        }
    """
    scan_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("米市場スキャン開始...")

    # 1. セクターETFモメンタム取得
    logger.info("セクターETFデータ取得中...")
    sector_ranking = fetch_etf_momentum(SECTOR_ETFS)

    # 2. マクロ指数取得
    logger.info("マクロ指数取得中...")
    macro = fetch_macro_indices()

    # 3. Claude APIで分析（APIキーがある場合のみ）
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

    # スキャン結果をJSONで保存
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
