"""
agents/investment_advisor.py
フェーズ4: 投資判断エージェント

qualify結果・PF・ポートフォリオ余力・米市場シグナルを統合して
「エントリー推奨 / 見送り推奨」をSlack通知する。

かいさんが夕方18時に通知を見て5分で Go/No-Go を決断できることを目的とする。

判断ロジック（ルールベース）:
  推奨エントリー条件（全て満たす）:
    1. qualifyResult == "STRONG"
    2. PF >= 1.2
    3. ポートフォリオ余力あり（保有数 < 最大枠）
  加点要素（Slackに表示するだけ）:
    - 関連セクターETFが上昇中（米市場スキャン結果）
    - 過去のSTRONG勝率が60%以上（qualify_log蓄積後）

作者: Japan Momentum Agent
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ファイルパス
BASE_DIR = Path(__file__).parent.parent
QUALIFY_LOG_PATH = BASE_DIR / "memory" / "qualify_log.json"
TRADE_LOG_PATH = BASE_DIR / "memory" / "trade_log.json"
US_SCAN_DIR = BASE_DIR / "data" / "processed" / "us_scans"
US_THEME_DIR = BASE_DIR / "data" / "processed" / "us_themes"

# エントリー推奨の閾値
MIN_PF = 1.2           # 最低プロフィットファクター
MAX_POSITIONS = 10     # 最大同時保有数
MAX_INVEST_PER_STOCK = 500000  # 1銘柄あたり最大投資額（円）


# ========================================
# 内部ユーティリティ
# ========================================

def _load_json(path: Path) -> dict | list:
    """JSONファイルを読み込む。失敗時はデフォルト値を返す。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"JSONロードエラー {path}: {e}")
        return {}


def _get_portfolio_status() -> dict:
    """
    trade_log.jsonからポートフォリオ余力を取得する。

    Returns:
        dict: {
            "positions": int,       # 現在の保有銘柄数
            "max_positions": int,   # 最大枠
            "has_capacity": bool,   # 余力があるか
            "cash_available": float # 概算余力金額
        }
    """
    trade_log = _load_json(TRADE_LOG_PATH)
    positions = trade_log.get("positions", [])
    pos_count = len(positions)
    has_capacity = pos_count < MAX_POSITIONS

    # 概算余力（初期資金3,000,000 - 投資済み金額）
    invested = sum(p.get("investedAmount", 0) for p in positions)
    cash_available = max(0, 3_000_000 - invested)

    return {
        "positions": pos_count,
        "max_positions": MAX_POSITIONS,
        "has_capacity": has_capacity,
        "cash_available": cash_available,
    }


def _get_latest_qualify_results() -> list:
    """
    qualify_log.jsonから最新の判定結果（直近スキャン日分）を取得する。
    """
    entries = _load_json(QUALIFY_LOG_PATH)
    if not isinstance(entries, list) or not entries:
        return []

    # 最新のscanDateを特定
    latest_date = max(e.get("scanDate", "") for e in entries)
    return [e for e in entries if e.get("scanDate") == latest_date]


def _get_strong_win_rate() -> Optional[float]:
    """
    qualify_logのSTRONG実績勝率を返す（5件未満ならNone）。
    """
    entries = _load_json(QUALIFY_LOG_PATH)
    if not isinstance(entries, list):
        return None

    recorded = [
        e for e in entries
        if e.get("qualifyResult") == "STRONG"
        and e.get("outcome", {}) is not None
        and e.get("outcome", {}).get("status") == "recorded"
    ]
    if len(recorded) < 5:
        return None

    wins = sum(1 for e in recorded if e.get("outcome", {}).get("isWin", False))
    return round(wins / len(recorded) * 100, 1)


def _get_us_sector_context() -> dict:
    """
    直近の米市場スキャン結果から上昇セクターTOP3を取得する。

    Returns:
        dict: {
            "top_sectors": list[str],   # 上昇中セクター名リスト
            "scan_date": str,
            "available": bool
        }
    """
    if not US_SCAN_DIR.exists():
        return {"top_sectors": [], "scan_date": "", "available": False}

    # 最新ファイルを取得
    files = sorted(US_SCAN_DIR.glob("*.json"))
    if not files:
        return {"top_sectors": [], "scan_date": "", "available": False}

    latest = _load_json(files[-1])
    ranking = latest.get("sector_ranking", [])
    top_sectors = [
        s["name"] for s in ranking
        if s.get("score", 0) > 0
    ][:3]
    scan_date = latest.get("scan_date", files[-1].stem[:10])

    return {
        "top_sectors": top_sectors,
        "scan_date": scan_date,
        "available": True
    }


def _get_us_hot_keywords() -> list:
    """
    直近の米テーマ抽出結果からホットキーワードを取得する。
    """
    if not US_THEME_DIR.exists():
        return []

    files = sorted(US_THEME_DIR.glob("*.json"))
    if not files:
        return []

    latest = _load_json(files[-1])
    keywords = latest.get("keywords", {}).get("hot_keywords", [])
    return [k.get("keyword", k) if isinstance(k, dict) else k for k in keywords[:5]]


# ========================================
# セクターマッピング（銘柄→関連ETF/セクター）
# ========================================

# 簡易マッピング。銘柄名・業種からキーワードを拾う
SECTOR_KEYWORDS = {
    "ゲーム": ["Gaming", "Consumer Discretionary", "Entertainment"],
    "半導体": ["Semiconductors", "Technology", "AI"],
    "建設": ["Industrials", "Infrastructure", "Construction"],
    "医薬": ["Healthcare", "Biotech", "Pharmaceuticals"],
    "エネルギー": ["Energy", "Clean Energy", "Utilities"],
    "IT": ["Technology", "AI", "Cloud", "Software"],
    "自動車": ["Industrials", "EV", "Consumer Discretionary"],
    "金融": ["Financials", "Banks"],
    "小売": ["Consumer Discretionary", "Retail"],
    "ディスプレイ": ["Technology", "Semiconductors"],
}

def _infer_sector_match(company_name: str, top_sectors: list) -> Optional[str]:
    """
    会社名から関連セクターを推定し、上昇セクターと一致するか確認する。
    一致したセクター名を返す（なければNone）。
    """
    name = company_name
    for jp_sector, en_keywords in SECTOR_KEYWORDS.items():
        if jp_sector in name:
            for kw in en_keywords:
                for top in top_sectors:
                    if kw.lower() in top.lower():
                        return top
    return None


# ========================================
# メイン: 投資判断生成
# ========================================

def generate_advice(qualify_results: list, pf_map: dict) -> list:
    """
    qualify結果とPFから投資判断を生成する。

    Args:
        qualify_results: qualify_signals()の戻り値（STRONGを含む銘柄リスト）
        pf_map: {"SHORT_TERM": 1.8, "MOMENTUM": 2.1} 形式のPF辞書

    Returns:
        list: 各銘柄の投資判断辞書リスト
        [
            {
                "stockCode": str,
                "companyName": str,
                "qualifyResult": str,
                "recommendation": "ENTRY" | "WATCH" | "SKIP",
                "reasons": list[str],    # 推奨理由
                "cautions": list[str],   # 注意点
                "entryPrice": float,     # エントリー想定価格
                "stopLoss": float,
                "takeProfit": float,
                "investAmount": float,
            }
        ]
    """
    portfolio = _get_portfolio_status()
    us_context = _get_us_sector_context()
    hot_keywords = _get_us_hot_keywords()
    strong_wr = _get_strong_win_rate()

    advices = []

    for result in qualify_results:
        stock_code = result.get("stockCode", "")
        company_name = result.get("companyName", "")
        qualify_result = result.get("qualifyResult", "NOISE")
        mode = result.get("mode", "SHORT_TERM")
        current_price = result.get("close", 0)
        pf = pf_map.get(mode, 0)

        reasons = []
        cautions = []

        # ---- STRONG/WATCH/WEAK/NOISE 判定 ----
        if qualify_result == "STRONG":
            reasons.append(f"✅ STRONG判定（{result.get('stage2', {}).get('comment', '')}）")
        elif qualify_result == "WATCH":
            cautions.append("⚠️ WATCH（Claude判定未実行）")
        elif qualify_result == "WEAK":
            cautions.append(f"❌ WEAK（{result.get('stage2', {}).get('comment', '')}）")
        else:
            cautions.append("❌ NOISE（ステージ1不通過）")

        # ---- PF チェック ----
        if pf >= MIN_PF:
            reasons.append(f"✅ PF {pf:.1f}（バックテスト良好）")
        elif pf > 0:
            cautions.append(f"⚠️ PF {pf:.1f}（基準値{MIN_PF}未満）")
        else:
            cautions.append("⚠️ PF未計算")

        # ---- ポートフォリオ余力チェック ----
        if portfolio["has_capacity"]:
            reasons.append(
                f"✅ PF余力あり（{portfolio['positions']}/{portfolio['max_positions']}枠使用中）"
            )
        else:
            cautions.append(
                f"❌ PF満枠（{portfolio['positions']}/{portfolio['max_positions']}）"
            )

        # ---- 米市場シグナルチェック ----
        if us_context["available"] and us_context["top_sectors"]:
            matched = _infer_sector_match(company_name, us_context["top_sectors"])
            if matched:
                reasons.append(f"✅ 関連セクター上昇中（{matched}）")
            else:
                pass  # 不一致は特に減点しない

        # ---- STRONG過去勝率チェック ----
        if strong_wr is not None:
            if strong_wr >= 60:
                reasons.append(f"✅ STRONG実績勝率 {strong_wr:.0f}%")
            else:
                cautions.append(f"⚠️ STRONG実績勝率 {strong_wr:.0f}%（参考値）")

        # ---- 総合推奨判定 ----
        is_strong = qualify_result == "STRONG"
        pf_ok = pf >= MIN_PF
        has_capacity = portfolio["has_capacity"]

        if is_strong and pf_ok and has_capacity:
            recommendation = "ENTRY"
        elif is_strong and (not pf_ok or not has_capacity):
            recommendation = "WATCH"
        else:
            recommendation = "SKIP"

        # ---- エントリー価格の計算 ----
        # 翌営業日の始値想定（現在終値の+0.5%を想定始値として使用）
        entry_price = round(current_price * 1.005) if current_price > 0 else 0
        stop_loss = round(entry_price * 0.95) if entry_price > 0 else 0
        take_profit = round(entry_price * 1.15) if entry_price > 0 else 0
        invest_amount = min(MAX_INVEST_PER_STOCK, portfolio["cash_available"])

        advices.append({
            "stockCode": stock_code,
            "companyName": company_name,
            "qualifyResult": qualify_result,
            "mode": mode,
            "recommendation": recommendation,
            "reasons": reasons,
            "cautions": cautions,
            "currentPrice": current_price,
            "entryPrice": entry_price,
            "stopLoss": stop_loss,
            "takeProfit": take_profit,
            "investAmount": invest_amount,
            "pf": pf,
            "portfolioStatus": portfolio,
        })

    return advices


# ========================================
# Slack通知フォーマット
# ========================================

def format_advice_for_slack(advices: list) -> str:
    """
    投資判断をSlack通知用テキストにフォーマットする。
    """
    if not advices:
        return ""

    entry_list = [a for a in advices if a["recommendation"] == "ENTRY"]
    watch_list = [a for a in advices if a["recommendation"] == "WATCH"]
    skip_list  = [a for a in advices if a["recommendation"] == "SKIP"]

    lines = ["🎯 *投資判断サマリー*\n"]

    if entry_list:
        lines.append("*━━ エントリー推奨 ━━*")
        for a in entry_list:
            invest_man = a["investAmount"] / 10000
            lines.append(
                f"*{a['stockCode']} {a['companyName']}*\n"
                + "\n".join(f"  {r}" for r in a["reasons"])
                + f"\n  📌 翌朝始値目安: ¥{a['entryPrice']:,}"
                + f"  損切: ¥{a['stopLoss']:,}  利確: ¥{a['takeProfit']:,}"
                + f"\n  💴 投資額目安: {invest_man:.0f}万円"
            )
            if a["cautions"]:
                lines.append("  " + " / ".join(a["cautions"]))

    if watch_list:
        lines.append("\n*━━ 様子見 ━━*")
        for a in watch_list:
            lines.append(
                f"{a['stockCode']} {a['companyName']} — "
                + " / ".join(a["cautions"])
            )

    if skip_list:
        lines.append("\n*━━ 見送り ━━*")
        for a in skip_list:
            top_caution = a["cautions"][0] if a["cautions"] else "判定WEAK/NOISE"
            lines.append(f"{a['stockCode']} {a['companyName']} — {top_caution}")

    return "\n".join(lines)
