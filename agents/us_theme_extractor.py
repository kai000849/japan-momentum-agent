"""
agents/us_theme_extractor.py
米国財務メディアのニュースからホットキーワード・テーマを抽出するモジュール

【処理フロー】
1. Yahoo Finance / WSJ / MarketWatch / FT のRSSフィードからニュースタイトルを収集
2. Claude APIに渡して（RSSデータのみ・web_searchなし）：
   - 今日の米市場で注目されているキーワードを抽出・ランキング
   - セクター別に細分化（例：半導体→光インターコネクト、HBM等）
   - 日本株への波及銘柄を特定
3. Slackに通知

作者: Japan Momentum Agent
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ========================================
# RSSフィード定義
# ========================================

RSS_FEEDS = [
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rssindex",
        "weight": 3,
    },
    {
        "name": "WSJ Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "weight": 3,
    },
    {
        "name": "MarketWatch",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "weight": 2,
    },
    {
        "name": "Financial Times",
        "url": "https://www.ft.com/markets?format=rss",
        "weight": 2,
    },
    {
        "name": "Investing.com",
        "url": "https://www.investing.com/rss/news_25.rss",
        "weight": 1,
    },
]

# Claude API設定
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# キャッシュ保存先
CACHE_DIR = Path(__file__).parent.parent / "data" / "raw" / "us_news"


# ========================================
# APIキー取得
# ========================================

from agents.utils import get_anthropic_key as _get_anthropic_key


# ========================================
# RSSフィード収集
# ========================================

def fetch_rss_headlines() -> list:
    """
    複数のRSSフィードからニュースタイトルを収集する。

    Returns:
        list: [{"title": str, "source": str, "weight": int}] のリスト
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    headlines = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for feed in RSS_FEEDS:
        try:
            resp = requests.get(feed["url"], timeout=15, headers=headers)
            resp.raise_for_status()

            if len(resp.content) < 500:
                logger.warning(f"{feed['name']}: レスポンスが空です")
                continue

            titles = re.findall(
                r'<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>',
                resp.text
            )

            count = 0
            for t in titles[1:]:
                title = (t[0] or t[1]).strip()
                title = title.replace("&#x2019;", "'").replace("&amp;", "&").replace("&quot;", '"')
                title = title.replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")

                if title and len(title) > 10:
                    headlines.append({
                        "title": title,
                        "source": feed["name"],
                        "weight": feed["weight"]
                    })
                    count += 1

            logger.info(f"{feed['name']}: {count}件取得")
            time.sleep(0.5)

        except Exception as e:
            logger.warning(f"{feed['name']} 取得エラー: {e}")
            continue

    logger.info(f"RSS収集完了: 合計{len(headlines)}件")
    return headlines


# ========================================
# Claude APIでキーワード・テーマ抽出
# ========================================

def extract_hot_keywords(headlines: list) -> dict:
    """
    収集したニュースヘッドラインからClaude APIでホットキーワードを抽出。

    Args:
        headlines (list): fetch_rss_headlines()の戻り値

    Returns:
        dict: {
            "hot_keywords": [...],
            "sector_narratives": [...],
            "japan_plays": [...],
            "macro_narrative": str,
            "risk_keywords": [...],
        }
    """
    api_key = _get_anthropic_key()
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY未設定 - キーワード抽出をスキップ")
        return {"error": "APIキー未設定"}

    if not headlines:
        return {"error": "ニュースデータなし"}

    # 80→40件に削減（inputトークン節約）
    headline_text = ""
    for h in headlines[:40]:
        prefix = "★" * h["weight"]
        headline_text += f"{prefix} [{h['source']}] {h['title']}\n"

    today = datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""
あなたは米国株式市場を専門とする投資アナリストです。
{today}の米国金融メディアのニュースヘッドライン（★が多いほど重要なソース）を分析し、
日本株モメンタム投資家に役立つキーワード・テーマを抽出してください。

【ニュースヘッドライン】
{headline_text}

【重要な指示】
- 「AI」「半導体」などの大きすぎるカテゴリではなく、
  「光インターコネクト」「HBM4」「原子力データセンター」「GLP-1薬」のような
  具体的・細分化されたキーワードを優先して抽出してください
- 米国での注目テーマが日本株のどの銘柄に波及するかを具体的に示してください

【出力形式】
必ず以下のJSON形式のみで回答してください。前置き・説明不要。

{{
  "hot_keywords": [
    {{
      "keyword": "<具体的なキーワード>",
      "mention_level": "<high/medium/low>",
      "context": "<なぜ今注目されているか1文で>",
      "sector": "<関連セクター>"
    }}
  ],
  "sector_narratives": [
    {{
      "sector": "<大セクター名>",
      "sub_theme": "<具体的なサブテーマ>",
      "detail": "<今起きていることを2文で>",
      "momentum": "<rising/stable/falling>"
    }}
  ],
  "japan_plays": [
    {{
      "theme": "<テーマ名>",
      "stocks": ["<日本株銘柄名1>", "<日本株銘柄名2>", "<日本株銘柄名3>"],
      "reason": "<なぜこの銘柄に波及するか1文で>"
    }}
  ],
  "macro_narrative": "<マクロ環境を2文で>",
  "risk_keywords": ["<リスクワード1>", "<リスクワード2>", "<リスクワード3>"]
}}

hot_keywordsは上位8件、sector_narrativesは上位5件、japan_playsは上位5件を出力してください。
"""

    try:
        import anthropic

        # web_searchなし・シンプルな1回呼び出し（コスト削減）
        logger.info("Claude APIでキーワード抽出中（web_searchなし・RSS情報のみ）...")
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,  # 4000→1500に削減
            messages=[{"role": "user", "content": prompt}]
        )

        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text
        raw_text = raw_text.strip()

        raw_text = re.sub(r'<cite[^>]*>', '', raw_text)
        raw_text = raw_text.replace('</cite>', '')
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
        logger.info(f"キーワード抽出完了: {len(result.get('hot_keywords', []))}件")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSONパースエラー: {e}\n生テキスト: {raw_text[:400]}")
        return {"error": "JSONパースエラー", "raw": raw_text[:300]}
    except Exception as e:
        logger.error(f"Claude API エラー: {e}")
        return {"error": str(e)}


# ========================================
# メイン：テーマ抽出スキャン実行
# ========================================

def run_theme_extraction() -> dict:
    """
    ニュース収集〜キーワード抽出を一括実行する。

    Returns:
        dict: {
            "headlines": list,
            "keywords": dict,
            "scan_date": str,
            "headline_count": int,
        }
    """
    scan_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("米市場テーマ抽出スキャン開始...")

    headlines = fetch_rss_headlines()

    keywords = {}
    if headlines:
        keywords = extract_hot_keywords(headlines)
    else:
        logger.warning("ニュース収集ゼロ件 - スキップ")

    result = {
        "headlines": headlines,
        "keywords": keywords,
        "scan_date": scan_date,
        "headline_count": len(headlines),
    }

    _save_theme_result(result)
    return result


def _save_theme_result(result: dict) -> None:
    save_dir = Path(__file__).parent.parent / "data" / "processed" / "us_themes"
    save_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    save_path = save_dir / f"theme_{date_str}.json"

    save_data = {k: v for k, v in result.items() if k != "headlines"}
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    logger.info(f"テーマ抽出結果を保存: {save_path}")
