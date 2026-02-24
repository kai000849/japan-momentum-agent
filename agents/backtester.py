"""
agents/backtester.py
バックテストモジュール

過去のスクリーニング結果を使って、仮想的なトレードをシミュレーションし、
戦略の有効性を検証する。

【バックテストルール】
- エントリー: シグナル翌営業日の始値
- イグジット: 以下のいずれかが先に成立した時点
  1. 保有10営業日経過（時間切れ）
  2. -5%損切り（ストップロス）
  3. +15%利確（テイクプロフィット）

【計算指標】
- 勝率: 利益が出たトレード数 / 総トレード数
- 平均リターン: 各トレードリターンの平均値
- 最大連敗数: 連続して負けた最大回数
- プロフィットファクター: 総利益 / 総損失

作者: Japan Momentum Agent
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# 同プロジェクト内のモジュールをインポート
from agents.jquants_fetcher import get_daily_quotes, load_latest_quotes

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# バックテスト定数
# ========================================

STOP_LOSS_PCT = -5.0      # 損切りライン（-5%）
TAKE_PROFIT_PCT = 15.0    # 利確ライン（+15%）
MAX_HOLD_DAYS = 10        # 最大保有営業日数
MIN_PROFIT_FACTOR = 1.2   # ペーパートレード発注条件（PF >= 1.2）


# ========================================
# バックテスト実行
# ========================================

def run_backtest(scan_results: list, lookback_days: int = 90) -> dict:
    """
    スキャン結果に対してバックテストを実行する。

    Args:
        scan_results (list): スキャン結果のリスト（scanner.run_scan()の戻り値）
        lookback_days (int): バックテスト対象の過去日数（デフォルト: 90日）

    Returns:
        dict: バックテスト結果
              主なキー: trades（個別取引リスト）, summary（集計結果）

    処理の流れ:
        1. 各シグナル銘柄の翌日以降の株価を取得
        2. イグジット条件を確認しながら仮想トレードをシミュレート
        3. 統計指標を計算
    """
    if not scan_results:
        logger.warning("警告: スキャン結果が空です。バックテストをスキップします。")
        return {"trades": [], "summary": {}, "profitFactor": 0}

    logger.info(f"バックテスト開始: {len(scan_results)}銘柄シグナル")

    # バックテスト期間を決定
    earliest_date = min(r.get("scanDate", "") for r in scan_results)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = earliest_date  # シグナル日から開始

    logger.info(f"バックテスト期間: {from_date} 〜 {to_date}")

    # ローカルに株価データがあれば読み込む（API節約）
    df_all = load_latest_quotes()
    use_local_data = not df_all.empty

    if not use_local_data:
        logger.warning("ローカル株価データがありません。APIから取得します（時間がかかります）。")

    # 各シグナルに対してバックテストを実行
    all_trades = []

    for i, signal in enumerate(scan_results):
        stock_code = signal.get("stockCode", "")
        scan_date = signal.get("scanDate", "")

        if not stock_code or not scan_date:
            continue

        logger.debug(f"バックテスト [{i+1}/{len(scan_results)}]: {stock_code}")

        try:
            # シグナル日以降の株価データを取得
            if use_local_data:
                # ローカルデータから抽出
                code_col = "Code" if "Code" in df_all.columns else df_all.columns[0]
                stock_df = df_all[df_all[code_col].astype(str) == str(stock_code)].copy()
                stock_df = stock_df.sort_values("Date").reset_index(drop=True)
            else:
                # APIから取得
                stock_df = get_daily_quotes(stock_code, from_date, to_date)

            if stock_df.empty:
                logger.debug(f"銘柄 {stock_code}: 株価データなし。スキップ。")
                continue

            # シグナル日の翌営業日以降のデータを取得
            signal_dt = pd.to_datetime(scan_date)
            future_df = stock_df[stock_df["Date"] > signal_dt].reset_index(drop=True)

            if len(future_df) < 2:
                logger.debug(f"銘柄 {stock_code}: シグナル日以降のデータが不足。スキップ。")
                continue

            # エントリー: 翌営業日の始値
            entry_row = future_df.iloc[0]
            entry_price = float(entry_row.get("Open", 0) or entry_row.get("Close", 0))

            if entry_price <= 0:
                continue

            entry_date = entry_row["Date"].strftime("%Y-%m-%d")

            # イグジット判定
            exit_price, exit_date, exit_reason, hold_days = _simulate_exit(
                future_df, entry_price
            )

            # リターン計算
            if exit_price and exit_price > 0:
                return_pct = ((exit_price - entry_price) / entry_price) * 100

                trade = {
                    "stockCode": stock_code,
                    "companyName": signal.get("companyName", ""),
                    "mode": signal.get("mode", ""),
                    "scanDate": scan_date,
                    "score": signal.get("score", 0),
                    "entryDate": entry_date,
                    "entryPrice": round(entry_price, 0),
                    "exitDate": exit_date,
                    "exitPrice": round(exit_price, 0),
                    "exitReason": exit_reason,
                    "holdDays": hold_days,
                    "returnPct": round(return_pct, 2),
                    "isWin": return_pct > 0
                }
                all_trades.append(trade)

        except Exception as e:
            logger.warning(f"銘柄 {stock_code} のバックテスト中にエラー（スキップ）: {e}")
            continue

    # 統計サマリーを計算
    summary = _calculate_summary(all_trades)

    # バックテスト実行モードを記録
    bt_mode = scan_results[0].get("mode", "UNKNOWN") if scan_results else "UNKNOWN"
    result = {
        "mode": bt_mode,
        "backtestedAt": datetime.now().isoformat(),
        "signalCount": len(scan_results),
        "tradeCount": len(all_trades),
        "summary": summary,
        "trades": all_trades
    }

    # 結果を保存
    save_date = datetime.now().strftime("%Y-%m-%d")
    _save_backtest_results(result, save_date, bt_mode)

    logger.info(f"バックテスト完了: {len(all_trades)}件のトレードをシミュレートしました。")
    return result


def _simulate_exit(future_df: pd.DataFrame, entry_price: float) -> tuple:
    """
    イグジット条件を確認しながら仮想トレードのイグジットを決定する（内部関数）。

    イグジット条件（先に成立したものを採用）:
    1. -5%損切り（ストップロス）
    2. +15%利確（テイクプロフィット）
    3. 10営業日保有後に終値で決済

    Args:
        future_df (pd.DataFrame): エントリー日以降の株価データ
        entry_price (float): エントリー価格（翌日始値）

    Returns:
        tuple: (exit_price, exit_date, exit_reason, hold_days)
               exit_price: イグジット価格
               exit_date: イグジット日付（YYYY-MM-DD）
               exit_reason: イグジット理由
               hold_days: 保有日数
    """
    # ストップロス・テイクプロフィットの絶対価格を計算
    stop_loss_price = entry_price * (1 + STOP_LOSS_PCT / 100)    # 損切り価格
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)  # 利確価格

    for hold_days, (_, row) in enumerate(future_df.iterrows(), 1):
        # 当日の始値・高値・安値・終値を取得
        row_open = float(row.get("Open", 0) or 0)
        row_high = float(row.get("High", 0) or 0)
        row_low = float(row.get("Low", 0) or 0)
        row_close = float(row.get("Close", 0) or 0)
        row_date = row["Date"].strftime("%Y-%m-%d")

        if row_close <= 0:
            continue

        # ---- 損切り判定（日中安値がストップロス価格を下回った場合）----
        if row_low > 0 and row_low <= stop_loss_price:
            # ギャップダウンで始値から下落した場合は始値でイグジット
            actual_exit = min(row_open, stop_loss_price) if row_open > 0 else stop_loss_price
            return actual_exit, row_date, "損切り(-5%)", hold_days

        # ---- 利確判定（日中高値が利確価格を上回った場合）----
        if row_high > 0 and row_high >= take_profit_price:
            return take_profit_price, row_date, "利確(+15%)", hold_days

        # ---- 時間切れ判定（10営業日保有後）----
        if hold_days >= MAX_HOLD_DAYS:
            return row_close, row_date, "時間切れ(10日)", hold_days

    # データが途中で終わった場合は最終データの終値でイグジット
    if len(future_df) > 0:
        last_row = future_df.iloc[-1]
        last_close = float(last_row.get("Close", 0) or 0)
        last_date = last_row["Date"].strftime("%Y-%m-%d")
        return last_close, last_date, "データ終端", len(future_df)

    return None, None, "データなし", 0


def _calculate_summary(trades: list) -> dict:
    """
    個別トレードリストからバックテスト統計サマリーを計算する（内部関数）。

    Args:
        trades (list): 個別トレードのリスト

    Returns:
        dict: 統計サマリー
              主なキー: winRate, avgReturn, maxLossStreak,
                       profitFactor, totalReturn等
    """
    if not trades:
        return {
            "totalTrades": 0,
            "winningTrades": 0,
            "losingTrades": 0,
            "winRate": 0.0,
            "avgReturn": 0.0,
            "totalReturn": 0.0,
            "maxLossStreak": 0,
            "profitFactor": 0.0,
            "message": "トレードデータなし"
        }

    returns = [t["returnPct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    # 勝率
    win_rate = (len(wins) / len(trades)) * 100 if trades else 0

    # 平均リターン
    avg_return = np.mean(returns) if returns else 0

    # 合計リターン
    total_return = sum(returns)

    # 最大連敗数
    max_loss_streak = _calc_max_loss_streak(trades)

    # プロフィットファクター = 総利益 / 総損失
    total_profit = sum(wins) if wins else 0
    total_loss = abs(sum(losses)) if losses else 0
    profit_factor = (total_profit / total_loss) if total_loss > 0 else float('inf')
    # 無限大の場合は上限を設ける
    if profit_factor == float('inf'):
        profit_factor = 99.99

    return {
        "totalTrades": len(trades),
        "winningTrades": len(wins),
        "losingTrades": len(losses),
        "winRate": round(win_rate, 2),
        "avgReturn": round(avg_return, 2),
        "totalReturn": round(total_return, 2),
        "maxLossStreak": max_loss_streak,
        "profitFactor": round(profit_factor, 2),
        "maxReturn": round(max(returns), 2) if returns else 0,
        "minReturn": round(min(returns), 2) if returns else 0,
        "meetsMinProfitFactor": profit_factor >= MIN_PROFIT_FACTOR
    }


def _calc_max_loss_streak(trades: list) -> int:
    """
    最大連敗数を計算する（内部関数）。

    Args:
        trades (list): トレードリスト

    Returns:
        int: 最大連続負けトレード数
    """
    max_streak = 0
    current_streak = 0

    for trade in trades:
        if not trade.get("isWin", False):
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak


def _save_backtest_results(results: dict, date: str, mode: str) -> Path:
    """
    バックテスト結果をJSONファイルに保存する（内部関数）。

    Args:
        results (dict): バックテスト結果
        date (str): 日付（YYYY-MM-DD）
        mode (str): スキャンモード

    Returns:
        Path: 保存ファイルパス
    """
    save_dir = Path(__file__).parent.parent / "data" / "processed" / "backtests"
    save_dir.mkdir(parents=True, exist_ok=True)

    date_str = date.replace("-", "")
    file_name = f"backtest_{date_str}_{mode}.json"
    save_path = save_dir / file_name

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info(f"バックテスト結果を保存しました: {save_path}")
    return save_path


# ========================================
# 結果表示
# ========================================

def display_backtest_summary(results: dict) -> None:
    """
    バックテスト結果のサマリーをコンソールに表示する。

    Args:
        results (dict): run_backtest()の戻り値
    """
    summary = results.get("summary", {})
    trades = results.get("trades", [])
    mode = results.get("mode", "UNKNOWN")

    print(f"\n{'='*55}")
    print(f"  バックテスト結果サマリー [{mode}]")
    print(f"{'='*55}")
    print(f"  総トレード数  : {summary.get('totalTrades', 0)}件")
    print(f"  勝ちトレード  : {summary.get('winningTrades', 0)}件")
    print(f"  負けトレード  : {summary.get('losingTrades', 0)}件")
    print(f"  勝率          : {summary.get('winRate', 0):.1f}%")
    print(f"  平均リターン  : {summary.get('avgReturn', 0):+.2f}%")
    print(f"  最大リターン  : {summary.get('maxReturn', 0):+.2f}%")
    print(f"  最小リターン  : {summary.get('minReturn', 0):+.2f}%")
    print(f"  最大連敗数    : {summary.get('maxLossStreak', 0)}連敗")
    print(f"  PF（利益/損失）: {summary.get('profitFactor', 0):.2f}")

    # プロフィットファクター判定
    pf = summary.get("profitFactor", 0)
    if pf >= 2.0:
        pf_comment = "優秀 ✓"
    elif pf >= 1.2:
        pf_comment = "合格（ペーパートレード対象）✓"
    elif pf >= 1.0:
        pf_comment = "収支トントン（要改善）"
    else:
        pf_comment = "損失超過（使用不可）✗"
    print(f"  PF評価        : {pf_comment}")
    print(f"{'='*55}")

    # 個別トレードを上位5件表示
    if trades:
        print(f"\n  【個別トレード詳細 - 上位5件】")
        print(f"  {'銘柄コード':>8} {'エントリー':>12} {'イグジット':>12} {'リターン':>8} {'理由':<15}")
        print(f"  {'-'*60}")
        # リターン順に並べて上位5件
        top_trades = sorted(trades, key=lambda x: x.get("returnPct", 0), reverse=True)[:5]
        for t in top_trades:
            print(
                f"  {t.get('stockCode', 'N/A'):>8} "
                f"{t.get('entryDate', 'N/A'):>12} "
                f"{t.get('exitDate', 'N/A'):>12} "
                f"{t.get('returnPct', 0):>+7.2f}% "
                f"{t.get('exitReason', 'N/A'):<15}"
            )

    print()


# ========================================
# メイン（単体テスト用）
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のテスト処理
    使い方: python agents/backtester.py

    事前にスキャンを実行してdata/processed/scans/にJSONファイルを保存しておくこと
    """
    from pathlib import Path

    print("=" * 55)
    print("Backtester - 動作テスト")
    print("=" * 55)

    # 最新のスキャン結果ファイルを探す
    scans_dir = Path(__file__).parent.parent / "data" / "processed" / "scans"
    scan_files = sorted(scans_dir.glob("scan_*.json"), reverse=True)

    if not scan_files:
        print("\n✗ スキャン結果ファイルが見つかりません。")
        print("先に以下を実行してください:")
        print("  python main.py --mode scan")
    else:
        latest_scan = scan_files[0]
        print(f"\n最新のスキャン結果を使用: {latest_scan.name}")

        with open(latest_scan, "r", encoding="utf-8") as f:
            scan_data = json.load(f)

        scan_results = scan_data.get("results", [])
        print(f"シグナル数: {len(scan_results)}銘柄")

        try:
            results = run_backtest(scan_results, lookback_days=90)
            display_backtest_summary(results)
            print("✓ バックテスト完了")

        except Exception as e:
            print(f"\n✗ エラーが発生しました: {e}")
