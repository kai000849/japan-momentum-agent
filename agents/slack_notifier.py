"""
agents/slack_notifier.py
Slack通知モジュール

以下のタイミングでSlackに通知を送る：
1. 毎朝の定時レポート（ポートフォリオ状況）
2. 新規シグナル検出時
3. 損切り・利確発動時

作者: Japan Momentum Agent
"""

import json
import logging
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from agents.momentum_qualifier import normalize_qualify_label

logger = logging.getLogger(__name__)


# ========================================
# 設定読み込み
# ========================================

def load_slack_config() -> dict:
    """
    config.yamlからSlack設定を読み込む。

    Returns:
        dict: Slack設定（webhook_url, channel等）
    """
    config_path = Path(__file__).parent.parent / "config.yaml"
    if not config_path.exists():
        logger.warning("config.yaml が見つかりません。")
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("slack", {})


# ========================================
# 送信コア関数
# ========================================

def send_slack_message(text: str, blocks: list = None) -> bool:
    """
    SlackにWebhook経由でメッセージを送信する。

    Args:
        text (str): プレーンテキスト（通知のフォールバック用）
        blocks (list): Slackブロックフォーマット（リッチ表示用）

    Returns:
        bool: 送信成功はTrue
    """
    slack_config = load_slack_config()
    webhook_url = slack_config.get("webhook_url", "")

    if not webhook_url:
        logger.error("Slack Webhook URLが設定されていません。config.yaml を確認してください。")
        return False

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                logger.info("Slack通知を送信しました。")
                return True
            else:
                logger.error(f"Slack送信失敗: HTTPステータス {response.status}")
                return False
    except urllib.error.URLError as e:
        msg = f"⚠️ Slack送信エラー: {e}"
        logger.error(msg)
        print(msg, file=sys.stderr)
        return False
    except Exception as e:
        msg = f"⚠️ Slack送信中に予期しないエラー: {e}"
        logger.error(msg)
        print(msg, file=sys.stderr)
        return False


# ========================================
# 通知テンプレート
# ========================================

def notify_daily_report(trade_log: dict, initial_capital: float) -> bool:
    """
    毎朝の定時レポートをSlackに送信する。

    Args:
        trade_log (dict): 取引ログ（trade_log.json の内容）
        initial_capital (float): 初期資金

    Returns:
        bool: 送信成功はTrue
    """
    positions = trade_log.get("positions", [])
    closed_trades = trade_log.get("closed_trades", [])
    summary = trade_log.get("summary", {})

    # 損益計算
    invested_total = sum(p.get("investedAmount", 0) for p in positions)
    unrealized_pnl = sum(p.get("unrealizedPnl", 0) for p in positions)
    realized_pnl = summary.get("total_pnl", 0)
    current_cash = initial_capital + realized_pnl - invested_total
    total_value = current_cash + invested_total + unrealized_pnl
    total_return_pct = ((total_value - initial_capital) / initial_capital) * 100

    # 勝率計算
    total_trades = summary.get("total_trades", 0)
    win_rate_str = "N/A"
    if total_trades > 0:
        win_rate = (summary.get("winning_trades", 0) / total_trades) * 100
        win_rate_str = f"{win_rate:.1f}%"

    # 損益の絵文字
    pnl_emoji = "📈" if (realized_pnl + unrealized_pnl) >= 0 else "📉"
    total_pnl = realized_pnl + unrealized_pnl
    pnl_sign = "+" if total_pnl >= 0 else ""

    # ポジション一覧テキスト
    position_lines = []
    for p in positions:
        pnl_pct = p.get("unrealizedPnlPct", 0)
        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
        sign = "+" if pnl_pct >= 0 else ""
        name = p.get("companyName", p.get("stockCode", ""))[:10]
        position_lines.append(
            f"  {pnl_icon} {p.get('stockCode')} {name} "
            f"{sign}{pnl_pct:.1f}% ({p.get('holdDays', 0)}日目)"
        )

    positions_text = "\n".join(position_lines) if position_lines else "  保有銘柄なし"

    # メッセージ組み立て
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    text = f"""
{pnl_emoji} *Japan Momentum Agent - 朝次レポート*
{now}

━━━━━━━━━━━━━━━━━━
💴 *資金状況*
  現在評価額: ¥{total_value:,.0f}
  総損益: {pnl_sign}¥{total_pnl:,.0f} ({pnl_sign}{total_return_pct:.2f}%)
  　├ 実現損益: {'+' if realized_pnl >= 0 else ''}¥{realized_pnl:,.0f}
  　└ 含み損益: {'+' if unrealized_pnl >= 0 else ''}¥{unrealized_pnl:,.0f}
  現金残高: ¥{current_cash:,.0f}

📊 *取引成績*
  総取引数: {total_trades}件 / 勝率: {win_rate_str}

📋 *保有ポジション ({len(positions)}銘柄)*
{positions_text}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_new_signal(signals: list, mode: str, profit_factor: float, skipped_count: int = 0) -> bool:
    """
    新規シグナル検出をSlackに通知する。

    Args:
        signals (list): シグナル銘柄リスト
        mode (str): スキャンモード（SHORT_TERM / MOMENTUM / EARNINGS）
        profit_factor (float): バックテストのプロフィットファクター
        skipped_count (int): 朝スキャン済みで除外した件数（0なら表示しない）

    Returns:
        bool: 送信成功はTrue
    """
    if not signals:
        return True

    # EARNINGSは専用通知関数を使用
    if mode == "EARNINGS":
        return notify_earnings_signal(signals)

    mode_label = {
        "SHORT_TERM": "🚀 急騰シグナル",
        "MOMENTUM": "📈 モメンタムシグナル",
    }.get(mode, mode)

    lines = []
    for s in signals[:10]:  # 最大10件
        code = s.get("stockCode", "")
        name = (s.get("companyName") or s.get("company_name") or "")[:12]
        name_str = f" {name}" if name else ""
        close = s.get("close", 0)
        score = s.get("score", s.get("momentum_score", 0))

        if mode == "MOMENTUM":
            rsi = s.get("rsi14", 0)
            high_ratio = s.get("priceToHighRatio", 0)
            new_high = s.get("newHighCount", 0)
            vol_trend = s.get("volumeTrend", 1.0)
            vol_icon = "📶" if vol_trend >= 1.2 else ("➡️" if vol_trend >= 0.9 else "📉")
            comment = s.get("comment", "")
            comment_str = f"\n    💬 {comment}" if comment else ""
            expected_wr = s.get("expected_win_rate")
            pattern_notes = s.get("pattern_notes", "")
            pattern_str = (
                f"\n    📊 過去勝率: {expected_wr:.0f}%  ({pattern_notes})"
                if expected_wr is not None else ""
            )
            lines.append(
                f"  • {code}{name_str}  スコア:{score:.1f}\n"
                f"    RSI:{rsi:.0f}  高値比:{high_ratio:.1f}%  新高値:{new_high}回/20日  出来高:{vol_icon}{vol_trend:.2f}x"
                f"{comment_str}{pattern_str}"
            )
        else:
            price_chg = s.get("priceChangePct", 0)
            vol_ratio = s.get("volumeRatio", 0)
            surge_reason = s.get("surgeReason", "")
            sign = "+" if price_chg >= 0 else ""
            reason_str = f"\n    💡 {surge_reason}" if surge_reason else ""
            lines.append(
                f"  • {code}{name_str}  ¥{close:,.0f}  スコア:{score:.1f}\n"
                f"    前日比:{sign}{price_chg:.1f}% / 出来高:{vol_ratio:.1f}x"
                f"{reason_str}"
            )

    signals_text = "\n".join(lines)
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    skipped_str = f"\n_（朝スキャン済みを{skipped_count}件除外）_" if skipped_count > 0 else ""

    text = f"""
🔔 *{mode_label} 検出！*
{now}

PF: *{profit_factor:.2f}*  |  対象: *{len(signals)}銘柄*

{signals_text}

{"⚠️ PF < 1.2（参考値）" if profit_factor > 0 and profit_factor < 1.2 else ""}{skipped_str}
""".strip()

    return send_slack_message(text)


def notify_earnings_signal(signals: list) -> bool:
    """
    決算スコアリング結果をSlackに通知する。
    ベスト/ワーストをランキング形式で表示。

    Args:
        signals (list): EARNINGSスキャン結果（analyze_earnings_batchの出力を含む）

    Returns:
        bool: 送信成功はTrue
    """
    if not signals:
        return True

    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # スコア付き分析済みと未分析を分離
    analyzed = [s for s in signals if s.get("analyzed", False)]
    unanalyzed = [s for s in signals if not s.get("analyzed", False)]

    if not analyzed:
        # 未分析のみの場合 → 理由に応じてメッセージを変える
        from agents.utils import get_anthropic_key as _check_key
        has_api_key = bool(_check_key())
        # skip_reason 別に集計
        no_doc_id_count = sum(1 for s in signals if s.get("skip_reason") == "no_doc_id")
        pdf_fail_count = sum(1 for s in signals if s.get("summary", "").startswith("PDF"))
        if not has_api_key:
            note = "APIキー設定で詳細分析が有効になります"
        elif no_doc_id_count == len(signals):
            note = "書類ID未取得（EDINET API応答に docID なし）"
        elif pdf_fail_count > 0:
            note = f"PDF取得・解析失敗 {pdf_fail_count}件"
        else:
            note = "決算PDF解析スキップ（書類種別・PDFなし等）"

        lines = []
        for s in signals[:20]:
            code = s.get("stockCode", "")
            name = (s.get("companyName") or "")[:15]
            doc_desc = s.get("docDescription", "")[:20] or "決算"
            lines.append(f"  • {code} {name}  [{doc_desc}]")

        text = f"""
📋 *決算シグナル 検出！*
{now}

対象: *{len(signals)}銘柄*（{note}）

{"　".join(lines[:20])}
""".strip()
        return send_slack_message(text)

    # スコア閾値: 30点以上のみ詳細表示（ウォッチリスト登録基準と統一）
    MIN_NOTIFY_SCORE = 30

    # 注目銘柄（スコア30以上）・低スコア・ネガティブに分類
    top = sorted([s for s in analyzed if s.get("score", 0) >= MIN_NOTIFY_SCORE],
                 key=lambda x: x.get("score", 0), reverse=True)[:8]
    low_positive = [s for s in analyzed if 0 < s.get("score", 0) < MIN_NOTIFY_SCORE]
    negative = [s for s in analyzed if s.get("score", 0) < 0]

    # ========== 注目銘柄: 詳細表示（追加APIコールなし・summaryをそのまま使用） ==========
    top_lines = []
    for i, s in enumerate(top, 1):
        code = s.get("stockCode", "")
        name = (s.get("companyName") or "")[:15]
        score = s.get("score", 0)
        revenue_yoy = s.get("revenue_yoy", "不明")
        profit_yoy = s.get("profit_yoy", "不明")
        vs_forecast = s.get("vs_forecast", "不明")
        summary = s.get("summary", "")

        icon = "🔥" if score >= 80 else ("🟢" if score >= 50 else "🔼")

        mom_pot = s.get("momentum_potential", "")
        entry_tim = s.get("entry_timing", "")
        catalyst = s.get("catalyst_type", "")
        mom_icon = {"high": "🚀", "medium": "📈", "low": "➡️"}.get(mom_pot, "")
        catalyst_str = f"  [{catalyst}]" if catalyst else ""
        mom_str = f"\n    {mom_icon} {mom_pot} / {entry_tim}" if mom_pot else ""
        expected_wr = s.get("expected_win_rate")
        pattern_notes = s.get("pattern_notes", "")
        pattern_str = (
            f"\n    📊 過去勝率: {expected_wr:.0f}%  ({pattern_notes})"
            if expected_wr is not None else ""
        )

        top_lines.append(
            f"{icon} *{i}位 +{score}点* {code} {name}{catalyst_str}\n"
            f"    売上:{revenue_yoy} / 営利:{profit_yoy} / 予想比:{vs_forecast}\n"
            f"    💬 {summary}{mom_str}{pattern_str}"
        )

    # ========== メッセージ組み立て ==========
    top_text = "\n\n".join(top_lines) if top_lines else "  スコア30点以上の銘柄なし"

    # 低スコア・ネガティブはコンパクトな件数表示のみ
    footer_parts = []
    if low_positive:
        footer_parts.append(f"📊 低スコア(1〜29点): {len(low_positive)}件")
    if negative:
        footer_parts.append(f"🔴 ネガティブ: {len(negative)}件")
    if unanalyzed:
        footer_parts.append(f"📄 未分析: {len(unanalyzed)}件")
    footer_str = ("\n" + "\n".join(footer_parts)) if footer_parts else ""

    text = f"""
📊 *決算スコアリング {now}*
分析済み: {len(analyzed)}件 | 総件数: {len(signals)}件

━━━━━━━━━━━━━━━━━━
🟢 *注目銘柄（30点以上） {len(top)}件*

{top_text}{footer_str}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_position_exit(stock_code: str, company_name: str,
                          entry_price: float, exit_price: float,
                          pnl_yen: float, pnl_pct: float,
                          exit_reason: str) -> bool:
    """
    損切り・利確発動をSlackに通知する。

    Args:
        stock_code (str): 銘柄コード
        company_name (str): 会社名
        entry_price (float): 取得価格
        exit_price (float): 決済価格
        pnl_yen (float): 損益（円）
        pnl_pct (float): 損益（%）
        exit_reason (str): 決済理由

    Returns:
        bool: 送信成功はTrue
    """
    is_win = pnl_yen >= 0
    emoji = "✅ 利確" if is_win else "🛑 損切り"
    sign = "+" if pnl_yen >= 0 else ""
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    text = f"""
{emoji} *ポジション決済*
{now}

銘柄: *{stock_code}* {company_name}
取得: ¥{entry_price:,.0f} → 決済: ¥{exit_price:,.0f}
損益: *{sign}¥{pnl_yen:,.0f} ({sign}{pnl_pct:.2f}%)*
理由: {exit_reason}
""".strip()

    return send_slack_message(text)


def notify_us_combined(scan_result: dict, theme_result: dict) -> bool:
    """
    米市場スキャン＋テーマ抽出を1通にまとめて通知する。
    目的: 市場の関心・動向把握と次のモメンタム候補への当たりつけ。

    構成:
      - マクロ指数
      - セクター強弱: 当日TOP5/WORST3（mom1d）＋中長期TOP5/WORST3（score）、各セクターに米国・日本代表銘柄3つずつ
      - 注目テーマ TOP5、各テーマに米国・日本代表銘柄3つずつ
      - リスク

    Args:
        scan_result (dict): run_us_market_scan() の戻り値
        theme_result (dict): run_theme_extraction() の戻り値

    Returns:
        bool: 送信成功はTrue
    """
    scan_date = scan_result.get("scan_date", datetime.now().strftime("%Y年%m月%d日 %H:%M"))
    headline_count = theme_result.get("headline_count", 0)
    keywords_data = theme_result.get("keywords", {}) or {}
    analysis = scan_result.get("analysis", {}) or {}

    # ========== マクロ指数 ==========
    macro = scan_result.get("macro", {})
    macro_narrative = keywords_data.get("macro_narrative", "") if isinstance(keywords_data, dict) else ""
    macro_lines = []
    for _sym, data in macro.items():
        change1d = data.get("change1d", 0)
        change5d = data.get("change5d", 0)
        icon = "📈" if change1d >= 0.5 else ("📉" if change1d <= -0.5 else "➡️")
        s1 = "+" if change1d >= 0 else ""
        s5 = "+" if change5d >= 0 else ""
        macro_lines.append(f"  {icon} {data['name']}: 当日{s1}{change1d:.1f}% / 5日{s5}{change5d:.1f}%")
    macro_text = "\n".join(macro_lines) if macro_lines else "  データなし"
    if macro_narrative:
        macro_text = f"  {macro_narrative}\n{macro_text}"

    # ========== セクター強弱（2軸） ==========
    sector_ranking = scan_result.get("sector_ranking", [])
    daily_section = "  データなし"
    score_section = "  データなし"

    def _fmt_sector_with_stocks(s, value_str: str) -> str:
        """セクター1件を銘柄付きで整形する。"""
        us = s.get("top_stocks", "")
        jp = s.get("japan_theme", "")
        line = f"{s['name']}({s['ticker']}){value_str}"
        if us:
            line += f"\n    🇺🇸 {us}"
        if jp:
            line += f"\n    🇯🇵 {jp}"
        return line

    if sector_ranking:
        # 当日軸（mom1d）
        sorted_day = sorted(sector_ranking, key=lambda x: x.get("mom1d", 0), reverse=True)
        day_top5 = sorted_day[:5]
        day_worst3 = sorted_day[-3:]

        top_lines = "\n".join(
            f"  {i}. " + _fmt_sector_with_stocks(s, f"{'+' if s.get('mom1d',0)>=0 else ''}{s.get('mom1d',0):.1f}%")
            for i, s in enumerate(day_top5, 1)
        )
        worst_lines = "\n".join(
            "  • " + _fmt_sector_with_stocks(s, f"{'+' if s.get('mom1d',0)>=0 else ''}{s.get('mom1d',0):.1f}%")
            for s in day_worst3
        )
        daily_section = f"🔥 TOP:\n{top_lines}\n🔻 WORST:\n{worst_lines}"

        # 中長期軸（score = mom5d×0.5 + mom20d×0.3 + mom60d×0.2）
        sorted_score = sorted(sector_ranking, key=lambda x: x.get("score", 0), reverse=True)
        sc_top5 = sorted_score[:5]
        sc_worst3 = sorted_score[-3:]

        sc_top_lines = "\n".join(
            f"  {i}. " + _fmt_sector_with_stocks(s, f"{'+' if s.get('score',0)>=0 else ''}{s.get('score',0):.1f}")
            for i, s in enumerate(sc_top5, 1)
        )
        sc_worst_lines = "\n".join(
            "  • " + _fmt_sector_with_stocks(s, f"{'+' if s.get('score',0)>=0 else ''}{s.get('score',0):.1f}")
            for s in sc_worst3
        )
        score_section = f"🔥 TOP:\n{sc_top_lines}\n🔻 WORST:\n{sc_worst_lines}"

    # ========== 注目テーマ TOP5（US・JP銘柄付き） ==========
    hot_keywords = keywords_data.get("hot_keywords", []) if isinstance(keywords_data, dict) else []
    mention_icons = {"high": "🔥", "medium": "📈", "low": "💡"}
    theme_lines = []
    for i, kw in enumerate(hot_keywords[:5], 1):
        icon = mention_icons.get(kw.get("mention_level", "medium"), "📈")
        us_stocks = kw.get("us_stocks", [])
        jp_stocks = kw.get("jp_stocks", [])
        us_str = " / ".join(us_stocks[:3]) if us_stocks else ""
        jp_str = " / ".join(jp_stocks[:3]) if jp_stocks else ""
        line = (
            f"  {icon} {i}. *{kw.get('keyword', '')}* [{kw.get('sector', '')}]\n"
            f"     {kw.get('context', '')}"
        )
        if us_str:
            line += f"\n     🇺🇸 {us_str}"
        if jp_str:
            line += f"\n     🇯🇵 {jp_str}"
        theme_lines.append(line)
    if not theme_lines:
        for t in (analysis.get("theme_analysis") or [])[:3]:
            theme_lines.append(f"  📈 *{t.get('sector', '')}*  {t.get('reason', '')}")
    themes_text = "\n\n".join(theme_lines) if theme_lines else "  データなし"

    # ========== リスク ==========
    risk_keywords = keywords_data.get("risk_keywords") or [] if isinstance(keywords_data, dict) else []
    analysis_risks = analysis.get("risk_factors") or []
    all_risks = list(dict.fromkeys(risk_keywords[:2] + analysis_risks[:2]))
    risk_text = "・".join(all_risks[:3]) if all_risks else "特になし"

    # ========== メッセージ組み立て ==========
    headline_str = f"（ニュース{headline_count}件分析）" if headline_count else ""
    text = f"""
🌏 *米市場まとめ*  {scan_date}  {headline_str}

━━━━━━━━━━━━━━━━━━
📊 *マクロ指数*
{macro_text}

━━━━━━━━━━━━━━━━━━
🏭 *セクター強弱 — 当日（mom1d）*
{daily_section}

━━━━━━━━━━━━━━━━━━
📈 *セクター強弱 — 中長期（5日×0.5+20日×0.3+60日×0.2）*
{score_section}

━━━━━━━━━━━━━━━━━━
📌 *注目テーマ TOP5*

{themes_text}

⚠️ *リスク*: {risk_text}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_us_market_scan(scan_result: dict) -> bool:
    """
    米市場セクター・テーマモメンタムスキャン結果をSlackに通知する。

    Args:
        scan_result (dict): us_market_scanner.run_us_market_scan()の戻り値

    Returns:
        bool: 送信成功はTrue
    """
    sector_ranking = scan_result.get("sector_ranking", [])
    macro = scan_result.get("macro", {})
    analysis = scan_result.get("analysis", {})
    scan_date = scan_result.get("scan_date", datetime.now().strftime("%Y年%m月%d日 %H:%M"))

    if not sector_ranking:
        return True

    # ========== マクロ状況 ==========
    macro_lines = []
    for sym, data in macro.items():
        change1d = data.get("change1d", 0)
        change5d = data.get("change5d", 0)
        icon = "📈" if change1d >= 0.5 else ("📉" if change1d <= -0.5 else "➡️")
        sign1 = "+" if change1d >= 0 else ""
        sign5 = "+" if change5d >= 0 else ""
        macro_lines.append(f"  {icon} {data['name']}: 当日{sign1}{change1d:.1f}% / 5日{sign5}{change5d:.1f}%")
    macro_text = "\n".join(macro_lines) if macro_lines else "  データなし"

    # ========== セクターランキング上位5・下位3 ==========
    top5 = sector_ranking[:5]
    bottom3 = [s for s in sector_ranking if s.get("score", 0) < 0][-3:]

    top_lines = []
    for i, s in enumerate(top5, 1):
        score = s.get("score", 0)
        mom1 = s.get("mom1d", 0)
        mom5 = s.get("mom5d", 0)
        mom20 = s.get("mom20d", 0)
        vol = s.get("vol_trend", 1.0)
        vol_icon = "📶" if vol >= 1.3 else ("➡️" if vol >= 0.9 else "📉")
        sign1 = "+" if mom1 >= 0 else ""
        sign5 = "+" if mom5 >= 0 else ""
        sign20 = "+" if mom20 >= 0 else ""
        japan = s.get("japan_theme", "")
        top_stocks = s.get("top_stocks", "")
        stocks_line = f"\n     🏢 {top_stocks}" if top_stocks else ""
        top_lines.append(
            f"  *{i}. {s['name']}({s['ticker']})*  スコア:{score:.1f}\n"
            f"     当日:{sign1}{mom1:.1f}% / 5日:{sign5}{mom5:.1f}% / 20日:{sign20}{mom20:.1f}%  出来高:{vol_icon}{vol:.2f}x"
            f"{stocks_line}\n"
            f"     🇯🇵 {japan}"
        )

    bottom_lines = []
    for s in bottom3:
        mom1 = s.get("mom1d", 0)
        mom5 = s.get("mom5d", 0)
        sign1 = "+" if mom1 >= 0 else ""
        sign5 = "+" if mom5 >= 0 else ""
        top_stocks = s.get("top_stocks", "")
        stocks_str = f"  ({top_stocks})" if top_stocks else ""
        bottom_lines.append(
            f"  🔻 {s['name']}({s['ticker']}): 当日{sign1}{mom1:.1f}% / 5日{sign5}{mom5:.1f}%{stocks_str}"
        )

    top_text = "\n\n".join(top_lines) if top_lines else "  データなし"
    bottom_text = "\n".join(bottom_lines) if bottom_lines else "  なし"

    # ========== Claude分析 ==========
    analysis_text = ""
    if analysis and "error" not in analysis:
        market_summary = analysis.get("market_summary", "")
        japan_opps = analysis.get("japan_opportunities", [])
        risks = analysis.get("risk_factors", [])
        theme_analysis = analysis.get("theme_analysis", [])

        # テーマ分析
        theme_lines = []
        for t in theme_analysis[:3]:
            sustain_map = {"short": "短期", "medium": "中期", "long": "長期"}
            sustain = sustain_map.get(t.get("sustainability", ""), t.get("sustainability", ""))
            theme_lines.append(
                f"  • *{t.get('sector', '')}*（{sustain}）\n"
                f"    {t.get('reason', '')}\n"
                f"    🇯🇵 注目: {t.get('japan_stocks', '')}"
            )

        opps_text = "・".join(japan_opps[:3]) if japan_opps else "なし"
        risk_text = "・".join(risks[:2]) if risks else "なし"
        themes_text = "\n\n".join(theme_lines)

        analysis_text = f"""
━━━━━━━━━━━━━━━━━━
🤖 *Claude分析*
{market_summary}

📌 *注目テーマ（詳細）*
{themes_text}

🇯🇵 *日本株チャンス*: {opps_text}
⚠️ *リスク*: {risk_text}"""

    # ========== メッセージ組み立て ==========
    text = f"""
🌏 *米市場テーマ検出*  {scan_date}

━━━━━━━━━━━━━━━━━━
📊 *マクロ指数（5日騰落）*
{macro_text}

━━━━━━━━━━━━━━━━━━
🔥 *強いセクター TOP5*

{top_text}

🔻 *弱いセクター*
{bottom_text}{analysis_text}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_us_theme_extraction(theme_result: dict, sector_ranking: list = None) -> bool:
    """
    米市場ホットキーワード・テーマ抽出結果をSlackに通知する。

    Args:
        theme_result (dict): us_theme_extractor.run_theme_extraction()の戻り値
        sector_ranking (list): us_market_scanner のセクターランキング（任意）。
                               指定時はTOP3/弱セクターTOP2をコンパクト表示する。

    Returns:
        bool: 送信成功はTrue
    """
    keywords_data = theme_result.get("keywords", {})
    scan_date = theme_result.get("scan_date", datetime.now().strftime("%Y年%m月%d日 %H:%M"))
    headline_count = theme_result.get("headline_count", 0)

    if not keywords_data or "error" in keywords_data:
        error = keywords_data.get("error", "不明なエラー")
        text = f"⚠️ 米市場テーマ抽出エラー\n{scan_date}\n{error}"
        return send_slack_message(text)

    hot_keywords = keywords_data.get("hot_keywords", [])
    sector_narratives = keywords_data.get("sector_narratives", [])
    japan_plays = keywords_data.get("japan_plays", [])
    macro_narrative = keywords_data.get("macro_narrative", "")
    risk_keywords = keywords_data.get("risk_keywords", [])

    # ========== ホットキーワード ==========
    mention_icons = {"high": "🔥", "medium": "📈", "low": "💡"}
    keyword_lines = []
    for i, kw in enumerate(hot_keywords[:5], 1):
        icon = mention_icons.get(kw.get("mention_level", "medium"), "📈")
        keyword_lines.append(
            f"  {icon} *{i}. {kw.get('keyword', '')}*  [{kw.get('sector', '')}]\n"
            f"     {kw.get('context', '')}"
        )
    keywords_text = "\n\n".join(keyword_lines) if keyword_lines else "  データなし"

    # ========== セクター別サブテーマ ==========
    momentum_icons = {"rising": "⬆️", "stable": "➡️", "falling": "⬇️"}
    narrative_lines = []
    for s in sector_narratives[:3]:
        icon = momentum_icons.get(s.get("momentum", "stable"), "➡️")
        narrative_lines.append(
            f"  {icon} *{s.get('sector', '')}* › {s.get('sub_theme', '')}\n"
            f"     {s.get('detail', '')}"
        )
    narratives_text = "\n\n".join(narrative_lines) if narrative_lines else "  データなし"

    # ========== 日本株への波及 ==========
    japan_lines = []
    for jp in japan_plays[:3]:
        stocks = "・".join(jp.get("stocks", [])[:3])
        japan_lines.append(
            f"  🇯🇵 *{jp.get('theme', '')}*\n"
            f"     注目: {stocks}\n"
            f"     {jp.get('reason', '')}"
        )
    japan_text = "\n\n".join(japan_lines) if japan_lines else "  なし"

    # ========== リスク ==========
    risk_text = "・".join(risk_keywords[:2]) if risk_keywords else "なし"

    # ========== セクター当日騰落（mom1d基準でソート・best5/worst3） ==========
    sector_section = ""
    if sector_ranking:
        sorted_by_day = sorted(sector_ranking, key=lambda x: x.get("mom1d", 0), reverse=True)
        best5 = sorted_by_day[:5]
        worst3 = sorted_by_day[-3:]

        top_lines = []
        for i, s in enumerate(best5, 1):
            m1 = s.get("mom1d", 0)
            s1 = "+" if m1 >= 0 else ""
            top_lines.append(f"  {i}. *{s['name']}*  {s1}{m1:.1f}%")

        weak_lines = []
        for s in worst3:
            m1 = s.get("mom1d", 0)
            s1 = "+" if m1 >= 0 else ""
            weak_lines.append(f"  🔻 {s['name']}  {s1}{m1:.1f}%")

        sector_section = (
            "\n━━━━━━━━━━━━━━━━━━\n"
            "🔥 *強いセクター TOP5（当日）*\n"
            + "\n".join(top_lines)
            + "\n🔻 *弱いセクター（当日）*\n"
            + "\n".join(weak_lines)
        )

    text = f"""
🔍 *米市場 ホットキーワード*  {scan_date}
（ニュース{headline_count}件を分析）

━━━━━━━━━━━━━━━━━━
📌 *マクロ環境*
  {macro_narrative}

━━━━━━━━━━━━━━━━━━
🔥 *注目キーワード TOP5*

{keywords_text}

━━━━━━━━━━━━━━━━━━
🏭 *セクター別サブテーマ*

{narratives_text}

━━━━━━━━━━━━━━━━━━
🇯🇵 *日本株への波及チャンス*

{japan_text}

⚠️ *リスクワード*: {risk_text}{sector_section}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_intraday_earnings_scan(scan_results: list, stats: dict = None) -> bool:
    """
    ザラ場モメンタムスキャン結果をSlackに通知する。

    Args:
        scan_results: earnings_momentum_scanner.run_intraday_earnings_scan()の戻り値
        stats: get_earnings_accuracy_stats()の戻り値（学習統計）

    Returns:
        bool: 送信成功はTrue
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    if not scan_results:
        text = f"🎯 *ザラ場モメンタムスキャン* {now}\n対象銘柄なし（前日ウォッチリストが空）"
        return send_slack_message(text)

    lines = []
    for i, r in enumerate(scan_results[:8], 1):
        code = r.get("stockCode", "")
        name = (r.get("companyName") or "")[:12]
        edinet_score = r.get("edinetScore", 0)
        intra = r.get("intradayData", {})
        total = r.get("totalScore", 0)
        judgment = r.get("entryJudgment", "")
        catalyst = r.get("catalystType", "")
        summary = r.get("summary", "")[:40]

        gap = intra.get("opening_gap_pct", 0)
        momentum = intra.get("intraday_momentum_pct", 0)
        vol = intra.get("volume_ratio", 0)
        new_highs = intra.get("is_making_new_highs", False)

        sign_g = "+" if gap >= 0 else ""
        sign_m = "+" if momentum >= 0 else ""
        is_haritsuki = intra.get("is_haritsuki", False)
        is_yobarazu = intra.get("is_yobarazu", False)
        highs_str = " 📶新高値更新中" if new_highs and not is_haritsuki else ""
        catalyst_str = f"[{catalyst}] " if catalyst else ""

        if not intra:
            detail = "（前場データ取得不可・寄らずS高の可能性あり）"
        elif is_yobarazu:
            detail = f"🚨 寄らずS高買い気配  ギャップ:{sign_g}{gap:.1f}%（板が刺さらない状態）"
        elif is_haritsuki:
            detail = f"🔥 ストップ高張り付き中  ギャップ:{sign_g}{gap:.1f}% / 出来高:{vol:.1f}x"
        else:
            detail = (
                f"寄り:{sign_g}{gap:.1f}% / 前場継続:{sign_m}{momentum:.1f}%"
                f" / 出来高:{vol:.1f}x{highs_str}"
            )

        comment = r.get("comment") or summary
        lines.append(
            f"{judgment}\n"
            f"*{i}. {code} {name}*  総合:{total:.0f}  EDINET:{edinet_score:+d}点\n"
            f"  {catalyst_str}{detail}\n"
            f"  💬 {comment}"
        )

    results_text = "\n\n".join(lines)

    # 学習統計
    stats_text = ""
    if stats and stats.get("total_signals", 0) >= 5:
        s5 = stats.get("outcome5d", {})
        s10 = stats.get("outcome10d", {})
        stats_text = (
            f"\n\n📊 *学習統計（{stats['total_signals']}件記録）*\n"
            f"  5日後: 勝率{s5.get('win_rate', 0):.0f}% / 平均{s5.get('avg_return', 0):+.1f}%\n"
            f"  10日後: 勝率{s10.get('win_rate', 0):.0f}% / 平均{s10.get('avg_return', 0):+.1f}%"
        ) if s5 and s10 else ""

    text = f"""
🎯 *ザラ場モメンタムスキャン*  {now}
決算・開示銘柄の前場反応ランキング

━━━━━━━━━━━━━━━━━━
{results_text}{stats_text}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_endofday_earnings_scan(scan_results: list, stats: dict = None) -> bool:
    """
    引け後決算モメンタム評価結果をSlackに通知する。
    J-Quants日足データによる一日の値動き評価ランキング。

    Args:
        scan_results: earnings_momentum_scanner.run_endofday_earnings_scan()の戻り値
        stats: get_earnings_accuracy_stats()の戻り値（学習統計）

    Returns:
        bool: 送信成功はTrue
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    if not scan_results:
        text = f"📊 *引け後決算モメンタム評価* {now}\n対象銘柄なし（前日ウォッチリストが空）"
        return send_slack_message(text)

    lines = []
    for i, r in enumerate(scan_results[:8], 1):
        code = r.get("stockCode", "")
        name = (r.get("companyName") or "")[:12]
        edinet_score = r.get("edinetScore", 0)
        eod = r.get("eodData", {})
        total = r.get("totalScore", 0)
        judgment = r.get("entryJudgment", "")
        catalyst = r.get("catalystType", "")
        comment = r.get("comment", r.get("summary", ""))[:40]

        day_ret = eod.get("day_return_pct", 0)
        close_vs_open = eod.get("close_vs_open_pct", 0)
        vol = eod.get("volume_ratio", 0)
        candle = eod.get("candle_pattern", "")

        sign_d = "+" if day_ret >= 0 else ""
        sign_c = "+" if close_vs_open >= 0 else ""
        catalyst_str = f"[{catalyst}] " if catalyst else ""

        is_stop_high = eod.get("is_stop_high", False)
        is_yobarazu_eod = eod.get("is_yobarazu", False)

        if not eod:
            detail = "（引け後データ取得不可）"
        elif is_yobarazu_eod:
            detail = f"🚨 寄らずS高  前日比:{sign_d}{day_ret:.1f}%（出来高ほぼゼロ）"
        elif is_stop_high:
            detail = f"🔥 ストップ高張り付き  前日比:{sign_d}{day_ret:.1f}% / 出来高:{vol:.1f}x"
        else:
            detail = (
                f"前日比:{sign_d}{day_ret:.1f}% / 寄り後:{sign_c}{close_vs_open:.1f}%"
                f" / 出来高:{vol:.1f}x / {candle}"
            )

        comment_str = f"\n  💬 {comment}" if comment else ""

        lines.append(
            f"{judgment}\n"
            f"*{i}. {code} {name}*  総合:{total:.0f}  EDINET:{edinet_score:+d}点\n"
            f"  {catalyst_str}{detail}{comment_str}"
        )

    results_text = "\n\n".join(lines)

    # 学習統計
    stats_text = ""
    if stats and stats.get("total_signals", 0) >= 5:
        s5 = stats.get("outcome5d", {})
        s10 = stats.get("outcome10d", {})
        stats_text = (
            f"\n\n📊 *学習統計（{stats['total_signals']}件記録）*\n"
            f"  5日後: 勝率{s5.get('win_rate', 0):.0f}% / 平均{s5.get('avg_return', 0):+.1f}%\n"
            f"  10日後: 勝率{s10.get('win_rate', 0):.0f}% / 平均{s10.get('avg_return', 0):+.1f}%"
        ) if s5 and s10 else ""

    text = f"""
📊 *引け後決算モメンタム評価*  {now}
決算・開示銘柄の一日の値動き評価ランキング（J-Quants日足）

━━━━━━━━━━━━━━━━━━
{results_text}{stats_text}
━━━━━━━━━━━━━━━━━━
""".strip()

    return send_slack_message(text)


def notify_cross_signals(cross_signals: list) -> bool:
    """
    複数シグナルが重なった銘柄をSlackに通知する。
    3シグナルのうち2つ以上が一致した銘柄 = 確信度が高いエントリー候補。

    Args:
        cross_signals: find_cross_signals()の戻り値

    Returns:
        bool: 送信成功はTrue
    """
    if not cross_signals:
        return True

    triple = [c for c in cross_signals if c["crossLevel"] == "TRIPLE"]
    double = [c for c in cross_signals if c["crossLevel"] == "DOUBLE"]

    label_map = {
        "SHORT_TERM": "急騰",
        "MOMENTUM": "中長期モメンタム",
        "EARNINGS": "決算",
    }

    def _fmt(c: dict) -> str:
        code = c["stockCode"]
        name = c["companyName"]
        sig_str = " ＋ ".join(label_map.get(s, s) for s in c["signals"])
        rows = [f"• *{code} {name}*  `{sig_str}`"]

        st = c.get("shortTermData")
        if st:
            pct = st.get("priceChangePct", 0)
            vol = st.get("volumeRatio", 0)
            qr = normalize_qualify_label(st.get("qualifyResult", ""))
            vp = st.get("volume_pattern", "unknown")
            vp_tag = " 📈バズ型" if vp == "late" else (" 🐸ジワジワ型" if vp == "early" else "")
            reason = st.get("surgeReason", "")
            row = f"  📈 急騰 +{pct:.1f}% / 出来高 {vol:.1f}倍 [{qr}]{vp_tag}"
            if reason:
                row += f"\n     💡 {reason}"
            rows.append(row)

        mo = c.get("momentumData")
        if mo:
            rsi = mo.get("rsi14", 0)
            hr = mo.get("priceToHighRatio", 0)
            comment = mo.get("comment", "")
            row = f"  📊 RSI {rsi:.0f} / 52週高値比 {hr:.0f}%"
            if comment:
                row += f" / {comment}"
            rows.append(row)

        ea = c.get("earningsData")
        if ea:
            score = ea.get("score", 0)
            rev = ea.get("revenue_yoy", "不明")
            prof = ea.get("profit_yoy", "不明")
            summ = (ea.get("summary") or "")[:50]
            rows.append(f"  💹 決算スコア {score:+d} / 売上 {rev} / 利益 {prof}")
            if summ:
                rows.append(f"     {summ}")

        return "\n".join(rows)

    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    lines = [f"🔀 *クロスシグナル検出* — {now}\n"]

    if triple:
        lines.append("*━━ トリプル確認（3シグナル全一致・最高確信度）━━*")
        for c in triple:
            lines.append(_fmt(c))

    if double:
        lines.append("\n*━━ ダブル確認（2シグナル一致）━━*")
        for c in double:
            lines.append(_fmt(c))

    return send_slack_message("\n".join(lines))


def notify_error(error_message: str, context: str = "") -> bool:
    """
    エラー発生をSlackに通知する。

    Args:
        error_message (str): エラーメッセージ
        context (str): エラーが発生した処理の名前

    Returns:
        bool: 送信成功はTrue
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    text = f"""
⚠️ *エラー発生* [{context}]
{now}

{error_message}
""".strip()

    return send_slack_message(text)


def notify_edinet_daily_summary(stats: dict, force_send: bool = False) -> bool:
    """
    EDINET日次サマリーをSlackに送信する。

    Args:
        stats (dict):
            date           : 日付文字列（YYYY-MM-DD）
            total_fetched  : EDINET全書類取得件数（int|None）
            earnings_signals: 決算関連シグナル件数
            analyzed_ok    : Claude分析完了件数
            pdf_failed     : PDF取得失敗件数
            other_skipped  : その他スキップ件数
        force_send (bool): Trueのとき earnings_signals=0 でも送信する（夕方用）

    Returns:
        bool: 送信成功はTrue
    """
    date_str = stats.get("date", datetime.now().strftime("%Y-%m-%d"))
    total = stats.get("total_fetched")
    earnings_signals = stats.get("earnings_signals", 0)
    analyzed_ok = stats.get("analyzed_ok", 0)
    pdf_failed = stats.get("pdf_failed", 0)
    other_skipped = stats.get("other_skipped", 0)

    # 閑散期（シグナルなし）かつ強制送信でなければスキップ
    if earnings_signals == 0 and not force_send:
        logger.info("EDINET日次サマリー: シグナルなし（閑散期）のため通知スキップ")
        return True

    total_str = f"{total}件" if total is not None else "取得中"

    lines = [
        f"📋 *EDINET 今日の取得状況* （{date_str}）",
        f"  全書類: {total_str}",
        f"  決算関連シグナル: {earnings_signals}件",
        f"  Claude分析完了: {analyzed_ok}件",
    ]
    if pdf_failed > 0:
        lines.append(f"  ⚠️ PDF取得失敗: {pdf_failed}件")
    if other_skipped > 0:
        lines.append(f"  スキップ（訂正等）: {other_skipped}件")
    if earnings_signals == 0:
        lines.append("  ℹ️ 本日は決算シーズン外（閑散期）")

    return send_slack_message("\n".join(lines))


def notify_jquants_earnings_summary(signals: list) -> bool:
    """
    J-Quants決算速報の正サプライズシグナルをSlackに通知する。

    Args:
        signals (list): jquants_earnings_analyzer.analyze_todays_earnings() が返すシグナルリスト

    Returns:
        bool: 送信成功はTrue
    """
    if not signals:
        logger.info("J-Quants決算速報: 正サプライズなし → 通知スキップ")
        return True

    # スコア降順でソート
    signals_sorted = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)

    lines = [
        f"📊 *決算速報 正サプライズ* （{datetime.now().strftime('%m/%d %H:%M')}）",
        f"  J-Quants /fins/summary より {len(signals)}銘柄を検出",
        "",
    ]

    # DocTypeの日本語ラベル
    doctype_label = {"120": "通期", "130": "四半期", "140": "業績修正"}
    # シグナルの絵文字
    signal_emoji = {"STRONG_POSITIVE": "🔥", "POSITIVE": "📈", "NEUTRAL": "➡️", "NEGATIVE": "📉"}

    for s in signals_sorted:
        code = s.get("stockCode", "?")
        score = s.get("score", "-")
        sig = s.get("signal", "POSITIVE")
        reason = s.get("reason", "")
        op_yoy = s.get("op_yoy")
        progress = s.get("progress_rate")
        doc = doctype_label.get(s.get("docType", ""), s.get("docType", ""))
        emoji = signal_emoji.get(sig, "📈")

        metrics = []
        if op_yoy is not None:
            metrics.append(f"営業利益{op_yoy:+.1f}%")
        if progress is not None:
            metrics.append(f"進捗{progress:.0f}%")
        metrics_str = " / ".join(metrics) if metrics else ""

        lines.append(f"{emoji} *{code}* （{doc}） スコア{score}/5")
        if metrics_str:
            lines.append(f"  {metrics_str}")
        if reason:
            lines.append(f"  💬 {reason}")
        lines.append("")

    lines.append("_※ 翌朝の急騰シグナルと照合してqualifyパイプラインに接続予定_")

    return send_slack_message("\n".join(lines))


def notify_tdnet_signals(signals: list) -> bool:
    """
    TDnet適時開示のHaiku分類結果をSlackに通知する。

    Args:
        signals: tdnet_fetcher.analyze_disclosures_with_haiku() が返すリスト
                 各dict: code, company, title, time, label, reason

    Returns:
        bool: 送信成功はTrue
    """
    strong = [s for s in signals if s.get("label") == "STRONG"]
    watch = [s for s in signals if s.get("label") == "WATCH"]

    if not strong and not watch:
        logger.info("TDnet適時開示: モメンタム候補なし → 通知スキップ")
        return True

    lines = [
        f"📋 *TDnet適時開示スキャン* （{datetime.now().strftime('%m/%d %H:%M')}）",
        f"  ◎強気候補 {len(strong)}件 / ○要確認 {len(watch)}件",
        "",
    ]

    if strong:
        lines.append("*◎ モメンタム候補*")
        for s in strong[:10]:  # 上限10件
            lines.append(f"  🔥 *{s.get('code', '?')}* {s.get('company', '')}  {s.get('time', '')}")
            lines.append(f"  　{s.get('title', '')}")
            if s.get("reason"):
                lines.append(f"  　💬 {s['reason']}")
        if len(strong) > 10:
            lines.append(f"  … 他 {len(strong) - 10}件")
        lines.append("")

    if watch:
        # WATCHは件数＋銘柄コード一覧のみ（翌朝モメンタムスキャンとの照合用）
        watch_codes = " / ".join(
            f"{s.get('code', '?')} {s.get('company', '')[:8]}" for s in watch[:15]
        )
        suffix = f" 他{len(watch)-15}件" if len(watch) > 15 else ""
        lines.append(f"*○ 要確認*  {watch_codes}{suffix}")
        lines.append("")

    lines.append("_※ 翌朝モメンタムスキャンと照合して候補を絞り込んでください_")

    return send_slack_message("\n".join(lines))


def notify_double_signals(doubles: list) -> bool:
    """
    J-Quants数値サプライズ ＋ TDnet STRONG開示のダブルシグナルをSlackに通知する。

    Args:
        doubles: [{"jq": jq_signal_dict, "tdnet": tdnet_signal_dict}, ...] のリスト
                 jq: jquants_earnings_analyzer が返すシグナル
                 tdnet: tdnet_fetcher.analyze_disclosures_with_haiku が返すシグナル

    Returns:
        bool: 送信成功はTrue
    """
    if not doubles:
        return True

    # J-Quantsスコア降順でソート
    doubles_sorted = sorted(doubles, key=lambda x: x["jq"].get("score", 0), reverse=True)

    lines = [
        f"🚨 *ダブルシグナル検出* （{datetime.now().strftime('%m/%d %H:%M')}）",
        f"  数値サプライズ＋業績開示が同一銘柄で一致: {len(doubles)}銘柄",
        "",
    ]

    doctype_label = {"120": "通期", "130": "四半期", "140": "業績修正"}

    for d in doubles_sorted:
        jq = d["jq"]
        td = d["tdnet"]

        code = jq.get("stockCode", "?")
        score = jq.get("score", "-")
        op_yoy = jq.get("op_yoy")
        progress = jq.get("progress_rate")
        doc = doctype_label.get(jq.get("docType", ""), jq.get("docType", ""))
        jq_reason = jq.get("reason", "")

        metrics = []
        if op_yoy is not None:
            metrics.append(f"営業利益{op_yoy:+.1f}%")
        if progress is not None:
            metrics.append(f"進捗{progress:.0f}%")
        metrics_str = " / ".join(metrics) if metrics else ""

        lines.append(f"🔥🔥 *{code}*  J-Quantsスコア {score}/5")
        lines.append(f"  📊 数値（{doc}）: {metrics_str}")
        if jq_reason:
            lines.append(f"  　💬 {jq_reason}")
        lines.append(f"  📋 TDnet: {td.get('title', '')}  ({td.get('time', '')})")
        if td.get("reason"):
            lines.append(f"  　💬 {td['reason']}")
        lines.append("")

    lines.append("_※ 数値＋開示の両面一致。翌朝モメンタムスキャンの最優先確認銘柄_")

    return send_slack_message("\n".join(lines))


def notify_kakuho_update(
    new_jq_signals: list,
    new_tdnet_signals: list,
    doubles: list,
    prev_day_str: str,
) -> bool:
    """
    確報アップデート通知（06:30 JST）。

    前営業日の速報から差分のあったJ-Quants確報シグナル、
    TDnet引け後開示（18:30以降）、ダブルシグナルをまとめてSlack通知する。

    Args:
        new_jq_signals: 速報になかった新規確報シグナル（jquants_earnings_analyzerの出力）
        new_tdnet_signals: 18:30以降の引け後TDnet開示（Haiku分類済み）
        doubles: 確報×TDnet引け後ダブルシグナル
        prev_day_str: 対象日 "YYYY-MM-DD"

    Returns:
        bool: 送信成功はTrue
    """
    if not new_jq_signals and not new_tdnet_signals:
        logger.info("確報アップデート: 新規シグナルなし → 通知スキップ")
        return True

    lines = [
        f"📊 *確報アップデート* （{prev_day_str} 引け後）",
        "",
    ]

    # ---- J-Quants 確報（速報から新規追加分）----
    if new_jq_signals:
        lines.append(f"*🆕 J-Quants確報 新規 {len(new_jq_signals)}件*（速報に未掲載）")
        doctype_label = {"120": "通期", "130": "四半期", "140": "業績修正"}
        for s in sorted(new_jq_signals, key=lambda x: x.get("score", 0), reverse=True):
            code = s.get("stockCode", "?")
            score = s.get("score", "-")
            doc = doctype_label.get(s.get("docType", ""), s.get("docType", ""))
            op_yoy = s.get("op_yoy")
            progress = s.get("progress_rate")
            metrics = []
            if op_yoy is not None:
                metrics.append(f"営業利益{op_yoy:+.1f}%")
            if progress is not None:
                metrics.append(f"進捗{progress:.0f}%")
            metrics_str = " / ".join(metrics) if metrics else ""
            lines.append(f"  • *{code}* [{doc}] スコア{score}  {metrics_str}")
            if s.get("reason"):
                lines.append(f"  　💬 {s['reason']}")
        lines.append("")

    # ---- TDnet 引け後開示（18:30以降）----
    strong = [s for s in new_tdnet_signals if s.get("label") == "STRONG"]
    watch = [s for s in new_tdnet_signals if s.get("label") == "WATCH"]
    if new_tdnet_signals:
        lines.append(
            f"*📋 TDnet引け後開示  ◎{len(strong)}件 / ○{len(watch)}件*（18:30以降提出）"
        )
        for s in strong:
            lines.append(
                f"  🔥 *{s.get('code', '?')}* {s.get('company', '')}  {s.get('time', '')}"
            )
            lines.append(f"  　{s.get('title', '')}")
            if s.get("reason"):
                lines.append(f"  　💬 {s['reason']}")
        for s in watch:
            lines.append(
                f"  📌 *{s.get('code', '?')}* {s.get('company', '')}  {s.get('time', '')}"
            )
            lines.append(f"  　{s.get('title', '')}")
            if s.get("reason"):
                lines.append(f"  　💬 {s['reason']}")
        lines.append("")

    # ---- 確報 × TDnet引け後 ダブルシグナル ----
    if doubles:
        lines.append(f"*🚨 確報ダブルシグナル {len(doubles)}銘柄*")
        for d in sorted(doubles, key=lambda x: x["jq"].get("score", 0), reverse=True):
            jq = d["jq"]
            td = d["tdnet"]
            lines.append(
                f"  🔥🔥 *{jq.get('stockCode', '?')}*  J-Quantsスコア {jq.get('score', '-')}/5"
            )
            lines.append(f"  　📋 {td.get('title', '')}  ({td.get('time', '')})")
            if td.get("reason"):
                lines.append(f"  　💬 {td['reason']}")
        lines.append("")

    lines.append("_※ 朝スキャン前の最終確認。モメンタム候補の優先度を上げてください_")

    return send_slack_message("\n".join(lines))


# ========================================
# テスト送信
# ========================================

def send_test_message() -> bool:
    """
    Slack接続テスト用メッセージを送信する。
    python agents/slack_notifier.py で実行可能。
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    text = f"✅ Japan Momentum Agent - Slack接続テスト成功！\n{now}"
    return send_slack_message(text)


def notify_earnings_outcomes_recorded(newly_recorded: list) -> bool:
    """
    決算シグナルの5/10/20日後結果が新たに記録されたときにSlackへ通知する。

    Args:
        newly_recorded: record_earnings_outcomes()の第2戻り値リスト

    Returns:
        bool: 送信成功かどうか
    """
    if not newly_recorded:
        return True

    lines = ["📊 *決算シグナル フォローアップ結果*\n"]

    # daysKey でグループ化（5d/10d/20d）
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for r in newly_recorded:
        grouped[r["daysKey"]].append(r)

    for days_key in ["outcome5d", "outcome10d", "outcome20d"]:
        entries = grouped.get(days_key, [])
        if not entries:
            continue
        label = days_key.replace("outcome", "").replace("d", "営業日後")
        lines.append(f"*━━ {label} 結果 ━━*")
        for r in sorted(entries, key=lambda x: x.get("returnPct", 0), reverse=True):
            ret = r.get("returnPct", 0)
            icon = "✅" if ret > 5 else ("⚠️" if ret > 0 else "❌")
            lines.append(
                f"{icon} *{r['stockCode']} {r['companyName']}*"
                f"  {ret:+.1f}%"
                f"  （エントリー¥{r['entryPrice']:,.0f} → ¥{r['exitPrice']:,.0f}）"
                + (f"  [{r.get('catalystType', '')}]" if r.get("catalystType") else "")
            )

    return send_slack_message("\n".join(lines))


def notify_earnings_followup_status(followup_results: list) -> bool:
    """
    フォローアップ中の決算銘柄の現在パフォーマンスをSlackに通知する。

    Args:
        followup_results: get_followup_status()の戻り値

    Returns:
        bool: 送信成功かどうか
    """
    if not followup_results:
        return True

    lines = ["📈 *決算後 中長期フォローアップ（現況）*\n"]

    for r in followup_results:
        ret = r.get("returnPct")
        elapsed = r.get("bdays_elapsed", 0)
        entry_price = r.get("entryPrice", 0)
        current_price = r.get("currentPrice", 0)
        max_days = r.get("followupMaxBdays", 20)
        remaining = max_days - elapsed

        if ret is None:
            icon = "❓"
            ret_str = "データなし"
        elif ret >= 10:
            icon = "🚀"
            ret_str = f"{ret:+.1f}%"
        elif ret >= 3:
            icon = "✅"
            ret_str = f"{ret:+.1f}%"
        elif ret >= -3:
            icon = "➡️"
            ret_str = f"{ret:+.1f}%"
        elif ret >= -8:
            icon = "⚠️"
            ret_str = f"{ret:+.1f}%"
        else:
            icon = "❌"
            ret_str = f"{ret:+.1f}%"

        lines.append(
            f"{icon} *{r['stockCode']} {r['companyName']}*"
            f"  {ret_str}"
            f"  ¥{current_price:,.0f}（+{elapsed}日・残{remaining}日）"
        )
        if r.get("summary"):
            lines.append(f"  _{r['summary'][:40]}_")

    return send_slack_message("\n".join(lines))


def notify_noon_scan(results: list) -> bool:
    """
    正午スキャン（後場エントリー判断）の結果をSlackに送信する。

    Args:
        results: noon_scanner.run_noon_scan() の戻り値

    Returns:
        bool: 送信成功かどうか
    """
    jst = timezone(timedelta(hours=9))
    now_str = datetime.now(tz=jst).strftime("%H:%M")
    go_list   = [r for r in results if r["judgment"] == "後場GO"]
    watch_list = [r for r in results if r["judgment"] == "様子見"]
    skip_list  = [r for r in results if r["judgment"] == "見送り"]

    lines = [f"🕐 *後場エントリー判断レポート（{now_str}現在）*\n"]

    def _fmt(r: dict) -> str:
        intra = r.get("intradayData") or {}
        code = r["stockCode"]
        name = r.get("companyName", "")
        scan_close = r.get("scanClose", 0)
        current = intra.get("current_price", 0)
        mode_label = {"SHORT_TERM": "急騰", "MOMENTUM": "モメンタム", "EARNINGS": "決算"}.get(r["mode"], r["mode"])
        qualify_label = normalize_qualify_label(r.get("qualifyResult", ""))
        vp = r.get("volume_pattern", "unknown")
        vp_tag = " 📈バズ型" if vp == "late" else (" 🐸ジワジワ型" if vp == "early" else "")

        header = f"*{code} {name}* （{mode_label}{('・' + qualify_label) if qualify_label else ''}{vp_tag}）"
        price_str = (
            f"  前日終値: ¥{scan_close:,.0f} → 現在: ¥{current:,.0f}"
            f"  （{(current - scan_close) / scan_close * 100:+.1f}%）"
            if scan_close > 0 and current > 0 else ""
        )
        reasons_str = "\n".join(f"  {reason}" for reason in r.get("reasons", []))
        return "\n".join(filter(None, [header, price_str, reasons_str]))

    if go_list:
        lines.append("*━━ 後場エントリー推奨 ━━*")
        for r in go_list:
            lines.append(_fmt(r))
            intra = r.get("intradayData") or {}
            current = intra.get("current_price", 0)
            if current > 0:
                stop = round(current * 0.95)
                tp   = round(current * 1.15)
                lines.append(f"  📌 後場12:30エントリー目安 損切: ¥{stop:,}  利確: ¥{tp:,}")
            lines.append("")

    if watch_list:
        lines.append("*━━ 様子見（後場前半を確認） ━━*")
        for r in watch_list:
            lines.append(_fmt(r))
            lines.append("")

    if skip_list:
        lines.append("*━━ 一旦見送り（前場で失速） ━━*")
        for r in skip_list:
            code = r["stockCode"]
            name = r.get("companyName", "")
            top_reason = r["reasons"][0] if r["reasons"] else ""
            lines.append(f"{code} {name} — {top_reason}")

    if not results:
        lines.append("今日は対象銘柄なし（朝スキャンでシグナルなし）")

    text = "\n".join(lines)
    return send_slack_message(text)


def notify_actual_positions(positions: list) -> bool:
    """
    実売買ポジションの含み損益をSlackに通知する。

    Args:
        positions: paper_trader.get_actual_positions() の戻り値

    Returns:
        bool: 送信成功かどうか
    """
    if not positions:
        return True

    lines = ["📈 *実売買ポジション 損益確認*\n"]
    total_pnl = sum(p.get("unrealizedPnl", 0) for p in positions)

    for p in positions:
        ret = p.get("unrealizedPnlPct", 0)
        pnl = p.get("unrealizedPnl", 0)
        icon = "✅" if ret >= 5 else ("📊" if ret >= 0 else ("⚠️" if ret >= -3 else "❌"))
        current = p.get("currentPrice", 0)
        entry = p.get("entryPrice", 0)
        shares = p.get("shares", 0)
        lines.append(
            f"{icon} *{p['stockCode']} {p.get('companyName', '')}*\n"
            f"  取得: ¥{entry:,}×{shares}株  現在: ¥{current:,}\n"
            f"  損益: {'+' if pnl >= 0 else ''}{pnl:,.0f}円（{'+' if ret >= 0 else ''}{ret:.1f}%）"
            f"  損切: ¥{p['stopLossPrice']:,} / 利確: ¥{p['takeProfitPrice']:,}"
        )

    total_sign = "+" if total_pnl >= 0 else ""
    lines.append(f"\n合計含み損益: {total_sign}{total_pnl:,.0f}円（{len(positions)}銘柄）")
    return send_slack_message("\n".join(lines))


def _generate_weekly_observations(
    stats: dict,
    patterns: dict,
    week_signals: list,
    open_positions: list,
    weekly_trend: list = None,
) -> list:
    """週次レポートの注目点・提言を自動生成する。"""
    obs = []
    total = stats.get("total_recorded", 0)

    if total < 5:
        obs.append("📊 データ蓄積中（5件で精度分析開始・10件で本格稼働）")
        return obs

    # 継続判定の勝率チェック
    strong_stat = stats.get("継続", {})
    if strong_stat.get("count", 0) >= 3:
        wr = strong_stat.get("win_rate", 0)
        if wr < 40:
            obs.append(f"⚠️ 継続判定の勝率が低め({wr}%) → Stage1閾値の見直しを検討")
        elif wr >= 65:
            obs.append(f"✅ 継続判定の精度良好({wr}%) → 現行ロジック維持")

    # 情報源別パターン
    by_tag = patterns.get("by_surge_tag", {})
    if len(by_tag) >= 2:
        best_tag = max(by_tag.items(), key=lambda x: x[1]["win_rate"])
        worst_tag = min(by_tag.items(), key=lambda x: x[1]["win_rate"])
        if best_tag[0] != worst_tag[0]:
            diff = best_tag[1]["win_rate"] - worst_tag[1]["win_rate"]
            if diff >= 20:
                obs.append(
                    f"💡 {best_tag[0]}情報の精度が高い({best_tag[1]['win_rate']}%) "
                    f"vs {worst_tag[0]}({worst_tag[1]['win_rate']}%) "
                    f"→ TDnet開示あり銘柄を優先"
                )

    # 確信度別パターン
    by_conf = patterns.get("by_confidence", {})
    if "high" in by_conf and "low" in by_conf:
        high_wr = by_conf["high"]["win_rate"]
        low_wr = by_conf["low"]["win_rate"]
        if high_wr - low_wr >= 20:
            obs.append(
                f"💡 Claude high確信度({high_wr}%) vs low({low_wr}%)で大差 "
                f"→ low確信度は見送り検討"
            )

    # 今週のシグナル数
    strong_this_week = sum(
        1 for e in week_signals
        if normalize_qualify_label(e.get("qualifyResult", "")) == "継続"
    )
    if strong_this_week == 0:
        obs.append("📉 今週は継続判定ゼロ → 地合い弱含みか条件が厳しすぎる可能性")
    elif strong_this_week >= 5:
        obs.append(f"📈 今週は継続判定{strong_this_week}件 → 積極的な相場環境")

    # 実売買ポジション数（ペーパートレード廃止のため実売買のみ参照）
    try:
        from agents.paper_trader import get_actual_positions
        actual = get_actual_positions()
        if len(actual) >= 7:
            obs.append(f"⚠️ 実売買ポジション{len(actual)}件 → 新規追加は慎重に（余力限界近い）")
        elif len(actual) == 0:
            obs.append("📭 現在実売買ポジションなし → エントリーチャンスを積極的に狙える状態")
    except Exception:
        pass

    # 戦略有効性アラート（4週推移ベース）
    if weekly_trend and len(weekly_trend) >= 2:
        # シグナル枯渇の長期化チェック（直近3週以上ゼロ）
        recent_3w_strong = [w["strong_count"] for w in weekly_trend[-3:]]
        if len(recent_3w_strong) >= 3 and all(s == 0 for s in recent_3w_strong):
            obs.append(
                "🚨 3週連続で継続判定ゼロ → モメンタム相場の終了を検討。"
                "バリュー・ディフェンシブへの切替を視野に"
            )

        # 勝率の下降トレンド検出
        recent_wr = [w["win_rate"] for w in weekly_trend if w["win_rate"] is not None]
        if len(recent_wr) >= 3:
            if recent_wr[-1] < recent_wr[-2] < recent_wr[-3]:
                obs.append(
                    f"📉 勝率が3週連続低下中（{recent_wr[-3]}%→{recent_wr[-2]}%→{recent_wr[-1]}%）"
                    f" → 戦略の見直し時期かもしれません"
                )

        # 平均リターンの悪化チェック
        recent_ret = [w["avg_return"] for w in weekly_trend if w["avg_return"] is not None]
        if len(recent_ret) >= 2 and all(r < 0 for r in recent_ret[-2:]):
            obs.append(
                "⚠️ 直近2週の平均リターンがマイナス → ポジションサイズ縮小を検討"
            )

    if not obs:
        obs.append("特記事項なし（継続的なデータ蓄積を進めてください）")

    return obs


def notify_weekly_report() -> bool:
    """
    週次パフォーマンスレポートをSlackに送信する。
    直近7日間のシグナル精度・ペーパートレード状況・累計パターン分析を集計して通知。

    Returns:
        bool: 送信成功はTrue
    """
    import json
    from datetime import date, timedelta
    from pathlib import Path

    BASE_DIR = Path(__file__).parent.parent
    qualify_log_path = BASE_DIR / "memory" / "qualify_log.json"

    today      = date.today()
    week_start = (today - timedelta(days=7)).isoformat()
    now_str    = datetime.now().strftime("%Y年%m月%d日")

    # ---- qualify_log 集計 ----
    week_signals:  list = []
    week_outcomes: list = []
    try:
        with open(qualify_log_path, "r", encoding="utf-8") as f:
            all_qualify = json.load(f)
        week_signals = [e for e in all_qualify if e.get("scanDate", "") >= week_start]
        week_outcomes = [
            e for e in all_qualify
            if isinstance(e.get("outcome"), dict)
            and e["outcome"].get("status") == "recorded"
            and (e["outcome"].get("recordedAt") or "") >= week_start
        ]
    except FileNotFoundError:
        logger.warning("qualify_log.json が見つかりません。週次レポートの判定集計をスキップします。")
        send_slack_message("⚠️ 週次レポート: qualify_log.json が見つからないため判定集計をスキップしました。")
    except Exception as e:
        logger.error(f"qualify_log.json の読み込みに失敗しました: {e}")
        send_slack_message(f"⚠️ 週次レポート: qualify_log.json の読み込みエラー（{e}）")

    signal_count = len(week_signals)
    strong_count = sum(1 for e in week_signals if normalize_qualify_label(e.get("qualifyResult", "")) == "継続")
    watch_count  = sum(1 for e in week_signals if normalize_qualify_label(e.get("qualifyResult", "")) == "様子見")
    noise_count  = sum(1 for e in week_signals if normalize_qualify_label(e.get("qualifyResult", "")) in ("ノイズ", "一時的"))

    outcome_count = len(week_outcomes)
    outcome_wins  = sum(1 for e in week_outcomes if e["outcome"].get("isWin", False))
    outcome_wr    = round(outcome_wins / outcome_count * 100, 1) if outcome_count > 0 else None
    outcome_avg   = (
        round(sum(e["outcome"].get("returnPct", 0) for e in week_outcomes) / outcome_count, 2)
        if outcome_count > 0 else None
    )

    # ---- 累計パターン分析 ----
    from agents.momentum_qualifier import get_outcome_stats, get_outcome_patterns
    all_stats    = get_outcome_stats()
    all_patterns = get_outcome_patterns()
    total_rec    = all_stats.get("total_recorded", 0)

    # ---- 戦略有効性モニター（4週分の週別集計） ----
    weekly_trend = []  # [{week_label, signal_count, strong_count, outcome_count, win_rate, avg_return}, ...]
    try:
        for weeks_ago in range(3, -1, -1):  # 3週前→2週前→1週前→今週
            w_end   = today - timedelta(days=7 * weeks_ago)
            w_start = w_end - timedelta(days=7)
            w_label = w_end.strftime("%m/%d")

            w_signals = [
                e for e in all_qualify
                if w_start.isoformat() <= e.get("scanDate", "") < w_end.isoformat()
            ]
            w_strong = sum(
                1 for e in w_signals
                if normalize_qualify_label(e.get("qualifyResult", "")) == "継続"
            )
            w_outcomes = [
                e for e in all_qualify
                if isinstance(e.get("outcome"), dict)
                and e["outcome"].get("status") == "recorded"
                and w_start.isoformat() <= e.get("scanDate", "") < w_end.isoformat()
            ]
            w_oc_count = len(w_outcomes)
            w_wins     = sum(1 for e in w_outcomes if e["outcome"].get("isWin", False))
            w_wr       = round(w_wins / w_oc_count * 100, 1) if w_oc_count > 0 else None
            w_avg_ret  = (
                round(sum(e["outcome"].get("returnPct", 0) for e in w_outcomes) / w_oc_count, 2)
                if w_oc_count > 0 else None
            )

            weekly_trend.append({
                "week_label": w_label,
                "signal_count": len(w_signals),
                "strong_count": w_strong,
                "outcome_count": w_oc_count,
                "win_rate": w_wr,
                "avg_return": w_avg_ret,
            })
    except Exception:
        weekly_trend = []

    # ---- メッセージ組み立て ----
    lines = [
        f"📊 *週次パフォーマンスレポート* {now_str}",
        f"（対象期間: {week_start} 〜 {today.isoformat()}）",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # シグナル統計
    lines.append(f"🔍 *今週のシグナル（{signal_count}件）*")
    if signal_count > 0:
        lines.append(f"  継続: {strong_count}件 / 様子見: {watch_count}件 / ノイズ・一時的: {noise_count}件")
    else:
        lines.append("  シグナルなし（稼働日なし or データ蓄積中）")

    # 今週のoutcome
    lines.append(f"\n📈 *今週の判定実績（記録分: {outcome_count}件）*")
    if outcome_count > 0:
        wr_str  = f"{outcome_wr}%" if outcome_wr is not None else "N/A"
        ret_str = f"{outcome_avg:+.2f}%" if outcome_avg is not None else "N/A"
        lines.append(f"  勝率: {wr_str} / 平均リターン: {ret_str}")
    else:
        lines.append("  まだ記録なし（10営業日後に自動記録）")

    # 累計パターン分析
    lines.append(f"\n🧠 *累計パターン分析（{total_rec}件記録済み）*")
    if total_rec >= 5:
        for label in ["継続", "一時的", "ノイズ"]:
            s = all_stats.get(label, {})
            if s.get("count", 0) > 0:
                lines.append(
                    f"  {label}: 勝率{s['win_rate']}% / 平均{s['avg_return']:+.1f}%（{s['count']}件）"
                )
        if all_patterns.get("by_surge_tag"):
            lines.append("  【情報源別勝率】")
            for tag, s in sorted(all_patterns["by_surge_tag"].items(), key=lambda x: -x[1]["win_rate"]):
                lines.append(f"    {tag}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
        if all_patterns.get("by_confidence"):
            lines.append("  【Claude確信度別勝率】")
            for conf, s in sorted(all_patterns["by_confidence"].items(), key=lambda x: -x[1]["win_rate"]):
                lines.append(f"    {conf}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
        if all_patterns.get("by_volume_rate"):
            lines.append("  【出来高維持率別勝率】")
            for bucket, s in sorted(all_patterns["by_volume_rate"].items(), key=lambda x: -x[1]["win_rate"]):
                lines.append(f"    {bucket}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
        if all_patterns.get("by_volume_pattern"):
            vp_labels = {"late": "📈バズ型", "early": "🐸ジワジワ型"}
            lines.append("  【出来高パターン別勝率】")
            for vp, s in sorted(all_patterns["by_volume_pattern"].items(), key=lambda x: -x[1]["win_rate"]):
                lines.append(f"    {vp_labels.get(vp, vp)}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
    else:
        lines.append(f"  蓄積中（{total_rec}件 / 5件で分析開始）")

    # 戦略有効性モニター
    lines.append(f"\n🎯 *戦略有効性モニター（直近4週推移）*")
    if weekly_trend:
        has_any_outcome = any(w["outcome_count"] > 0 for w in weekly_trend)
        # ヘッダー行
        labels = [w["week_label"] for w in weekly_trend]
        lines.append(f"  週末日:     {' → '.join(labels)}")
        # シグナル数の推移
        sig_vals = [str(w["signal_count"]) for w in weekly_trend]
        lines.append(f"  シグナル数:  {' → '.join(sig_vals)}")
        # 継続判定数の推移
        str_vals = [str(w["strong_count"]) for w in weekly_trend]
        lines.append(f"  継続判定数:  {' → '.join(str_vals)}")
        # 勝率の推移（outcomeがある場合のみ表示）
        if has_any_outcome:
            wr_vals = [f"{w['win_rate']}%" if w["win_rate"] is not None else "-" for w in weekly_trend]
            lines.append(f"  勝率:       {' → '.join(wr_vals)}")
            ret_vals = [f"{w['avg_return']:+.2f}%" if w["avg_return"] is not None else "-" for w in weekly_trend]
            lines.append(f"  平均ﾘﾀｰﾝ:   {' → '.join(ret_vals)}")
        else:
            lines.append("  勝率・ﾘﾀｰﾝ: outcome蓄積待ち")

        # トレンド判定
        recent_strong = [w["strong_count"] for w in weekly_trend[-2:]]
        recent_wr = [w["win_rate"] for w in weekly_trend[-2:] if w["win_rate"] is not None]

        if all(s == 0 for s in recent_strong):
            lines.append("  ⚠️ 2週連続で継続判定ゼロ → モメンタム不在の可能性")
        elif len(recent_wr) >= 2 and all(wr < 50 for wr in recent_wr):
            lines.append("  ⚠️ 2週連続で勝率50%割れ → 戦略有効性の低下兆候")
        elif len(recent_wr) >= 2 and all(wr >= 60 for wr in recent_wr):
            lines.append("  ✅ モメンタム戦略が機能中")
        else:
            lines.append("  📊 データ蓄積中（推移を引き続き監視）")
    else:
        lines.append("  データ不足（qualify_log蓄積中）")

    # ---- モメンタム学習ループ（momentum_log.json） ----
    lines.append(f"\n📈 *モメンタム学習ループ*")
    try:
        from agents.momentum_log_manager import get_momentum_patterns, get_momentum_log_summary
        mo_summary = get_momentum_log_summary()
        mo_total_rec = mo_summary.get("total_recorded", 0)
        mo_total_log = mo_summary.get("total_logged", 0)
        mo_pending = mo_summary.get("total_pending", 0)
        lines.append(f"  記録: {mo_total_log}件（outcome待ち: {mo_pending}件 / 確定: {mo_total_rec}件）")

        mo_patterns = get_momentum_patterns()
        if mo_patterns.get("insufficient"):
            lines.append(f"  蓄積中（{mo_total_rec}件 / 5件で分析開始）")
        elif mo_patterns.get("overall"):
            ov = mo_patterns["overall"]
            lines.append(f"  全体: 勝率{ov['win_rate']}% / 平均{ov['avg_return']:+.1f}% / {ov['count']}件")
            if mo_patterns.get("by_ma_gap"):
                lines.append("  【MAギャップ別勝率（5-25MA）】")
                for k, s in sorted(mo_patterns["by_ma_gap"].items(), key=lambda x: -x[1]["win_rate"]):
                    lines.append(f"    {k}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
            if mo_patterns.get("by_high52w_ratio"):
                lines.append("  【52週高値比別勝率】")
                for k, s in sorted(mo_patterns["by_high52w_ratio"].items(), key=lambda x: -x[1]["win_rate"]):
                    lines.append(f"    {k}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
            if mo_patterns.get("by_volume_trend"):
                lines.append("  【出来高トレンド別勝率】")
                for k, s in sorted(mo_patterns["by_volume_trend"].items(), key=lambda x: -x[1]["win_rate"]):
                    lines.append(f"    {k}: {s['win_rate']}%（{s['count']}件, 平均{s['avg_return']:+.1f}%）")
        else:
            lines.append("  データなし")
    except Exception as e:
        lines.append(f"  ⚠️ momentum_log読み込みエラー（{e}）")

    # 注目点
    lines.append("\n📌 *注目点*")
    for obs in _generate_weekly_observations(all_stats, all_patterns, week_signals, [], weekly_trend):
        lines.append(f"  {obs}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    return send_slack_message("\n".join(lines))


if __name__ == "__main__":
    print("Slack接続テストを実行します...")
    success = send_test_message()
    if success:
        print("✅ テスト送信成功！SlackチャンネルにメッセージがAれているか確認してください。")
    else:
        print("❌ テスト送信失敗。config.yaml のWebhook URLを確認してください。")
