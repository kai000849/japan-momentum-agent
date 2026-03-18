from dotenv import load_dotenv
load_dotenv()

import argparse
import io
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Windows環境でUTF-8出力が文字化けしないよう設定
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ログ設定（INFOレベル以上を表示）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# バナー表示
# ========================================

BANNER = """
+======================================================+
|          Japan Momentum Agent  v1.0                  |
|          日本株モメンタムトレードAIエージェント       |
+======================================================+
"""


def print_banner():
    """起動バナーを表示する。"""
    print(BANNER)
    print(f"  実行日時: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}")
    print()


# ========================================
# PF計算ユーティリティ
# ========================================

def _calc_pf_for_mode(mode: str) -> float:
    """
    指定モードのPFを計算する。

    戦略:
      1. ローカルCSVデータの最終日から20・25・30営業日前の3時点でスキャン
         （複数日のシグナルを集約してPFの安定性を向上）
      2. 各日付でスキャン → 全シグナルを合算してバックテスト → PF取得
      3. ヒットなし/エラー時は0.0を返す

    Args:
        mode (str): スキャンモード（SHORT_TERM / MOMENTUM）

    Returns:
        float: プロフィットファクター
    """
    try:
        from agents.jquants_fetcher import load_latest_quotes
        from agents.scanner import run_scan
        from agents.backtester import run_backtest

        df_all = load_latest_quotes()
        if df_all.empty or "Date" not in df_all.columns:
            logger.warning(f"PF計算: 株価データなし → PF=0")
            return 0.0

        last_date = pd.to_datetime(df_all["Date"]).max()

        # 20・25・30営業日前の3時点でスキャンして集約（1日分だと不安定なため）
        all_bt_signals = []
        seen_keys = set()
        for bdays_back in [20, 25, 30]:
            offset = pd.tseries.offsets.BDay(bdays_back)
            bt_scan_date = (last_date - offset).strftime("%Y-%m-%d")
            logger.info(f"PF計算: {mode} スキャン日={bt_scan_date}（{bdays_back}営業日前）")
            try:
                results = run_scan(mode=mode, date=bt_scan_date)
                for r in results:
                    key = (r.get("stockCode", ""), r.get("scanDate", ""))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_bt_signals.append(r)
            except Exception as e:
                logger.warning(f"PF計算: {bt_scan_date} スキャンエラー（スキップ）: {e}")

        if not all_bt_signals:
            logger.warning(f"PF計算: {mode} 全日付でヒットなし → PF=0")
            return 0.0

        bt_result = run_backtest(all_bt_signals, lookback_days=90)
        pf = bt_result.get("summary", {}).get("profitFactor", 0.0)
        trade_count = bt_result.get("tradeCount", 0)
        logger.info(f"PF計算完了: {mode} PF={pf:.2f}（{len(all_bt_signals)}シグナル→{trade_count}トレード）")
        return pf

    except Exception as e:
        logger.warning(f"PF計算エラー（{mode}）: {e} → PF=0")
        return 0.0


# ========================================
# モード別処理関数
# ========================================

def run_fetch_mode(args):
    """
    株価データ取得モード。
    J-Quants APIからプライム市場全銘柄の株価データを取得してCSVに保存する。

    Args:
        args: コマンドライン引数
    """
    print("【データ取得モード】")
    print("J-Quants APIからプライム市場の株価データを取得します...")
    print("※ 銘柄数によっては数分かかる場合があります。\n")

    try:
        from agents.jquants_fetcher import fetch_all_stocks_data

        days = getattr(args, "days", 150)
        df = fetch_all_stocks_data(days=days)

        if df.empty:
            print("エラー: データを取得できませんでした。")
            print("config.yaml のJ-Quants認証情報を確認してください。")
            sys.exit(1)

        print(f"\n✓ データ取得完了: {len(df)}レコード")
        print(f"  保存先: data/raw/jquants/quotes_{datetime.now().strftime('%Y%m%d')}.csv")

    except Exception as e:
        print(f"\n✗ データ取得中にエラーが発生しました: {e}")
        sys.exit(1)


def run_scan_mode(args):
    """
    スクリーニングモード。
    指定されたモード（または全モード）でスクリーニングを実行する。

    Args:
        args: コマンドライン引数（args.type でスキャンタイプを指定）

    Returns:
        dict: {モード名: スキャン結果リスト} の辞書
    """
    from agents.scanner import (
        run_scan, display_top_results,
        MODE_SHORT_TERM, MODE_MOMENTUM, MODE_EARNINGS
    )

    # スキャンタイプに応じて実行するモードを決定
    scan_type = getattr(args, "type", "all") or "all"
    scan_date = getattr(args, "date", None)

    # スキャンタイプ → モード定数のマッピング
    type_to_mode = {
        "short": MODE_SHORT_TERM,
        "momentum": MODE_MOMENTUM,
        "earnings": MODE_EARNINGS,
    }

    # 実行するモードリストを作成
    if scan_type == "all":
        modes_to_run = [MODE_SHORT_TERM, MODE_MOMENTUM, MODE_EARNINGS]
    elif scan_type in type_to_mode:
        modes_to_run = [type_to_mode[scan_type]]
    else:
        print(f"エラー: 無効なスキャンタイプ '{scan_type}' です。")
        print("有効な値: short, momentum, earnings, all")
        sys.exit(1)

    print(f"【スキャンモード】")
    print(f"実行モード: {', '.join(modes_to_run)}")
    if scan_date:
        print(f"スキャン日付: {scan_date}")
    print()

    all_results = {}

    for mode in modes_to_run:
        print(f"--- {mode} スキャン開始 ---")
        try:
            results = run_scan(mode=mode, date=scan_date)
            all_results[mode] = results
            display_top_results(results, top_n=10)
        except Exception as e:
            print(f"エラー: {mode} スキャン中にエラーが発生しました。\n詳細: {e}")
            all_results[mode] = []

    # 合計ヒット数を表示
    total_hits = sum(len(r) for r in all_results.values())
    print(f"\n【スキャン完了】合計ヒット数: {total_hits}銘柄")

    # ---- モメンタム判定（SHORT_TERMシグナルを先に実行 → surgeReasonを通知に含める） ----
    short_term_results = all_results.get("SHORT_TERM", [])
    qualify_results = []
    df_all_cache = None
    if short_term_results:
        try:
            from agents.momentum_qualifier import qualify_signals, format_qualify_result_for_slack
            from agents.jquants_fetcher import load_latest_quotes

            logger.info("モメンタム判定を開始します...")
            df_all_cache = load_latest_quotes()

            if not df_all_cache.empty:
                qualify_results = qualify_signals(short_term_results, df_all_cache)
                # 急騰シグナル通知にsurgeReasonを含めるため結果を差し替え
                all_results["SHORT_TERM"] = qualify_results
            else:
                logger.warning("株価データが取得できずモメンタム判定をスキップ")

        except Exception as e:
            logger.warning(f"モメンタム判定エラー（スキップ）: {e}")

    # バックテストを実行してPFを計算してからSlack通知
    try:
        from agents.slack_notifier import notify_new_signal

        for mode, results in all_results.items():
            if not results:
                continue

            # EARNINGS（決算）モードはClaude APIで分析してスコアリング通知
            if mode == "EARNINGS":
                try:
                    from agents.edinet_analyzer import analyze_earnings_batch
                    from agents.earnings_momentum_scanner import save_watchlist
                    print("  決算書類をClaude APIで分析中...")
                    analyzed = analyze_earnings_batch(results)
                    analyzed_map = {r.get("stockCode"): r for r in analyzed}
                    merged = []
                    for r in results:
                        code = r.get("stockCode", "")
                        if code in analyzed_map:
                            merged.append({**r, **analyzed_map[code]})
                        else:
                            merged.append(r)
                    notify_new_signal(merged, mode=mode, profit_factor=0.0)
                    # 分析済み結果をクロスシグナル照合のために保存
                    all_results["EARNINGS"] = merged
                    # 翌朝ザラ場スキャン用ウォッチリストを保存
                    save_watchlist(merged)
                    # 決算・業績開示があった銘柄のモメンタムコメントを失効（再点検対象に）
                    try:
                        from agents.momentum_qualifier import invalidate_momentum_cache_for_codes
                        analyzed_codes = [r.get("stockCode") for r in merged if r.get("analyzed")]
                        if analyzed_codes:
                            invalidate_momentum_cache_for_codes(analyzed_codes)
                    except Exception as e:
                        logger.warning(f"モメンタムキャッシュ失効エラー（スキップ）: {e}")
                except Exception as e:
                    print(f"  決算分析エラー（簡易通知に切り替え）: {e}")
                    notify_new_signal(results, mode=mode, profit_factor=0.0)
                continue

            # MOMENTUMはキャッシュ付きコメント生成（新規銘柄のみAPI呼び出し）
            if mode == "MOMENTUM":
                try:
                    from agents.momentum_qualifier import generate_and_cache_momentum_comments
                    comment_map = generate_and_cache_momentum_comments(results)
                    for r in results:
                        r["comment"] = comment_map.get(r.get("stockCode", ""), "")
                except Exception as e:
                    logger.warning(f"モメンタムコメント生成エラー（スキップ）: {e}")

            # SHORT_TERM・MOMENTUMはバックテストでPFを計算してSlack通知
            # CSVの最終日から15営業日前の日付でスキャン→バックテスト→PF計算
            pf = _calc_pf_for_mode(mode)
            notify_new_signal(results, mode=mode, profit_factor=pf)

    except Exception as e:
        logger.warning(f"Slack通知送信失敗: {e}")

    # ---- クロスシグナル照合（3シグナルの重複銘柄を抽出） ----
    try:
        from agents.investment_advisor import find_cross_signals
        from agents.slack_notifier import notify_cross_signals
        cross = find_cross_signals(all_results)
        if cross:
            notify_cross_signals(cross)
            logger.info(f"クロスシグナル: {len(cross)}銘柄検出（TRIPLE:{sum(1 for c in cross if c['crossLevel']=='TRIPLE')} / DOUBLE:{sum(1 for c in cross if c['crossLevel']=='DOUBLE')}）")
        else:
            logger.info("クロスシグナル: 重複銘柄なし")
    except Exception as e:
        logger.warning(f"クロスシグナルエラー（スキップ）: {e}")

    # ---- モメンタム判定サマリーをSlackに送信 ----
    if qualify_results:
        try:
            from agents.momentum_qualifier import format_qualify_result_for_slack
            from agents.slack_notifier import send_slack_message

            slack_text = format_qualify_result_for_slack(qualify_results)
            send_slack_message(slack_text)
            logger.info("モメンタム判定結果をSlackに送信しました。")
        except Exception as e:
            logger.warning(f"モメンタム判定Slack送信エラー（スキップ）: {e}")

    # ---- フェーズ4: 投資判断エージェント ----
    # qualify結果・PF・ポートフォリオ余力・米市場シグナルを統合してエントリー推奨を生成
    short_term_results_for_advice = all_results.get("SHORT_TERM", [])
    if short_term_results_for_advice:
        try:
            from agents.momentum_qualifier import get_outcome_stats
            from agents.investment_advisor import generate_advice, format_advice_for_slack
            from agents.slack_notifier import send_slack_message

            # PFマップを作成
            pf_map = {
                "SHORT_TERM": _calc_pf_for_mode("SHORT_TERM"),
                "MOMENTUM": _calc_pf_for_mode("MOMENTUM"),
            }

            # qualify結果をqualify_logから取得（最新スキャン日分）
            from agents.momentum_qualifier import QUALIFY_LOG_PATH
            import json as _json
            qualify_entries = []
            if QUALIFY_LOG_PATH.exists():
                with open(QUALIFY_LOG_PATH, "r", encoding="utf-8") as _f:
                    _log = _json.load(_f)
                if _log:
                    latest_scan_date = max(e.get("scanDate", "") for e in _log)
                    qualify_entries = [e for e in _log if e.get("scanDate") == latest_scan_date]

            if qualify_entries:
                advices = generate_advice(qualify_entries, pf_map)
                advice_text = format_advice_for_slack(advices)
                if advice_text:
                    send_slack_message(advice_text)
                    logger.info("投資判断をSlackに送信しました。")
            else:
                logger.info("qualify結果がないため投資判断をスキップ")

        except Exception as e:
            logger.warning(f"投資判断エラー（スキップ）: {e}")

    return all_results


def run_backtest_mode(args):
    """
    バックテストモード。
    スキャンを実行した後、結果を使ってバックテストを行う。

    Args:
        args: コマンドライン引数

    Returns:
        dict: バックテスト結果
    """
    from agents.scanner import run_scan, MODE_SHORT_TERM, MODE_MOMENTUM
    from agents.backtester import run_backtest, display_backtest_summary, MAX_HOLD_DAYS
    from agents.jquants_fetcher import load_latest_quotes

    scan_type = getattr(args, "type", "momentum") or "momentum"
    scan_date = getattr(args, "date", None)

    print("【バックテストモード】")
    print(f"スキャンタイプ: {scan_type}")
    print()

    # スキャン日が未指定の場合は「CSVの最終日 - (MAX_HOLD_DAYS+3)営業日」を自動設定
    # → バックテストに必要な「シグナル日以降10日分」のデータを確保するため
    if scan_date is None:
        df_check = load_latest_quotes()
        if not df_check.empty and "Date" in df_check.columns:
            last_date = df_check["Date"].max()
            # 最終日からMAX_HOLD_DAYS+3営業日前をスキャン日とする
            offset = pd.tseries.offsets.BDay(MAX_HOLD_DAYS + 3)
            scan_date = (last_date - offset).strftime("%Y-%m-%d")
            print(
                f"  スキャン日を自動設定: {scan_date}\n"
                f"  （CSVの最終日{last_date.strftime('%Y-%m-%d')}から"
                f"{MAX_HOLD_DAYS+3}営業日前 → バックテスト用の将来データを確保）"
            )

    # スキャンタイプに応じてモードを決定
    type_to_mode = {
        "short": MODE_SHORT_TERM,
        "momentum": MODE_MOMENTUM,
    }
    mode = type_to_mode.get(scan_type, MODE_MOMENTUM)

    print(f"Step 1: {mode} スキャンを実行中...")
    try:
        scan_results = run_scan(mode=mode, date=scan_date)
        print(f"  スキャン完了: {len(scan_results)}銘柄がヒット")
    except Exception as e:
        print(f"エラー: スキャン中にエラーが発生しました。\n詳細: {e}")
        return {}

    if not scan_results:
        print("  ヒット銘柄がなかったため、バックテストをスキップします。")
        return {}

    print(f"\nStep 2: バックテストを実行中（{len(scan_results)}銘柄）...")
    try:
        lookback = getattr(args, "lookback", 90) or 90
        bt_results = run_backtest(scan_results, lookback_days=lookback)
        display_backtest_summary(bt_results)
        return bt_results
    except Exception as e:
        print(f"エラー: バックテスト中にエラーが発生しました。\n詳細: {e}")
        return {}


def run_full_mode(args):
    """
    全パイプライン実行モード。
    データ取得 → スキャン → バックテスト → ペーパートレード判定を一括実行する。

    Args:
        args: コマンドライン引数
    """
    from agents.scanner import (
        run_scan, display_top_results,
        MODE_SHORT_TERM, MODE_MOMENTUM, MODE_EARNINGS
    )
    from agents.backtester import run_backtest, display_backtest_summary
    from agents.paper_trader import PaperTrader

    print("【フルパイプライン実行モード】")
    print("全ステップを順番に実行します...\n")

    scan_date = getattr(args, "date", None)

    # バックテスト用スキャン日の自動設定
    # （CSVの最終日そのままではバックテスト用の将来データが0日になるため）
    if scan_date is None:
        from agents.jquants_fetcher import load_latest_quotes
        from agents.backtester import MAX_HOLD_DAYS
        df_check = load_latest_quotes()
        if not df_check.empty and "Date" in df_check.columns:
            last_date = df_check["Date"].max()
            offset = pd.tseries.offsets.BDay(MAX_HOLD_DAYS + 3)
            scan_date = (last_date - offset).strftime("%Y-%m-%d")
            print(
                f"  スキャン日を自動設定: {scan_date}\n"
                f"  （CSVの最終日{last_date.strftime('%Y-%m-%d')}から"
                f"{MAX_HOLD_DAYS+3}営業日前 → バックテスト用将来データを確保）\n"
            )

    # ========== Step 1: スキャン ==========
    print("=" * 50)
    print("Step 1/3: スクリーニング実行")
    print("=" * 50)

    all_scan_results = {}
    for mode in [MODE_SHORT_TERM, MODE_MOMENTUM, MODE_EARNINGS]:
        print(f"\n  {mode} スキャン中...")
        try:
            results = run_scan(mode=mode, date=scan_date)
            all_scan_results[mode] = results
            print(f"  ✓ {mode}: {len(results)}銘柄ヒット")
        except Exception as e:
            print(f"  ✗ {mode} エラー: {e}")
            all_scan_results[mode] = []

    # ========== Step 2: バックテスト ==========
    print("\n" + "=" * 50)
    print("Step 2/3: バックテスト実行")
    print("=" * 50)

    bt_results_all = {}
    for mode in [MODE_SHORT_TERM, MODE_MOMENTUM]:
        scan_results = all_scan_results.get(mode, [])
        if not scan_results:
            print(f"\n  {mode}: ヒット銘柄なし。スキップ。")
            continue

        print(f"\n  {mode} のバックテスト中（{len(scan_results)}銘柄）...")
        try:
            bt_result = run_backtest(scan_results, lookback_days=90)
            bt_results_all[mode] = bt_result
            summary = bt_result.get("summary", {})
            pf = summary.get("profitFactor", 0)
            win_rate = summary.get("winRate", 0)
            print(f"  ✓ PF: {pf:.2f} / 勝率: {win_rate:.1f}%")
        except Exception as e:
            print(f"  ✗ {mode} バックテストエラー: {e}")

    # ========== Step 3: ペーパートレード判定 ==========
    print("\n" + "=" * 50)
    print("Step 3/3: ペーパートレード判定")
    print("=" * 50)

    trader = PaperTrader()
    added_count = 0

    for mode, bt_result in bt_results_all.items():
        summary = bt_result.get("summary", {})
        pf = summary.get("profitFactor", 0)

        # プロフィットファクター基準を満たすモードのシグナルのみ発注
        if pf < 1.2:
            print(f"\n  {mode}: PF({pf:.2f}) < 1.2 のため発注見送り")
            continue

        print(f"\n  {mode}: PF({pf:.2f}) >= 1.2 → 発注対象")

        # スキャン結果の上位銘柄をペーパートレードに追加
        scan_results = all_scan_results.get(mode, [])[:5]  # 上位5銘柄まで

        for signal in scan_results:
            stock_code = signal.get("stockCode", "")
            entry_price = signal.get("close", 0)
            company_name = signal.get("companyName", "")

            if not stock_code or not entry_price:
                continue

            success = trader.add_position(
                stock_code=stock_code,
                entry_price=entry_price,
                reason=f"{mode}シグナル（PF:{pf:.2f}）",
                company_name=company_name,
                profit_factor=pf
            )
            if success:
                added_count += 1

    print(f"\n  新規ポジション追加数: {added_count}件")

    # ポートフォリオ状況を表示
    print("\n" + "=" * 50)
    print("最終ポートフォリオ状況")
    print("=" * 50)
    trader.display_portfolio_status()

    print("✓ 全パイプライン実行完了")


def run_status_mode(args):
    """
    ペーパートレード状況表示モード。
    現在のポジション・損益を表示する。

    Args:
        args: コマンドライン引数
    """
    from agents.paper_trader import display_portfolio_status, PaperTrader

    print("【ペーパートレード状況】")
    display_portfolio_status()

    # Slack通知: 朝次レポート送信（--notify オプション指定時）
    notify = getattr(args, "notify", False)
    if notify:
        try:
            from agents.slack_notifier import notify_daily_report
            trader = PaperTrader()
            success = notify_daily_report(trader.trade_log, trader.initial_capital)
            if success:
                print("✅ Slackに朝次レポートを送信しました。")
            else:
                print("❌ Slack送信失敗。config.yaml のWebhook URLを確認してください。")
        except Exception as e:
            print(f"Slack通知エラー: {e}")


# ========================================
# コマンドライン引数パーサー
# ========================================

def create_parser() -> argparse.ArgumentParser:
    """
    コマンドライン引数パーサーを作成する。

    Returns:
        argparse.ArgumentParser: パーサーオブジェクト
    """
    parser = argparse.ArgumentParser(
        description="Japan Momentum Agent - 日本株モメンタムトレードAIエージェント",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python main.py --mode status              # ポートフォリオ状況確認
  python main.py --mode fetch               # 株価データを取得（最初に実行）
  python main.py --mode scan                # 全スキャン実行
  python main.py --mode scan --type short   # 急騰スキャンのみ
  python main.py --mode scan --type momentum # モメンタムスキャンのみ
  python main.py --mode scan --type earnings # 決算スキャンのみ
  python main.py --mode backtest            # スキャン＋バックテスト
  python main.py --mode full                # 全パイプライン実行
        """
    )

    # 必須: 実行モード
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["fetch", "scan", "backtest", "full", "status", "us_scan", "qualify_report", "earnings_intraday", "earnings_endofday"],
        help="実行モード: fetch / scan / backtest / full / status / us_scan / qualify_report / earnings_intraday(ザラ場) / earnings_endofday(引け後)"
    )

    # オプション: スキャンタイプ
    parser.add_argument(
        "--type",
        type=str,
        default=None,
        choices=["short", "momentum", "earnings", "all"],
        help="スキャンタイプ: short(急騰) / momentum(モメンタム) / earnings(決算) / all(全て)"
    )

    # オプション: 日付指定
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="スキャン日付（形式: YYYY-MM-DD）。省略時は今日"
    )

    # オプション: バックテスト期間
    parser.add_argument(
        "--lookback",
        type=int,
        default=90,
        help="バックテストの対象期間（日数）。デフォルト: 90日"
    )

    # オプション: データ取得日数
    parser.add_argument(
        "--days",
        type=int,
        default=150,
        help="データ取得日数（fetchモード用）。デフォルト: 150日（75日MA計算に必要）"
    )

    # オプション: Slack通知送信フラグ
    parser.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="Slack通知を送信する（statusモード用）"
    )

    return parser


# ========================================
# メイン処理
# ========================================

def main():
    """
    メイン処理。コマンドライン引数に応じて各モードを実行する。
    """
    print_banner()

    # 引数パーサーを作成・解析
    parser = create_parser()
    args = parser.parse_args()

    logger.info(f"実行モード: {args.mode}")

    # config.yamlの存在確認
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        print("エラー: config.yaml が見つかりません。")
        print("config.yaml を作成してAPIキーを設定してください。")
        print("（config.yaml.example を参考にしてください）")
        sys.exit(1)

    # ---- モード別処理の実行 ----
    try:
        if args.mode == "fetch":
            # データ取得モード
            run_fetch_mode(args)

        elif args.mode == "scan":
            # スキャンモード
            run_scan_mode(args)

        elif args.mode == "backtest":
            # バックテストモード
            run_backtest_mode(args)

        elif args.mode == "full":
            # 全パイプライン実行モード
            run_full_mode(args)

        elif args.mode == "status":
            # ポートフォリオ状況表示モード
            run_status_mode(args)

        elif args.mode == "us_scan":
            # 米市場セクター・テーマスキャン
            print("【米市場スキャンモード】")
            print("① 米国セクターETFモメンタム分析")
            print("② 米国財務メディアからホットキーワード抽出\n")
            from agents.us_market_scanner import run_us_market_scan
            from agents.us_theme_extractor import run_theme_extraction
            from agents.slack_notifier import notify_us_market_scan, notify_us_theme_extraction

            # ETFモメンタムスキャン
            print("--- ETFモメンタム取得中 ---")
            scan_result = run_us_market_scan()
            ranking = scan_result.get("sector_ranking", [])
            print(f"セクターETF取得: {len(ranking)}本")
            if ranking:
                for s in ranking[:5]:
                    print(f"  {s['name']}({s['ticker']}): "
                          f"スコア{'+' if s['score']>=0 else ''}{s['score']:.1f} "
                          f"[5日:{'+' if s['mom5d']>=0 else ''}{s['mom5d']:.1f}%]")
            notify_us_market_scan(scan_result)
            print("✓ ETFスキャン通知送信\n")

            # テーマ・キーワード抽出
            print("--- ホットキーワード抽出中 ---")
            theme_result = run_theme_extraction()
            kw_count = len(theme_result.get("keywords", {}).get("hot_keywords", []))
            print(f"キーワード抽出: {kw_count}件")
            notify_us_theme_extraction(theme_result)
            print("✓ キーワード通知送信")

        elif args.mode == "earnings_intraday":
            # ザラ場決算モメンタムスキャン（10:30 JST頃に実行）
            print("【ザラ場決算モメンタムスキャン】")
            print("前日決算発表銘柄の前場反応を確認してエントリー判断します...\n")
            from agents.earnings_momentum_scanner import (
                run_intraday_earnings_scan, record_earnings_outcomes,
                get_earnings_accuracy_stats
            )
            from agents.slack_notifier import notify_intraday_earnings_scan
            from agents.jquants_fetcher import load_latest_quotes

            # 学習ループ: 過去シグナルの結果を記録
            try:
                df_all = load_latest_quotes()
                if not df_all.empty:
                    updated = record_earnings_outcomes(df_all)
                    if updated > 0:
                        print(f"  過去シグナルの結果記録: {updated}件更新")
            except Exception as e:
                logger.warning(f"結果記録エラー（スキップ）: {e}")

            # ザラ場スキャン実行
            scan_results = run_intraday_earnings_scan()
            print(f"  スキャン完了: {len(scan_results)}銘柄")
            for r in scan_results[:5]:
                intra = r.get("intradayData", {})
                print(
                    f"  {r['entryJudgment']:15s}  {r['stockCode']} {r['companyName'][:10]}"
                    f"  総合:{r['totalScore']:.0f}"
                    f"  ギャップ:{intra.get('opening_gap_pct', 0):+.1f}%"
                    f"  前場:{intra.get('intraday_momentum_pct', 0):+.1f}%"
                )

            stats = get_earnings_accuracy_stats()
            notify_intraday_earnings_scan(scan_results, stats)
            print("✓ Slack通知送信完了")

        elif args.mode == "earnings_endofday":
            # 引け後決算モメンタム評価（18:00 JST頃に実行・J-Quants日足使用）
            print("【引け後決算モメンタム評価】")
            print("前日・当日決算発表銘柄の一日の値動きをJ-Quantsデータで評価します...\n")
            from agents.earnings_momentum_scanner import (
                run_endofday_earnings_scan, record_earnings_outcomes,
                get_earnings_accuracy_stats
            )
            from agents.slack_notifier import notify_endofday_earnings_scan
            from agents.jquants_fetcher import load_latest_quotes

            df_all = load_latest_quotes()
            if df_all.empty:
                print("エラー: 株価データが取得できませんでした。先に --mode fetch を実行してください。")
                sys.exit(1)

            # 学習ループ: 過去シグナルの結果を記録
            try:
                updated = record_earnings_outcomes(df_all)
                if updated > 0:
                    print(f"  過去シグナルの結果記録: {updated}件更新")
            except Exception as e:
                logger.warning(f"結果記録エラー（スキップ）: {e}")

            # 引け後スキャン実行
            scan_results = run_endofday_earnings_scan(df_all)
            print(f"  スキャン完了: {len(scan_results)}銘柄")
            for r in scan_results[:5]:
                eod = r.get("eodData", {})
                print(
                    f"  {r['entryJudgment']:15s}  {r['stockCode']} {r['companyName'][:10]}"
                    f"  総合:{r['totalScore']:.0f}"
                    f"  前日比:{eod.get('day_return_pct', 0):+.1f}%"
                    f"  {eod.get('candle_pattern', '')}"
                )

            stats = get_earnings_accuracy_stats()
            notify_endofday_earnings_scan(scan_results, stats)
            print("✓ Slack通知送信完了")

        elif args.mode == "qualify_report":
            # 判定精度レポートモード
            print("【判定精度レポートモード】")
            from agents.momentum_qualifier import get_outcome_stats, QUALIFY_LOG_PATH
            import json

            # qualify_log.json の内容サマリーを表示
            if QUALIFY_LOG_PATH.exists():
                with open(QUALIFY_LOG_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                total = len(entries)
                recorded = sum(1 for e in entries if e.get("outcome") and e.get("outcome", {}).get("status") == "recorded")
                pending = sum(1 for e in entries if e.get("outcome") is None)
                print(f"\n📊 qualify_log.json 状況")
                print(f"  総エントリ数   : {total}件")
                print(f"  outcome記録済み: {recorded}件")
                print(f"  outcome待ち    : {pending}件")
                print()
            else:
                print("qualify_log.json が存在しません。")

            stats = get_outcome_stats()
            total_recorded = stats.get("total_recorded", 0)
            if total_recorded == 0:
                print("⚠️  まだoutcome記録済みのエントリがありません。")
                print("   （10営業日後に自動記録されます）")
            else:
                print(f"📈 判定精度サマリー（{total_recorded}件記録済み）")
                for label in ["STRONG", "WEAK", "WATCH", "NOISE"]:
                    s = stats.get(label, {})
                    count = s.get("count", 0)
                    if count > 0:
                        wr = s.get("win_rate")
                        ar = s.get("avg_return")
                        print(f"  {label:6s}: 勝率{wr:.1f}% / 平均リターン{ar:+.2f}%（{count}件）")
                print()

                # Slack通知も送る（--notifyオプション付きの場合）
                if args.notify:
                    from agents.slack_notifier import send_slack_message
                    lines = [f"📊 *判定精度レポート（{total_recorded}件記録済み）*"]
                    for label in ["STRONG", "WEAK", "WATCH", "NOISE"]:
                        s = stats.get(label, {})
                        count = s.get("count", 0)
                        if count > 0:
                            wr = s.get("win_rate")
                            ar = s.get("avg_return")
                            lines.append(f"  {label}: 勝率{wr:.1f}% / 平均{ar:+.2f}%（{count}件）")
                    send_slack_message("\n".join(lines))
                    print("✓ Slack通知を送信しました")

        else:
            print(f"エラー: 無効なモード '{args.mode}' です。")
            parser.print_help()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n処理を中断しました。")
        sys.exit(0)
    except Exception as e:
        print(f"\n予期しないエラーが発生しました: {e}")
        logger.exception("詳細エラー情報:")
        sys.exit(1)

    print("\n処理が完了しました。")


# ========================================
# エントリーポイント
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のエントリーポイント。
    python main.py --mode <モード> で実行する。
    """
    main()
