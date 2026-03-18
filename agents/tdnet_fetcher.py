"""
agents/tdnet_fetcher.py
東証 TDnet（適時開示情報閲覧サービス）から当日の開示情報を取得するモジュール

【処理フロー】
1. https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html をページ送りしながら取得
2. HTML を regex でパースして (証券コード, 会社名, 開示タイトル, 時刻) を抽出
3. 結果を data/raw/tdnet/ にキャッシュ（同日再取得を防ぐ）

【コード変換】
TDnet は 5桁コード（例: "80110"）を使用。末尾の "0" を除いた 4桁が J-Quants と同じ証券コード。

作者: Japan Momentum Agent
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TDNET_BASE = "https://www.release.tdnet.info/inbs"
CACHE_DIR = Path(__file__).parent.parent / "data" / "raw" / "tdnet"
MAX_PAGES = 20  # 1日最大 20ページ × 約25件 = 約500件をカバー

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

# 急騰原因として重要度が高い開示キーワード（タイトルに含まれると優先表示）
HIGH_VALUE_KEYWORDS = [
    "業績", "上方修正", "下方修正", "増益", "増収", "黒字", "赤字",
    "配当", "自己株", "自社株", "買付", "MBO", "TOB",
    "合併", "買収", "子会社化", "資本業務提携", "提携",
    "契約", "受注", "採択", "認可", "承認",
    "決算", "四半期", "通期", "修正",
]


def _normalize_code(raw_code: str) -> str:
    """TDnet の5桁コードを J-Quants の4桁証券コードに変換する。"""
    code = raw_code.strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code[:4] if len(code) >= 4 else code


def _parse_tdnet_html(html: str) -> list:
    """
    TDnet の開示一覧 HTML をパースして開示リストを返す。

    Returns:
        list of dict: [{code, company, title, time, is_high_value}]
    """
    pattern = re.compile(
        r'<td[^>]*kjTime[^>]*>\s*([^<]+?)\s*</td>.*?'
        r'<td[^>]*kjCode[^>]*>\s*([^<]+?)\s*</td>.*?'
        r'<td[^>]*kjName[^>]*>\s*([^<]+?)\s*</td>.*?'
        r'<td[^>]*kjTitle[^>]*><a[^>]*href="([^"]*)"[^>]*>\s*([^<]+?)\s*</a>',
        re.DOTALL
    )

    results = []
    for m in pattern.finditer(html):
        disc_time = m.group(1).strip()
        raw_code = m.group(2).strip()
        company = m.group(3).strip()
        pdf_href = m.group(4).strip()
        title = m.group(5).strip()

        # HTML エンティティ変換
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        title = title.replace("&quot;", '"').replace("&#39;", "'")
        company = company.replace("&amp;", "&")

        code = _normalize_code(raw_code)
        if not code or not title:
            continue

        pdf_url = f"{TDNET_BASE}/{pdf_href}" if pdf_href and not pdf_href.startswith("http") else pdf_href
        is_high = any(kw in title for kw in HIGH_VALUE_KEYWORDS)

        results.append({
            "code": code,
            "company": company,
            "title": title,
            "time": disc_time,
            "pdf_url": pdf_url,
            "is_high_value": is_high,
        })

    return results


def fetch_disclosures(date_str: str, use_cache: bool = True) -> list:
    """
    指定日の全 TDnet 開示情報を取得する。

    Args:
        date_str: "YYYY-MM-DD" 形式
        use_cache: True の場合、当日キャッシュがあれば再取得をスキップ

    Returns:
        list of dict: [{code, company, title, time, pdf_url, is_high_value}]
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    date_compact = date_str.replace("-", "")
    cache_path = CACHE_DIR / f"tdnet_{date_compact}.json"

    if use_cache and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info(f"TDnet キャッシュ使用: {date_str} ({len(cached)}件)")
            return cached
        except Exception:
            pass

    all_disclosures = []
    for page in range(1, MAX_PAGES + 1):
        page_str = str(page).zfill(3)
        url = f"{TDNET_BASE}/I_list_{page_str}_{date_compact}.html"
        try:
            resp = requests.get(url, timeout=10, headers=HEADERS)
            if resp.status_code != 200:
                break
            resp.encoding = "UTF-8"
            items = _parse_tdnet_html(resp.text)
            if not items:
                break
            all_disclosures.extend(items)
            logger.debug(f"TDnet page {page}: {len(items)}件")
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"TDnet ページ{page} 取得エラー: {e}")
            break

    if all_disclosures:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(all_disclosures, f, ensure_ascii=False, indent=2)
        logger.info(f"TDnet 取得完了: {date_str} 計{len(all_disclosures)}件 → キャッシュ保存")
    else:
        logger.info(f"TDnet: {date_str} の開示なし（休場日・週末の可能性）")

    return all_disclosures


def get_disclosures_for_stock(
    stock_code: str,
    surge_date: str,
    look_back_days: int = 2,
) -> list:
    """
    急騰銘柄に関連する TDnet 開示を取得する。
    surge_date 当日 + 直前 look_back_days 日分を検索する。

    Args:
        stock_code: 4桁証券コード
        surge_date: "YYYY-MM-DD" 形式
        look_back_days: 何日前まで遡るか（デフォルト2日）

    Returns:
        list of dict: 関連開示のリスト（重要度降順）
    """
    base_dt = datetime.strptime(surge_date, "%Y-%m-%d")
    all_matches = []

    for delta in range(look_back_days + 1):
        check_dt = base_dt - timedelta(days=delta)
        # 週末は TDnet 開示なし → スキップ
        if check_dt.weekday() >= 5:
            continue
        date_str = check_dt.strftime("%Y-%m-%d")
        disclosures = fetch_disclosures(date_str)
        for d in disclosures:
            if d["code"] == stock_code:
                all_matches.append({**d, "disclosure_date": date_str})

    # 重要度の高い開示を先頭に
    all_matches.sort(key=lambda x: (not x["is_high_value"], x["disclosure_date"]))
    return all_matches
