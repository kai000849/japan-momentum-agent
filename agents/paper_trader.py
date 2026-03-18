"""
agents/paper_trader.py
ペーパートレード（仮想取引）管理モジュール

仮想資金を使って、実際の市場データでシミュレーショントレードを行う。
本番トレードへの移行前に戦略の実力を検証するためのモジュール。

【取引ルール】
- 仮想資金: 300万円
- 1銘柄最大投資額: 30万円
- 同時保有最大銘柄数: 10銘柄
- 発注条件: プロフィットファクター >= 1.2のシグナルのみ

【イグジット条件】（backtester.pyと同じルール）
- -5%損切り
- +15%利確
- 10営業日保有後

作者: Japan Momentum Agent
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import yaml

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

STOP_LOSS_PCT = -5.0      # 損切りライン
TAKE_PROFIT_PCT = 15.0    # 利確ライン
MAX_HOLD_DAYS = 10        # 最大保有営業日数
MIN_PROFIT_FACTOR = 1.2   # 発注条件（プロフィットファクター）


# ========================================
# 設定読み込み
# ========================================

def load_config() -> dict:
    """
    config.yamlから設定を読み込む。

    Returns:
        dict: 設定内容
    """
    config_path = Path(__file__).parent.parent / "config.yaml"
    if not config_path.exists():
        # デフォルト設定を返す
        return {
            "paper_trading": {
                "initial_capital": 3000000,
                "max_position_size": 300000,
                "max_positions": 10
            }
        }
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ========================================
# ペーパートレード管理クラス
# ========================================

class PaperTrader:
    """
    ペーパートレードを管理するクラス。
    ポジションの追加・更新・決済を行い、取引履歴をJSONに記録する。
    """

    def __init__(self):
        """
        PaperTraderを初期化する。
        設定ファイルから資金・制限値を読み込み、取引ログを読み込む。
        """
        # 設定読み込み
        config = load_config()
        pt_config = config.get("paper_trading", {})

        self.initial_capital = pt_config.get("initial_capital", 3000000)   # 初期資金
        self.max_position_size = pt_config.get("max_position_size", 300000)  # 1銘柄最大
        self.max_positions = pt_config.get("max_positions", 10)             # 最大同時保有数

        # 取引ログファイルのパス
        self.log_path = Path(__file__).parent.parent / "memory" / "trade_log.json"

        # 取引ログを読み込む（存在しない場合は初期化）
        self.trade_log = self._load_trade_log()

    def _load_trade_log(self) -> dict:
        """
        取引ログJSONファイルを読み込む（内部メソッド）。

        Returns:
            dict: 取引ログ
        """
        if self.log_path.exists():
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("警告: trade_log.jsonが破損しています。新規作成します。")

        # 初期構造を返す
        return {
            "initialCapital": self.initial_capital,
            "positions": [],          # 現在保有中のポジション
            "closed_trades": [],      # 決済済みトレード
            "summary": {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0,       # 総損益（円）
                "last_updated": None
            }
        }

    def _save_trade_log(self) -> None:
        """
        取引ログをJSONファイルに保存する（内部メソッド）。
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_log["summary"]["last_updated"] = datetime.now().isoformat()

        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(self.trade_log, f, ensure_ascii=False, indent=2)

        logger.debug(f"取引ログを保存しました: {self.log_path}")

    def _get_current_cash(self) -> float:
        """
        現在の手持ち現金を計算する（内部メソッド）。
        初期資金 - 現在のポジション評価額（取得価格ベース）

        Returns:
            float: 現在の現金残高（円）
        """
        positions = self.trade_log.get("positions", [])
        invested = sum(
            p.get("investedAmount", 0) for p in positions
        )
        return self.initial_capital + self.trade_log["summary"]["total_pnl"] - invested

    # ========================================
    # ポジション管理
    # ========================================

    def add_position(
        self,
        stock_code: str,
        entry_price: float,
        reason: str,
        company_name: str = "",
        profit_factor: float = None
    ) -> bool:
        """
        新規ポジションを追加する。

        Args:
            stock_code (str): 銘柄コード（例: "7203"）
            entry_price (float): エントリー価格（円）
            reason (str): エントリー理由（例: "急騰シグナル"）
            company_name (str): 会社名（表示用）
            profit_factor (float): バックテストのプロフィットファクター。
                                   Noneの場合はPFチェックをスキップ

        Returns:
            bool: ポジション追加成功はTrue、失敗はFalse
        """
        positions = self.trade_log.get("positions", [])

        # ---- 発注条件チェック ----

        # 1. PFチェック（MIN_PROFIT_FACTOR以上のシグナルのみ発注）
        if profit_factor is not None and profit_factor < MIN_PROFIT_FACTOR:
            logger.warning(
                f"発注見送り: {stock_code} のPF({profit_factor:.2f})が"
                f"基準値({MIN_PROFIT_FACTOR})未満です。"
            )
            return False

        # 2. 同銘柄の重複チェック
        existing_codes = [p.get("stockCode") for p in positions]
        if stock_code in existing_codes:
            logger.warning(f"発注見送り: {stock_code} は既に保有中です。")
            return False

        # 3. 最大ポジション数チェック
        if len(positions) >= self.max_positions:
            logger.warning(
                f"発注見送り: 最大保有銘柄数({self.max_positions}銘柄)に達しています。"
            )
            return False

        # 4. 資金チェック
        invest_amount = min(self.max_position_size, entry_price * 100)  # 最低100株単位
        current_cash = _get_cash_balance(self.trade_log, self.initial_capital)
        if current_cash < invest_amount:
            logger.warning(
                f"発注見送り: 資金不足。必要: {invest_amount:,.0f}円, "
                f"利用可能: {current_cash:,.0f}円"
            )
            return False

        # ---- ポジション追加 ----

        # 投資口数を計算（最大投資額を1株価格で割る）
        shares = int(self.max_position_size / entry_price)
        if shares <= 0:
            shares = 1  # 最低1株

        actual_invest = entry_price * shares

        # 損切り・利確価格を計算
        stop_loss_price = entry_price * (1 + STOP_LOSS_PCT / 100)
        take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)

        new_position = {
            "stockCode": stock_code,
            "companyName": company_name,
            "entryDate": datetime.now().strftime("%Y-%m-%d"),
            "entryPrice": round(entry_price, 0),
            "shares": shares,
            "investedAmount": round(actual_invest, 0),    # 投資額（円）
            "stopLossPrice": round(stop_loss_price, 0),   # 損切り価格
            "takeProfitPrice": round(take_profit_price, 0),  # 利確価格
            "holdDays": 0,                                  # 保有日数
            "currentPrice": entry_price,
            "unrealizedPnl": 0,                            # 含み損益（円）
            "unrealizedPnlPct": 0,                         # 含み損益（%）
            "reason": reason,
            "profitFactor": profit_factor
        }

        positions.append(new_position)
        self.trade_log["positions"] = positions
        self._save_trade_log()

        logger.info(
            f"新規ポジション追加: {stock_code} ({company_name})\n"
            f"  エントリー価格: {entry_price:,.0f}円\n"
            f"  投資口数: {shares}株 / 投資額: {actual_invest:,.0f}円\n"
            f"  損切り: {stop_loss_price:,.0f}円 / 利確: {take_profit_price:,.0f}円\n"
            f"  理由: {reason}"
        )
        return True

    def update_positions(self, current_prices: dict) -> list:
        """
        現在価格を使って全ポジションの損益を更新し、イグジット条件を確認する。

        Args:
            current_prices (dict): 銘柄コードをキー、現在価格を値とする辞書
                                   例: {"7203": 2800.0, "6758": 1500.0}

        Returns:
            list: イグジットが実行されたポジションのリスト
        """
        positions = self.trade_log.get("positions", [])
        exited_positions = []

        for position in positions[:]:  # コピーでイテレート（削除対応）
            stock_code = position["stockCode"]

            # 現在価格を取得（ない場合はスキップ）
            current_price = current_prices.get(stock_code)
            if current_price is None:
                logger.debug(f"銘柄 {stock_code}: 現在価格なし。スキップ。")
                continue

            # 保有日数を更新
            position["holdDays"] += 1
            position["currentPrice"] = current_price

            # 含み損益を計算
            entry_price = position["entryPrice"]
            shares = position["shares"]
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            pnl_yen = (current_price - entry_price) * shares

            position["unrealizedPnl"] = round(pnl_yen, 0)
            position["unrealizedPnlPct"] = round(pnl_pct, 2)

            # ---- イグジット条件の確認 ----
            exit_triggered = False
            exit_price = None
            exit_reason = None

            # 損切り判定
            if current_price <= position["stopLossPrice"]:
                exit_price = current_price
                exit_reason = f"損切り(-5%)"
                exit_triggered = True

            # 利確判定
            elif current_price >= position["takeProfitPrice"]:
                exit_price = current_price
                exit_reason = f"利確(+15%)"
                exit_triggered = True

            # 時間切れ判定
            elif position["holdDays"] >= MAX_HOLD_DAYS:
                exit_price = current_price
                exit_reason = f"時間切れ({MAX_HOLD_DAYS}日)"
                exit_triggered = True

            # イグジット実行
            if exit_triggered and exit_price:
                self.close_position(stock_code, exit_price, exit_reason)
                exited_positions.append({
                    "stockCode": stock_code,
                    "exitPrice": exit_price,
                    "exitReason": exit_reason
                })

        self._save_trade_log()
        return exited_positions

    def close_position(
        self,
        stock_code: str,
        exit_price: float,
        exit_reason: str
    ) -> bool:
        """
        ポジションを決済して取引記録に保存する。

        Args:
            stock_code (str): 銘柄コード
            exit_price (float): 決済価格（円）
            exit_reason (str): 決済理由（"損切り"/"利確"/"手動"等）

        Returns:
            bool: 決済成功はTrue、銘柄が見つからない場合はFalse
        """
        positions = self.trade_log.get("positions", [])

        # 対象ポジションを検索
        target_position = None
        for p in positions:
            if p.get("stockCode") == stock_code:
                target_position = p
                break

        if not target_position:
            logger.warning(f"警告: {stock_code} のポジションが見つかりません。")
            return False

        # 損益計算
        entry_price = target_position["entryPrice"]
        shares = target_position["shares"]
        pnl_yen = (exit_price - entry_price) * shares
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        # 決済済みトレードに追加
        closed_trade = {
            **target_position,  # ポジション情報をコピー
            "exitDate": datetime.now().strftime("%Y-%m-%d"),
            "exitPrice": round(exit_price, 0),
            "exitReason": exit_reason,
            "realizedPnl": round(pnl_yen, 0),      # 実現損益（円）
            "returnPct": round(pnl_pct, 2),          # リターン（%）
            "isWin": pnl_yen > 0
        }

        self.trade_log["closed_trades"].append(closed_trade)

        # アクティブポジションから削除
        self.trade_log["positions"] = [p for p in positions if p.get("stockCode") != stock_code]

        # サマリー更新
        self.trade_log["summary"]["total_trades"] += 1
        self.trade_log["summary"]["total_pnl"] += pnl_yen
        if pnl_yen > 0:
            self.trade_log["summary"]["winning_trades"] += 1
        else:
            self.trade_log["summary"]["losing_trades"] += 1

        self._save_trade_log()

        pnl_sign = "+" if pnl_yen >= 0 else ""
        logger.info(
            f"ポジション決済: {stock_code}\n"
            f"  エントリー: {entry_price:,.0f}円 → イグジット: {exit_price:,.0f}円\n"
            f"  損益: {pnl_sign}{pnl_yen:,.0f}円 ({pnl_sign}{pnl_pct:.2f}%)\n"
            f"  理由: {exit_reason}"
        )
        return True

    def display_portfolio_status(self) -> None:
        """
        現在の保有状況・損益をコンソールに表示する。
        """
        positions = self.trade_log.get("positions", [])
        closed_trades = self.trade_log.get("closed_trades", [])
        summary = self.trade_log.get("summary", {})

        # 現金残高計算
        current_cash = _get_cash_balance(self.trade_log, self.initial_capital)
        invested_total = sum(p.get("investedAmount", 0) for p in positions)
        unrealized_pnl = sum(p.get("unrealizedPnl", 0) for p in positions)
        realized_pnl = summary.get("total_pnl", 0)
        total_value = current_cash + invested_total + unrealized_pnl

        print(f"\n{'='*60}")
        print(f"  ペーパートレード ポートフォリオ状況")
        print(f"  更新日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")

        # 資金状況
        print(f"\n【資金状況】")
        print(f"  初期資金        : {self.initial_capital:>12,.0f} 円")
        print(f"  現在評価額      : {total_value:>12,.0f} 円")
        pnl_total_sign = "+" if (realized_pnl + unrealized_pnl) >= 0 else ""
        print(f"  総損益          : {pnl_total_sign}{realized_pnl + unrealized_pnl:>11,.0f} 円")
        print(f"    ├ 実現損益    : {'+' if realized_pnl >= 0 else ''}{realized_pnl:>11,.0f} 円")
        print(f"    └ 含み損益    : {'+' if unrealized_pnl >= 0 else ''}{unrealized_pnl:>11,.0f} 円")
        print(f"  現金残高        : {current_cash:>12,.0f} 円")

        # 取引サマリー
        total_closed = summary.get("total_trades", 0)
        if total_closed > 0:
            win_rate = (summary.get("winning_trades", 0) / total_closed) * 100
            print(f"\n【取引サマリー】")
            print(f"  総取引数    : {total_closed}件")
            print(f"  勝ち/負け   : {summary.get('winning_trades', 0)}/{summary.get('losing_trades', 0)}")
            print(f"  勝率        : {win_rate:.1f}%")

        # 現在保有ポジション
        print(f"\n【現在保有ポジション】({len(positions)}/{self.max_positions}銘柄)")
        if not positions:
            print("  保有銘柄なし")
        else:
            print(f"  {'銘柄コード':>8} {'会社名':<18} {'取得価格':>8} {'現在価格':>8} {'損益':>10} {'日数':>4}")
            print(f"  {'-'*65}")
            for p in positions:
                company = p.get("companyName", "")[:16] or "N/A"
                pnl_pct = p.get("unrealizedPnlPct", 0)
                pnl_sign = "+" if pnl_pct >= 0 else ""
                print(
                    f"  {p.get('stockCode', 'N/A'):>8} "
                    f"{company:<18} "
                    f"{p.get('entryPrice', 0):>8,.0f} "
                    f"{p.get('currentPrice', 0):>8,.0f} "
                    f"{pnl_sign}{pnl_pct:>8.2f}% "
                    f"{p.get('holdDays', 0):>4}日"
                )

        # 直近5件の決済済みトレード
        if closed_trades:
            recent_trades = closed_trades[-5:][::-1]  # 直近5件を逆順で
            print(f"\n【直近決済トレード（最大5件）】")
            print(f"  {'銘柄コード':>8} {'決済日':>12} {'リターン':>9} {'理由':<15}")
            print(f"  {'-'*50}")
            for t in recent_trades:
                ret = t.get("returnPct", 0)
                ret_sign = "+" if ret >= 0 else ""
                print(
                    f"  {t.get('stockCode', 'N/A'):>8} "
                    f"{t.get('exitDate', 'N/A'):>12} "
                    f"{ret_sign}{ret:>7.2f}% "
                    f"{t.get('exitReason', 'N/A'):<15}"
                )

        print(f"\n{'='*60}\n")


# ========================================
# ユーティリティ関数（モジュール外からも呼び出し可能）
# ========================================

def _get_cash_balance(trade_log: dict, initial_capital: float) -> float:
    """
    現金残高を計算する（内部ユーティリティ）。

    Args:
        trade_log (dict): 取引ログ
        initial_capital (float): 初期資金

    Returns:
        float: 現金残高（円）
    """
    positions = trade_log.get("positions", [])
    invested = sum(p.get("investedAmount", 0) for p in positions)
    realized_pnl = trade_log.get("summary", {}).get("total_pnl", 0)
    return initial_capital + realized_pnl - invested


# ========================================
# 便利関数（main.pyから呼びやすいように）
# ========================================

def add_position(
    stock_code: str,
    entry_price: float,
    reason: str,
    company_name: str = "",
    profit_factor: float = None
) -> bool:
    """
    新規ポジションを追加する（グローバル関数版）。
    PaperTraderクラスを毎回インスタンス化しなくても使える。

    Args:
        stock_code (str): 銘柄コード
        entry_price (float): エントリー価格
        reason (str): エントリー理由
        company_name (str): 会社名
        profit_factor (float): バックテストのPF

    Returns:
        bool: 成功はTrue
    """
    trader = PaperTrader()
    return trader.add_position(stock_code, entry_price, reason, company_name, profit_factor)


def update_positions(current_prices: dict) -> list:
    """
    全ポジションの損益を更新する（グローバル関数版）。

    Args:
        current_prices (dict): {銘柄コード: 現在価格} の辞書

    Returns:
        list: イグジットされたポジションのリスト
    """
    trader = PaperTrader()
    return trader.update_positions(current_prices)


def close_position(
    stock_code: str,
    exit_price: float,
    exit_reason: str
) -> bool:
    """
    ポジションを手動決済する（グローバル関数版）。

    Args:
        stock_code (str): 銘柄コード
        exit_price (float): 決済価格
        exit_reason (str): 決済理由

    Returns:
        bool: 成功はTrue
    """
    trader = PaperTrader()
    return trader.close_position(stock_code, exit_price, exit_reason)


def display_portfolio_status() -> None:
    """
    ポートフォリオ状況を表示する（グローバル関数版）。
    """
    trader = PaperTrader()
    trader.display_portfolio_status()


# ========================================
# 実売買記録（ペーパーとは別・tradeType:"actual"）
# ========================================

def add_actual_trade(
    stock_code: str,
    entry_price: float,
    shares: int,
    company_name: str = "",
    signal: str = "",
) -> bool:
    """
    実際の売買をtrade_log.jsonに記録する。
    ペーパートレードとは tradeType:"actual" で区別される。

    Args:
        stock_code:   銘柄コード（例: "7203"）
        entry_price:  実際のエントリー価格（円）
        shares:       購入株数
        company_name: 会社名（任意）
        signal:       きっかけシグナル（例: "SHORT_TERM"）

    Returns:
        bool: 記録成功はTrue
    """
    trader = PaperTrader()
    positions = trader.trade_log.get("positions", [])

    # 同銘柄の実売買重複チェック
    for p in positions:
        if p.get("stockCode") == stock_code and p.get("tradeType") == "actual":
            logger.warning(f"既に実売買記録済み: {stock_code}")
            return False

    stop_loss = round(entry_price * 0.95)
    take_profit = round(entry_price * 1.15)
    invested = round(entry_price * shares)

    positions.append({
        "stockCode": stock_code,
        "companyName": company_name,
        "tradeType": "actual",
        "entryDate": datetime.now().strftime("%Y-%m-%d"),
        "entryPrice": round(entry_price),
        "shares": shares,
        "investedAmount": invested,
        "stopLossPrice": stop_loss,
        "takeProfitPrice": take_profit,
        "holdDays": 0,
        "currentPrice": round(entry_price),
        "unrealizedPnl": 0,
        "unrealizedPnlPct": 0.0,
        "signal": signal,
    })
    trader.trade_log["positions"] = positions
    trader._save_trade_log()
    logger.info(f"実売買記録: {stock_code} ¥{entry_price:,}×{shares}株 = ¥{invested:,}")
    return True


def close_actual_trade(stock_code: str, exit_price: float) -> bool:
    """
    実売買ポジションの決済を記録する。

    Args:
        stock_code:  銘柄コード
        exit_price:  実際の決済価格（円）

    Returns:
        bool: 成功はTrue
    """
    trader = PaperTrader()
    return trader.close_position(stock_code, exit_price, "手動決済（実売買）")


def get_actual_positions() -> list:
    """
    実売買ポジション一覧を返す。yfinanceで現在価格を取得して損益を更新する。

    Returns:
        list: 損益更新済みのポジションリスト（含み損益降順）
    """
    trader = PaperTrader()
    positions = [
        p for p in trader.trade_log.get("positions", [])
        if p.get("tradeType") == "actual"
    ]
    if not positions:
        return []

    # yfinanceで最新価格を取得して損益を更新
    try:
        from agents.noon_scanner import fetch_intraday
        for p in positions:
            intraday = fetch_intraday(p["stockCode"])
            if intraday and intraday.get("current_price", 0) > 0:
                current = intraday["current_price"]
                entry = p["entryPrice"]
                p["currentPrice"] = current
                p["unrealizedPnl"] = round((current - entry) * p["shares"])
                p["unrealizedPnlPct"] = round((current - entry) / entry * 100, 2)
    except Exception as e:
        logger.warning(f"実売買現在価格取得エラー（記録価格で表示）: {e}")

    return sorted(positions, key=lambda x: x.get("unrealizedPnlPct", 0), reverse=True)


# ========================================
# メイン（単体テスト用）
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のテスト処理
    使い方: python agents/paper_trader.py
    """
    print("=" * 55)
    print("Paper Trader - 動作テスト")
    print("=" * 55)

    trader = PaperTrader()

    print(f"\n初期資金: {trader.initial_capital:,.0f}円")
    print(f"1銘柄最大: {trader.max_position_size:,.0f}円")
    print(f"最大保有数: {trader.max_positions}銘柄")

    # テスト用ポジション追加
    print("\n[テスト] ポジション追加...")
    success = trader.add_position(
        stock_code="72030",
        entry_price=2800.0,
        reason="急騰シグナル（テスト）",
        company_name="トヨタ自動車",
        profit_factor=1.5
    )
    print(f"  ポジション追加: {'成功' if success else '失敗'}")

    # 価格更新テスト
    print("\n[テスト] 価格更新...")
    exited = trader.update_positions({"72030": 2900.0})
    print(f"  イグジット発生: {len(exited)}件")

    # 状況表示
    trader.display_portfolio_status()
    print("✓ ペーパートレーダー動作テスト完了")
