"""
agents/us_theme_extractor.py
米国財務メディアのニュースからホットキーワード・テーマを抽出するモジュール

【処理フロー】
1. Yahoo Finance / WSJ / MarketWatch / FT のRSSフィードからニュースタイトルを収集
2. Claude API（web_search付き）に渡して：
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
        "weight": 3,  # 重要度（キーワード重み付けに使用）
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

            # タイトルをXMLから抽出（CDATA対応）
            titles = re.findall(
                r'<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>',
                resp.text
            )

            count = 0
            for t in titles[1:]:  # 最初のタイトルはフィード名なのでスキップ
                title = (t[0] or t[1]).strip()
                # HTMLエンティティのデコード
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
    web_searchも併用して最新情報を補完する。

    Args:
        headlines (list): fetch_rss_headlines()の戻り値

    Returns:
        dict: {
            "hot_keywords": [{"keyword": str, "count": int, "context": str, "sector": str}],
            "sector_narratives": [{"sector": str, "sub_theme": str, "detail": str}],
            "japan_plays": [{"theme": str, "stocks": [str], "reason": str}],
            "macro_narrative": str,
            "risk_keywords": [str],
        }
    """
    api_key = _get_anthropic_key()
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY未設定 - キーワード抽出をスキップ")
        return {"error": "APIキー未設定"}

    if not headlines:
        return {"error": "ニュースデータなし"}

    # 重み付きでタイトルをテキスト化（重要ソースは複数回含める）
    headline_text = ""
    for h in headlines[:80]:  # 最大80件
        prefix = "★" * h["weight"]  # 重要度を★で表現
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
- 不確かな情報は書かず、ヘッドラインから明確に読み取れることのみ記載してください

【出力形式】
必ず以下のJSON形式のみで回答してください。前置き・説明不要。

{{
  "hot_keywords": [
    {{
      "keyword": "<具体的なキーワード（英語または日本語）>",
      "mention_level": "<high/medium/low・ヘッドラインでの言及度>",
      "context": "<なぜ今注目されているか1文で>",
      "sector": "<関連セクター>"
    }}
  ],
  "sector_narratives": [
    {{
      "sector": "<大セクター名>",
      "sub_theme": "<具体的なサブテーマ・細分化キーワード>",
      "detail": "<今起きていることを2文で詳しく説明>",
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
  "macro_narrative": "<マクロ環境を2文で。リスクオン/オフ、金利、ドル動向等>",
  "risk_keywords": ["<注意すべきリスクワード1>", "<リスクワード2>", "<リスクワード3>"]
}}

hot_keywordsは上位8件、sector_narrativesは上位5件、japan_playsは上位5件を出力してください。
"""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
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
        logger.info("Claude APIでキーワード抽出中（web_search有効）...")
        resp = requests.post(CLAUDE_API_URL, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()

        data = resp.json()

        # textブロックを全て結合
        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        raw_text = raw_text.strip()

        # JSON部分だけ抽出
        if "```" in raw_text:
            parts = raw_text.split("```")
            for part in parts:
                if part.startswith("json"):
                    raw_text = part[4:].strip()
                    break

        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start >= 0 and end > start:
            raw_text = raw_text[start:end]

        result = json.loads(raw_text)
        logger.info(f"キーワード抽出完了: {len(result.get('hot_keywords', []))}件")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSONパースエラー: {e}\n生テキスト: {raw_text[:400]}")
        return {"error": f"JSONパースエラー", "raw": raw_text[:300]}
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

    # 1. RSS収集
    headlines = fetch_rss_headlines()

    # 2. Claude APIでキーワード抽出
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

    # 保存
    _save_theme_result(result)
    return result


def _save_theme_result(result: dict) -> None:
    save_dir = Path(__file__).parent.parent / "data" / "processed" / "us_themes"
    save_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    save_path = save_dir / f"theme_{date_str}.json"

    # headlinesは保存容量節約のため省略
    save_data = {k: v for k, v in result.items() if k != "headlines"}
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    logger.info(f"テーマ抽出結果を保存: {save_path}")
