"""
main.py
Japan Momentum Agent - メインエントリーポイント

実行モード一覧:
  python main.py --mode scan              # 全スキャン（急騰+モメンタム+決算）
  python main.py --mode scan --type short    # 急騰スキャンのみ
  python main.py --mode scan --type momentum # モメンタムスキャンのみ
  python main.py --mode scan --type earnings # 決算スキャンのみ
  python main.py --mode backtest          # スキャン + バックテスト
  python main.py --mode full              # 全パイプライン実行
  python main.py --mode status            # ペーパートレード状況表示
  python main.py --mode fetch             # 株価データ取得のみ

作者: Japan Momentum Agent
"""

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

        days = getattr(args, "days", 60)
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

    # バックテストを実行してPFを計算してからSlack通知
    try:
        from agents.backtester import run_backtest
        from agents.slack_notifier import notify_new_signal
        import json
        from pathlib import Path

        for mode, results in all_results.items():
            if not results:
                continue

            # EARNINGS（決算）モードはバックテスト対象外なのでPF=0で通知
            if mode == "EARNINGS":
                notify_new_signal(results, mode=mode, profit_factor=0.0)
                continue

            # SHORT_TERM・MOMENTUMはバックテストを実行してPFを取得
            # ※ 当日スキャン結果では翌日データがないためPF=0になる問題を回避するため
            #   過去のスキャン結果ファイル（翌日以降のデータが存在するもの）を優先使用する
            try:
                pf = 0.0
                scans_dir = Path(__file__).parent / "data" / "processed" / "scans"
                # 過去スキャンファイルを日付降順で取得（当日分を除く最新2件を候補に）
                past_scan_files = sorted(
                    scans_dir.glob(f"scan_*_{mode}.json"), reverse=False
                )
                # 当日ファイル名を作成（除外用）
                today_str = datetime.now().strftime("%Y%m%d")

                past_results = []
                for f in past_scan_files:
                    if today_str in f.name:
                        continue  # 当日ファイルはスキップ
                    try:
                        with open(f, "r", encoding="utf-8") as fp:
                            data = json.load(fp)
                        past_results.extend(data.get("results", []))
                    except Exception:
                        continue

                if past_results:
                    logger.info(f"{mode}: 過去スキャン結果{len(past_results)}件でバックテスト実行...")
                    bt_result = run_backtest(past_results, lookback_days=90)
                    pf = bt_result.get("summary", {}).get("profitFactor", 0.0)
                    logger.info(f"{mode} PF（過去データ基準）: {pf:.2f}")
                else:
                    # 過去データがない場合は当日データでバックテスト（初回起動時など）
                    logger.info(f"{mode}: 過去スキャンデータなし。当日データでバックテスト実行...")
                    bt_result = run_backtest(results, lookback_days=90)
                    pf = bt_result.get("summary", {}).get("profitFactor", 0.0)
                    logger.info(f"{mode} PF: {pf:.2f}")

            except Exception as bt_err:
                logger.warning(f"{mode} バックテストエラー（PF=0で通知）: {bt_err}")
                pf = 0.0

            notify_new_signal(results, mode=mode, profit_factor=pf)

    except Exception as e:
        logger.warning(f"Slack通知送信失敗: {e}")

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
        choices=["fetch", "scan", "backtest", "full", "status"],
        help="実行モード: fetch(データ取得) / scan(スキャン) / backtest(バックテスト) / full(全実行) / status(状況表示)"
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
        default=60,
        help="データ取得日数（fetchモード用）。デフォルト: 60日"
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
