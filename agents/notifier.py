"""
agents/notifier.py
Discord通知モジュール

朝レポート（8:00）と夜レポート（20:00）をDiscordに送信する。
Webhook URLは環境変数 DISCORD_WEBHOOK_URL から読み込む。

使い方:
  python agents/notifier.py --report morning   # 朝レポート送信
  python agents/notifier.py --report evening   # 夜レポート送信
  python agents/notifier.py --test             # 接続テスト

作者: Japan Momentum Agent
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


# ========================================
# Webhook URL の取得
# ========================================

def get_webhook_url() -> str:
    """
    Webhook URLを取得する。
    環境変数 DISCORD_WEBHOOK_URL から読み込む。

    Returns:
        str: Webhook URL

    Raises:
        ValueError: URLが設定されていない場合
    """
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError(
            "エラー: 環境変数 DISCORD_WEBHOOK_URL が設定されていません。\n"
            "以下の手順で設定してください:\n"
            "  Windows: set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...\n"
            "  Mac/Linux: export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..."
        )
    return url


# ========================================
# Discord へのメッセージ送信
# ========================================

def send_discord_message(content: str = None, embeds: list = None) -> bool:
    """
    Discordにメッセージを送信する。

    Args:
        content (str): テキストメッセージ（省略可）
        embeds (list): Embedオブジェクトのリスト（省略可）

    Returns:
        bool: 送信成功はTrue、失敗はFalse
    """
    try:
        url = get_webhook_url()
    except ValueError as e:
        logger.error(str(e))
        return False

    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            logger.info("Discord送信成功")
            return True
        else:
            logger.error(f"Discord送信失敗: HTTP {resp.status_code} - {resp.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Discord送信エラー: {e}")
        return False


# ========================================
# ポートフォリオデータの読み込み
# ========================================

def _load_trade_log() -> dict:
    """
    trade_log.jsonを読み込む（内部関数）。

    Returns:
        dict: 取引ログ。ファイルが存在しない場合は空の辞書。
    """
    log_path = Path(__file__).parent.parent / "memory" / "trade_log.json"
    if not log_path.exists():
        return {}
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"trade_log.json の読み込みに失敗: {e}")
        return {}


def _load_latest_scan_results() -> dict:
    """
    最新のスキャン結果JSONを読み込む（内部関数）。
    data/processed/scans/ 内の最新ファイルを返す。

    Returns:
        dict: {モード名: [スキャン結果リスト]} の辞書
    """
    scan_dir = Path(__file__).parent.parent / "data" / "processed" / "scans"
    if not scan_dir.exists():
        return {}

    # 最新のスキャン結果を各モードから1件ずつ取得
    results = {}
    for mode in ["SHORT_TERM", "MOMENTUM", "EARNINGS"]:
        files = sorted(scan_dir.glob(f"scan_*_{mode}.json"), reverse=True)
        if files:
            try:
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                results[mode] = data.get("results", [])
            except Exception:
                results[mode] = []

    return results


# ========================================
# 朝レポート（8:00送信）
# ========================================

def send_morning_report() -> bool:
    """
    朝レポートをDiscordに送信する。
    内容: スキャン上位銘柄・注目銘柄リスト

    Returns:
        bool: 送信成功はTrue
    """
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    scan_results = _load_latest_scan_results()

    # ---- Embed 作成 ----
    fields = []

    # モメンタム上位3銘柄
    momentum_list = scan_results.get("MOMENTUM", [])[:3]
    if momentum_list:
        lines = []
        for i, r in enumerate(momentum_list, 1):
            code = r.get("stockCode", "N/A")
            name = r.get("companyName", "") or "N/A"
            score = r.get("score", 0)
            rsi = r.get("rsi14", 0)
            ratio = r.get("priceToHighRatio", 0)
            lines.append(f"`{i}.` **{code}** {name}\n　RSI: {rsi:.1f} / 高値比: {ratio:.1f}% / スコア: {score:.1f}")
        fields.append({
            "name": "📈 モメンタム上位3銘柄",
            "value": "\n".join(lines),
            "inline": False
        })
    else:
        fields.append({
            "name": "📈 モメンタム上位3銘柄",
            "value": "該当銘柄なし（スキャンデータなし）",
            "inline": False
        })

    # 急騰上位3銘柄
    short_list = scan_results.get("SHORT_TERM", [])[:3]
    if short_list:
        lines = []
        for i, r in enumerate(short_list, 1):
            code = r.get("stockCode", "N/A")
            name = r.get("companyName", "") or "N/A"
            change = r.get("priceChangePct", 0)
            vol = r.get("volumeRatio", 0)
            lines.append(f"`{i}.` **{code}** {name}\n　前日比: {change:+.1f}% / 出来高倍率: {vol:.1f}x")
        fields.append({
            "name": "🚀 急騰上位3銘柄",
            "value": "\n".join(lines),
            "inline": False
        })
    else:
        fields.append({
            "name": "🚀 急騰上位3銘柄",
            "value": "該当銘柄なし（スキャンデータなし）",
            "inline": False
        })

    # 保有ポジション数・含み損益の簡易サマリー
    trade_log = _load_trade_log()
    positions = trade_log.get("positions", [])
    if positions:
        total_unrealized = sum(p.get("unrealizedPnl", 0) for p in positions)
        sign = "+" if total_unrealized >= 0 else ""
        pos_lines = [f"保有: {len(positions)}銘柄　含み損益合計: **{sign}{total_unrealized:,.0f}円**"]
        for p in positions[:5]:  # 最大5件
            code = p.get("stockCode", "N/A")
            name = (p.get("companyName", "") or "N/A")[:10]
            pnl_pct = p.get("unrealizedPnlPct", 0)
            pnl_sign = "+" if pnl_pct >= 0 else ""
            days = p.get("holdDays", 0)
            pos_lines.append(f"　{code} {name}　{pnl_sign}{pnl_pct:.1f}%（{days}日目）")
        fields.append({
            "name": "💼 保有ポジション",
            "value": "\n".join(pos_lines),
            "inline": False
        })

    embed = {
        "title": "🌅 朝の投資レポート",
        "description": f"{now_str} のマーケット情報をお届けします",
        "color": 0x00BFFF,  # 青
        "fields": fields,
        "footer": {"text": "Japan Momentum Agent • 発注は必ずご自身でご確認ください"}
    }

    return send_discord_message(embeds=[embed])


# ========================================
# 夜レポート（20:00送信）
# ========================================

def send_evening_report() -> bool:
    """
    夜レポートをDiscordに送信する。
    内容: 保有ポジションの損益・損切り・利確ラインの確認

    Returns:
        bool: 送信成功はTrue
    """
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    trade_log = _load_trade_log()

    positions = trade_log.get("positions", [])
    summary = trade_log.get("summary", {})
    initial_capital = trade_log.get("initialCapital", 3000000)

    # 現金残高計算
    invested = sum(p.get("investedAmount", 0) for p in positions)
    realized_pnl = summary.get("total_pnl", 0)
    current_cash = initial_capital + realized_pnl - invested
    unrealized_pnl = sum(p.get("unrealizedPnl", 0) for p in positions)

    fields = []

    # 資金サマリー
    total_pnl = realized_pnl + unrealized_pnl
    pnl_sign = "+" if total_pnl >= 0 else ""
    fields.append({
        "name": "💰 資金状況",
        "value": (
            f"現金残高: **{current_cash:,.0f}円**\n"
            f"含み損益: {'+' if unrealized_pnl >= 0 else ''}{unrealized_pnl:,.0f}円\n"
            f"実現損益: {'+' if realized_pnl >= 0 else ''}{realized_pnl:,.0f}円\n"
            f"合計損益: **{pnl_sign}{total_pnl:,.0f}円**"
        ),
        "inline": False
    })

    # 保有ポジション詳細（要注意銘柄を強調）
    if positions:
        lines = []
        warning_count = 0
        for p in positions:
            code = p.get("stockCode", "N/A")
            name = (p.get("companyName", "") or "N/A")[:10]
            pnl_pct = p.get("unrealizedPnlPct", 0)
            days = p.get("holdDays", 0)
            entry = p.get("entryPrice", 0)
            stop = p.get("stopLossPrice", 0)
            take = p.get("takeProfitPrice", 0)
            current = p.get("currentPrice", entry)

            pnl_sign = "+" if pnl_pct >= 0 else ""

            # 損切りライン近接 or 保有日数が長い場合は警告マーク
            near_stop = current <= stop * 1.03  # 損切りまで3%以内
            near_days = days >= 8               # 残り2日以内

            if near_stop or near_days:
                prefix = "⚠️"
                warning_count += 1
            elif pnl_pct >= 10:
                prefix = "🎯"  # 利確ライン近接
            else:
                prefix = "▪️"

            lines.append(
                f"{prefix} **{code}** {name}\n"
                f"　{pnl_sign}{pnl_pct:.1f}%　{days}日目　"
                f"損切: {stop:,.0f}円 / 利確: {take:,.0f}円"
            )

        header = f"📊 保有ポジション（{len(positions)}銘柄）"
        if warning_count > 0:
            header += f"　⚠️ 要確認: {warning_count}銘柄"

        fields.append({
            "name": header,
            "value": "\n".join(lines),
            "inline": False
        })
    else:
        fields.append({
            "name": "📊 保有ポジション",
            "value": "現在保有なし",
            "inline": False
        })

    # 直近決済トレード（あれば）
    closed = trade_log.get("closed_trades", [])
    recent = [t for t in closed if t.get("exitDate") == datetime.now().strftime("%Y-%m-%d")]
    if recent:
        lines = []
        for t in recent:
            ret = t.get("returnPct", 0)
            sign = "+" if ret >= 0 else ""
            lines.append(f"{'✅' if ret > 0 else '❌'} {t.get('stockCode')} {sign}{ret:.1f}% ({t.get('exitReason', '')})")
        fields.append({
            "name": "📋 本日の決済",
            "value": "\n".join(lines),
            "inline": False
        })

    # 色: 含み損益がプラスなら緑、マイナスなら赤
    color = 0x00C851 if unrealized_pnl >= 0 else 0xFF4444

    embed = {
        "title": "🌙 夜の投資レポート",
        "description": f"{now_str} の終了時点サマリー",
        "color": color,
        "fields": fields,
        "footer": {"text": "Japan Momentum Agent • ⚠️マークの銘柄は明日要確認"}
    }

    return send_discord_message(embeds=[embed])


# ========================================
# 接続テスト
# ========================================

def send_test_message() -> bool:
    """
    Discordへの接続テストメッセージを送信する。

    Returns:
        bool: 送信成功はTrue
    """
    embed = {
        "title": "✅ 接続テスト成功！",
        "description": "Japan Momentum Agent からの通知が正常に届いています。",
        "color": 0x00C851,
        "fields": [
            {
                "name": "次のステップ",
                "value": "朝8:00と夜20:00に自動でレポートが届きます 📊",
                "inline": False
            }
        ],
        "footer": {"text": f"送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
    }
    return send_discord_message(embeds=[embed])


# ========================================
# コマンドライン実行
# ========================================

def main():
    parser = argparse.ArgumentParser(description="Discord通知送信")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", choices=["morning", "evening"], help="送信するレポートの種類")
    group.add_argument("--test", action="store_true", help="接続テストメッセージを送信")
    args = parser.parse_args()

    if args.test:
        success = send_test_message()
    elif args.report == "morning":
        success = send_morning_report()
    elif args.report == "evening":
        success = send_evening_report()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
