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
from datetime import datetime
from pathlib import Path

import yaml

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


def notify_new_signal(signals: list, mode: str, profit_factor: float) -> bool:
    """
    新規シグナル検出をSlackに通知する。

    Args:
        signals (list): シグナル銘柄リスト
        mode (str): スキャンモード（SHORT_TERM / MOMENTUM / EARNINGS）
        profit_factor (float): バックテストのプロフィットファクター

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
            lines.append(
                f"  • {code}{name_str}  スコア:{score:.1f}\n"
                f"    RSI:{rsi:.0f}  高値比:{high_ratio:.1f}%  新高値:{new_high}回/20日  出来高:{vol_icon}{vol_trend:.2f}x"
                f"{comment_str}"
            )
        else:
            price_chg = s.get("price_change_pct", 0)
            vol_ratio = s.get("volume_ratio", 0)
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

    text = f"""
🔔 *{mode_label} 検出！*
{now}

PF: *{profit_factor:.2f}*  |  対象: *{len(signals)}銘柄*

{signals_text}

{"⚠️ PF < 1.2のため発注見送り" if profit_factor < 1.2 else "✅ 発注条件クリア → ペーパートレード追加"}
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

        top_lines.append(
            f"{icon} *{i}位 +{score}点* {code} {name}{catalyst_str}\n"
            f"    売上:{revenue_yoy} / 営利:{profit_yoy} / 予想比:{vs_forecast}\n"
            f"    💬 {summary}{mom_str}"
        )

    # ========== メッセージ組み立て ==========
    top_text = "\n\n".join(top_lines) if top_lines else "  スコア30点以上の銘柄なし"

    # 低スコア・ネガティブはコンパクトな件数表示のみ
    footer_parts = []
    if low_positive:
        names = "・".join(f"{s.get('stockCode')}({s.get('score',0):+d})" for s in low_positive[:5])
        footer_parts.append(f"📊 低スコア(1〜29点): {len(low_positive)}件  {names}")
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


def notify_us_theme_extraction(theme_result: dict) -> bool:
    """
    米市場ホットキーワード・テーマ抽出結果をSlackに通知する。

    Args:
        theme_result (dict): us_theme_extractor.run_theme_extraction()の戻り値

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
    for i, kw in enumerate(hot_keywords[:8], 1):
        icon = mention_icons.get(kw.get("mention_level", "medium"), "📈")
        keyword_lines.append(
            f"  {icon} *{i}. {kw.get('keyword', '')}*  [{kw.get('sector', '')}]\n"
            f"     {kw.get('context', '')}"
        )
    keywords_text = "\n\n".join(keyword_lines) if keyword_lines else "  データなし"

    # ========== セクター別サブテーマ ==========
    momentum_icons = {"rising": "⬆️", "stable": "➡️", "falling": "⬇️"}
    narrative_lines = []
    for s in sector_narratives[:5]:
        icon = momentum_icons.get(s.get("momentum", "stable"), "➡️")
        narrative_lines.append(
            f"  {icon} *{s.get('sector', '')}* › {s.get('sub_theme', '')}\n"
            f"     {s.get('detail', '')}"
        )
    narratives_text = "\n\n".join(narrative_lines) if narrative_lines else "  データなし"

    # ========== 日本株への波及 ==========
    japan_lines = []
    for jp in japan_plays[:5]:
        stocks = "・".join(jp.get("stocks", [])[:3])
        japan_lines.append(
            f"  🇯🇵 *{jp.get('theme', '')}*\n"
            f"     注目: {stocks}\n"
            f"     {jp.get('reason', '')}"
        )
    japan_text = "\n\n".join(japan_lines) if japan_lines else "  なし"

    # ========== リスク ==========
    risk_text = "・".join(risk_keywords[:3]) if risk_keywords else "なし"

    text = f"""
🔍 *米市場 ホットキーワード*  {scan_date}
（ニュース{headline_count}件を分析）

━━━━━━━━━━━━━━━━━━
📌 *マクロ環境*
  {macro_narrative}

━━━━━━━━━━━━━━━━━━
🔥 *注目キーワード TOP8*

{keywords_text}

━━━━━━━━━━━━━━━━━━
🏭 *セクター別サブテーマ*

{narratives_text}

━━━━━━━━━━━━━━━━━━
🇯🇵 *日本株への波及チャンス*

{japan_text}

⚠️ *リスクワード*: {risk_text}
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
        "MOMENTUM": "中長期MO",
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
            qr = st.get("qualifyResult", "")
            reason = st.get("surgeReason", "")
            row = f"  📈 急騰 +{pct:.1f}% / 出来高 {vol:.1f}倍 [{qr}]"
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
    now_str = datetime.now().strftime("%H:%M")
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
        qualify_label = r.get("qualifyResult", "")

        header = f"*{code} {name}* （{mode_label}・{qualify_label}）"
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


if __name__ == "__main__":
    print("Slack接続テストを実行します...")
    success = send_test_message()
    if success:
        print("✅ テスト送信成功！SlackチャンネルにメッセージがAれているか確認してください。")
    else:
        print("❌ テスト送信失敗。config.yaml のWebhook URLを確認してください。")
