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

    mode_label = {
        "SHORT_TERM": "🚀 急騰シグナル",
        "MOMENTUM": "📈 モメンタムシグナル",
        "EARNINGS": "📋 決算シグナル",
    }.get(mode, mode)

    lines = []
    for s in signals[:10]:  # 最大10件
        code = s.get("stockCode", "")
        name = s.get("companyName", "")[:12]
        close = s.get("close", 0)
        score = s.get("score", s.get("momentum_score", 0))
        lines.append(f"  • {code} {name}  ¥{close:,.0f}  スコア:{score:.1f}")

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
