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
MIN_PF = 1.2           # 最低プロフィットファクター（SHORT_TERM / EARNINGS）
MIN_PF_MOMENTUM = 1.5  # MOMMENTUMシグナルのPF閾値（より厳格・遅れ入場リスク対策）
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
        if e.get("qualifyResult") == "継続"
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
# セクターマッピング（J-Quants Sector17CodeName → 米ETFセクター名）
# ========================================

# J-Quants の Sector17CodeName（日本語）→ 米市場スキャン結果のセクター名キーワード
SECTOR17_TO_US_ETF: dict[str, list[str]] = {
    "情報通信・サービスその他": ["Technology", "AI", "Cloud", "Software", "Communication"],
    "電機・精密":              ["Technology", "Semiconductors", "AI", "Industrials"],
    "機械":                    ["Industrials", "Machinery"],
    "自動車・輸送機":          ["Consumer Discretionary", "Industrials", "EV"],
    "医薬品":                  ["Healthcare", "Biotech", "Pharmaceuticals"],
    "建設・資材":              ["Industrials", "Infrastructure", "Materials"],
    "素材・化学":              ["Materials", "Industrials"],
    "鉄鋼・非鉄":             ["Materials", "Industrials"],
    "エネルギー資源":          ["Energy", "Clean Energy"],
    "電力・ガス":              ["Utilities", "Energy"],
    "食品":                    ["Consumer Staples"],
    "小売":                    ["Consumer Discretionary", "Retail"],
    "商社・卸売":              ["Industrials", "Materials"],
    "運輸・物流":              ["Industrials", "Transportation"],
    "銀行":                    ["Financials", "Banks"],
    "金融（除く銀行）":        ["Financials"],
    "不動産":                  ["Real Estate"],
}

# コード→Sector17CodeNameのキャッシュ（プロセス内）
_sector_cache: dict[str, str] = {}


def _build_sector_cache() -> None:
    """
    J-Quants get_listed_stocks() から stock_code → Sector17CodeName の辞書を構築してキャッシュする。
    失敗しても空のまま続行（セクターマッチなしとして扱う）。
    """
    global _sector_cache
    if _sector_cache:
        return
    try:
        from agents.jquants_fetcher import get_listed_stocks
        df = get_listed_stocks()
        if df.empty or "Sector17CodeName" not in df.columns:
            return
        code_col = "Code" if "Code" in df.columns else df.columns[0]
        _sector_cache = dict(zip(df[code_col].astype(str).str.strip(), df["Sector17CodeName"].astype(str)))
        logger.info(f"セクターキャッシュ構築完了: {len(_sector_cache)}銘柄")
    except Exception as e:
        logger.warning(f"セクターキャッシュ構築失敗（セクターマッチをスキップ）: {e}")


def _infer_sector_match(stock_code: str, top_sectors: list) -> Optional[str]:
    """
    銘柄コードの Sector17CodeName を引き、米国上昇セクターと一致するか確認する。
    一致したセクター名を返す（なければNone）。
    """
    _build_sector_cache()
    # 4桁・5桁どちらでも検索
    code = str(stock_code).strip()
    sector17 = _sector_cache.get(code) or _sector_cache.get(code + "0")
    if not sector17:
        return None
    en_keywords = SECTOR17_TO_US_ETF.get(sector17, [])
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
                "recommendation": "エントリー" | "様子見" | "見送り",
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
        qualify_result = result.get("qualifyResult", "ノイズ")
        mode = result.get("mode", "SHORT_TERM")
        current_price = result.get("close", 0)
        pf = pf_map.get(mode, 0)

        reasons = []
        cautions = []

        # ---- モード別役割の明示 ----
        if mode == "MOMENTUM":
            cautions.append("📊 長期トレンド確認型（保有継続向け・新規エントリーは慎重に）")
        elif mode == "SHORT_TERM":
            reasons.append("⚡ 急騰シグナル（早期エントリー候補）")
        elif mode == "EARNINGS":
            reasons.append("📋 決算シグナル（業績裏付けあり）")

        # ---- STRONG/WATCH/WEAK/NOISE 判定 ----
        if qualify_result == "継続":
            reasons.append(f"✅ 継続判定（{result.get('stage2', {}).get('comment', '')}）")
        elif qualify_result == "様子見":
            cautions.append("⚠️ 様子見（Claude判定未実行）")
        elif qualify_result == "一時的":
            cautions.append(f"❌ 一時的（{result.get('stage2', {}).get('comment', '')}）")
        else:
            cautions.append("❌ ノイズ（ステージ1不通過）")

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
            matched = _infer_sector_match(stock_code, us_context["top_sectors"])
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
        is_strong = qualify_result == "継続"
        has_capacity = portfolio["has_capacity"]

        # MOMMENTUMは長期トレンド確認型のため、PF閾値を高く・デフォルト様子見
        if mode == "MOMENTUM":
            pf_ok = pf >= MIN_PF_MOMENTUM
        else:
            pf_ok = pf >= MIN_PF

        if is_strong and pf_ok and has_capacity:
            recommendation = "エントリー"
        elif is_strong and (not pf_ok or not has_capacity):
            recommendation = "様子見"
        else:
            recommendation = "見送り"

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

def find_cross_signals(all_results: dict) -> list:
    """
    SHORT_TERM・MOMENTUM・EARNINGSの3シグナルを照合し、
    複数シグナルが重なった銘柄を返す。

    重なりの意味:
      SHORT_TERM + EARNINGS  → 業績裏付けのある急騰（継続性が高い）
      MOMENTUM   + EARNINGS  → 決算でモメンタム再加速
      SHORT_TERM + MOMENTUM  → トレンド中の追い風急騰
      全3つ                  → トリプル確認（最高確信度）

    Args:
        all_results: {モード名: スキャン結果リスト} の辞書

    Returns:
        list: crossLevel降順（TRIPLE→DOUBLE）でソートされた重複銘柄リスト
    """
    def _norm(code: str) -> str:
        c = str(code).strip()
        return c[:4] if (len(c) == 5 and c.endswith("0")) else c

    # SHORT_TERM: STRONG/WATCHのみ（NOISEは対象外）
    st_map = {
        _norm(r["stockCode"]): r
        for r in all_results.get("SHORT_TERM", [])
        if r.get("qualifyResult") in ("継続", "様子見")
    }

    # MOMENTUM: 全銘柄対象
    mo_map = {
        _norm(r["stockCode"]): r
        for r in all_results.get("MOMENTUM", [])
    }

    # EARNINGS: スコア30以上・分析済みのみ
    ea_map = {
        _norm(r["stockCode"]): r
        for r in all_results.get("EARNINGS", [])
        if r.get("score", 0) >= 30 and r.get("analyzed", False)
    }

    cross = []
    for code in set(st_map) | set(mo_map) | set(ea_map):
        signals = []
        if code in st_map:
            signals.append("SHORT_TERM")
        if code in mo_map:
            signals.append("MOMENTUM")
        if code in ea_map:
            signals.append("EARNINGS")

        if len(signals) < 2:
            continue

        rep = st_map.get(code) or mo_map.get(code) or ea_map.get(code)
        cross.append({
            "stockCode": code,
            "companyName": rep.get("companyName", ""),
            "signals": signals,
            "crossLevel": "TRIPLE" if len(signals) == 3 else "DOUBLE",
            "shortTermData": st_map.get(code),
            "momentumData": mo_map.get(code),
            "earningsData": ea_map.get(code),
        })

    return sorted(cross, key=lambda x: len(x["signals"]), reverse=True)


def format_advice_for_slack(advices: list) -> str:
    """
    投資判断をSlack通知用テキストにフォーマットする。
    """
    if not advices:
        return ""

    entry_list = [a for a in advices if a["recommendation"] == "エントリー"]
    watch_list = [a for a in advices if a["recommendation"] == "様子見"]
    skip_list  = [a for a in advices if a["recommendation"] == "見送り"]

    lines = ["🎯 *投資判断サマリー*\n"]

    mode_label_map = {"SHORT_TERM": "急騰", "MOMENTUM": "長期モメンタム", "EARNINGS": "決算"}

    if entry_list:
        lines.append("*━━ エントリー推奨 ━━*")
        for a in entry_list:
            invest_man = a["investAmount"] / 10000
            mode_lbl = mode_label_map.get(a.get("mode", ""), "")
            lines.append(
                f"*{a['stockCode']} {a['companyName']}*（{mode_lbl}）\n"
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
            mode_lbl = mode_label_map.get(a.get("mode", ""), "")
            lines.append(
                f"{a['stockCode']} {a['companyName']}（{mode_lbl}） — "
                + " / ".join(a["cautions"])
            )

    if skip_list:
        lines.append("\n*━━ 見送り ━━*")
        for a in skip_list:
            top_caution = a["cautions"][0] if a["cautions"] else "判定: 一時的/ノイズ"
            lines.append(f"{a['stockCode']} {a['companyName']} — {top_caution}")

    return "\n".join(lines)
