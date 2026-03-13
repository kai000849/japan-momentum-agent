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
        logger.error(f"Slack送信エラー: {e}")
        return False
    except Exception as e:
        logger.error(f"Slack送信中に予期しないエラー: {e}")
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
            lines.append(
                f"  • {code}{name_str}  スコア:{score:.1f}\n"
                f"    RSI:{rsi:.0f}  高値比:{high_ratio:.1f}%  新高値:{new_high}回/20日  出来高:{vol_icon}{vol_trend:.2f}x"
            )
        else:
            lines.append(f"  • {code}{name_str}  ¥{close:,.0f}  スコア:{score:.1f}")

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
        # 未分析のみの場合（APIキー未設定など）→ 旧来の簡易表示
        lines = []
        for s in signals[:20]:
            code = s.get("stockCode", "")
            name = (s.get("companyName") or "")[:15]
            doc_desc = s.get("docDescription", "")[:20] or "決算"
            lines.append(f"  • {code} {name}  [{doc_desc}]")

        text = f"""
📋 *決算シグナル 検出！*
{now}

対象: *{len(signals)}銘柄*（未分析・APIキー設定で詳細分析が有効になります）

{"　".join(lines[:20])}
""".strip()
        return send_slack_message(text)

    # ベスト/ワーストを抽出
    best = sorted([s for s in analyzed if s.get("score", 0) > 0],
                  key=lambda x: x.get("score", 0), reverse=True)[:10]
    worst = sorted([s for s in analyzed if s.get("score", 0) < 0],
                   key=lambda x: x.get("score", 0))[:10]
    neutral = [s for s in analyzed if s.get("score", 0) == 0]

    # ========== ベスト部分 ==========
    best_lines = []
    for i, s in enumerate(best, 1):
        code = s.get("stockCode", "")
        name = (s.get("companyName") or "")[:15]
        score = s.get("score", 0)
        revenue_yoy = s.get("revenue_yoy", "不明")
        profit_yoy = s.get("profit_yoy", "不明")
        vs_forecast = s.get("vs_forecast", "不明")
        summary = s.get("summary", "")
        structural = s.get("structural_change", False)
        structural_comment = s.get("structural_comment", "")

        # スコアに応じたアイコン
        if score >= 80:
            icon = "🔥"
        elif score >= 50:
            icon = "🟢"
        else:
            icon = "🔼"

        struct_str = f"\n    🔄 {structural_comment}" if structural and structural_comment else ""

        best_lines.append(
            f"{icon} *{i}位 +{score}点* {code} {name}\n"
            f"    売上:{revenue_yoy} / 営利:{profit_yoy} / 予想比:{vs_forecast}\n"
            f"    💬 {summary}{struct_str}"
        )

    # ========== ワースト部分 ==========
    worst_lines = []
    for i, s in enumerate(worst, 1):
        code = s.get("stockCode", "")
        name = (s.get("companyName") or "")[:15]
        score = s.get("score", 0)
        revenue_yoy = s.get("revenue_yoy", "不明")
        profit_yoy = s.get("profit_yoy", "不明")
        vs_forecast = s.get("vs_forecast", "不明")
        summary = s.get("summary", "")
        structural = s.get("structural_change", False)
        structural_comment = s.get("structural_comment", "")

        if score <= -80:
            icon = "💀"
        elif score <= -50:
            icon = "🔴"
        else:
            icon = "🔽"

        struct_str = f"\n    ⚠️ {structural_comment}" if structural and structural_comment else ""

        worst_lines.append(
            f"{icon} *{i}位 {score}点* {code} {name}\n"
            f"    売上:{revenue_yoy} / 営利:{profit_yoy} / 予想比:{vs_forecast}\n"
            f"    💬 {summary}{struct_str}"
        )

    # ========== メッセージ組み立て ==========
    best_text = "\n\n".join(best_lines) if best_lines else "  ポジティブな決算なし"
    worst_text = "\n\n".join(worst_lines) if worst_lines else "  ネガティブな決算なし"

    unanalyzed_str = ""
    if unanalyzed:
        unanalyzed_str = f"\n\n📄 未分析（PDF取得不可等）: {len(unanalyzed)}件"

    text = f"""
📊 *決算スコアリング {now}*
分析済み: {len(analyzed)}件 | 総件数: {len(signals)}件

━━━━━━━━━━━━━━━━━━
🟢 *ポジティブ TOP{len(best)}*

{best_text}

━━━━━━━━━━━━━━━━━━
🔴 *ネガティブ TOP{len(worst)}*

{worst_text}{unanalyzed_str}
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
        change = data.get("change5d", 0)
        icon = "📈" if change >= 1 else ("📉" if change <= -1 else "➡️")
        sign = "+" if change >= 0 else ""
        macro_lines.append(f"  {icon} {data['name']}: {sign}{change:.1f}%")
    macro_text = "\n".join(macro_lines) if macro_lines else "  データなし"

    # ========== セクターランキング上位5・下位3 ==========
    top5 = sector_ranking[:5]
    bottom3 = [s for s in sector_ranking if s.get("score", 0) < 0][-3:]

    top_lines = []
    for i, s in enumerate(top5, 1):
        score = s.get("score", 0)
        mom5 = s.get("mom5d", 0)
        mom20 = s.get("mom20d", 0)
        vol = s.get("vol_trend", 1.0)
        vol_icon = "📶" if vol >= 1.3 else ("➡️" if vol >= 0.9 else "📉")
        sign5 = "+" if mom5 >= 0 else ""
        sign20 = "+" if mom20 >= 0 else ""
        japan = s.get("japan_theme", "")
        top_lines.append(
            f"  *{i}. {s['name']}({s['ticker']})*  スコア:{score:.1f}\n"
            f"     5日:{sign5}{mom5:.1f}% / 20日:{sign20}{mom20:.1f}%  出来高:{vol_icon}{vol:.2f}x\n"
            f"     🇯🇵 {japan}"
        )

    bottom_lines = []
    for s in bottom3:
        mom5 = s.get("mom5d", 0)
        sign5 = "+" if mom5 >= 0 else ""
        bottom_lines.append(
            f"  🔻 {s['name']}({s['ticker']}): {sign5}{mom5:.1f}%/5日"
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


if __name__ == "__main__":
    print("Slack接続テストを実行します...")
    success = send_test_message()
    if success:
        print("✅ テスト送信成功！SlackチャンネルにメッセージがAれているか確認してください。")
    else:
        print("❌ テスト送信失敗。config.yaml のWebhook URLを確認してください。")
