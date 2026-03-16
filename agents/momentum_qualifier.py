"""
agents/momentum_qualifier.py
モメンタム判定モジュール

急騰シグナルが出た銘柄に対して「本物のモメンタムか？」を2段階で判定する。

ステージ1: 出来高継続パターン判定（数値）
  - 急騰後の出来高が高水準を維持しているか
  - 株価が急騰後に急落していないか

ステージ2: Claude APIによる構造的変化判定
  - EDINETの直近開示書類を確認
  - web_searchで最新ニュースを検索
  - 「構造的変化あり/なし」＋理由コメントを出力

結果は memory/qualify_log.json に記録される。
APIキー未設定時はステージ1のみ実行し、ステージ2はスキップ。

作者: Japan Momentum Agent
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

# ステージ1: 急騰後の出来高維持率の閾値（急騰日の何%以上を維持するか）
VOLUME_SUSTAIN_RATIO = 0.5   # 50%以上を維持していれば継続とみなす

# ステージ1: 急騰後の株価維持率の閾値（急騰後に何%以上の株価を維持するか）
PRICE_SUSTAIN_RATIO = 0.97   # 急騰終値の97%以上を維持（-3%以内の下落は許容）

# ステージ1: 急騰後の継続確認日数
SUSTAIN_CHECK_DAYS = 3       # 急騰後3日間のデータで判定

# ステージ2: Claude APIモデル
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# 結果保存パス
QUALIFY_LOG_PATH = Path(__file__).parent.parent / "memory" / "qualify_log.json"


# ========================================
# ステージ1: 出来高継続パターン判定
# ========================================

def _check_volume_sustain(df_stock: pd.DataFrame, surge_date: str) -> dict:
    """
    急騰後の出来高・株価継続性を判定する。

    Args:
        df_stock: 1銘柄の株価データ（Date, Close, Volume列を含む）
        surge_date: 急騰日（YYYY-MM-DD）

    Returns:
        dict: {
            "volumeSustained": bool,  # 出来高が継続しているか
            "priceSustained": bool,   # 株価が維持されているか
            "stage1Pass": bool,       # ステージ1通過フラグ
            "surgeVolume": float,     # 急騰日の出来高
            "avgPostVolume": float,   # 急騰後平均出来高
            "volumeSustainRate": float, # 出来高維持率
            "surgeClose": float,      # 急騰日終値
            "latestClose": float,     # 最新終値
            "priceChangeAfterSurge": float, # 急騰後の株価変化率
            "daysChecked": int,       # 確認できた日数
        }
    """
    surge_dt = pd.to_datetime(surge_date)
    df_stock = df_stock.sort_values("Date").reset_index(drop=True)

    # 急騰日のデータを取得
    surge_mask = df_stock["Date"] == surge_dt
    if not surge_mask.any():
        return {"stage1Pass": False, "reason": "急騰日のデータなし"}

    surge_idx = df_stock[surge_mask].index[-1]
    surge_row = df_stock.loc[surge_idx]
    surge_volume = float(surge_row.get("Volume", 0) or 0)
    surge_close = float(surge_row.get("Close", 0) or 0)

    if surge_volume <= 0 or surge_close <= 0:
        return {"stage1Pass": False, "reason": "急騰日データ不正"}

    # 急騰後のデータを取得（最大SUSTAIN_CHECK_DAYS日分）
    post_data = df_stock.loc[surge_idx + 1: surge_idx + SUSTAIN_CHECK_DAYS]

    if len(post_data) == 0:
        # 急騰後のデータがない（最新日の急騰）→ 判定保留・通過扱い
        return {
            "volumeSustained": True,
            "priceSustained": True,
            "stage1Pass": True,
            "surgeVolume": surge_volume,
            "avgPostVolume": 0.0,
            "volumeSustainRate": 0.0,
            "surgeClose": surge_close,
            "latestClose": surge_close,
            "priceChangeAfterSurge": 0.0,
            "daysChecked": 0,
            "reason": "急騰後データなし（最新日）→ 保留通過"
        }

    # 出来高継続チェック
    post_volumes = post_data["Volume"].astype(float)
    avg_post_volume = float(post_volumes.mean())
    volume_sustain_rate = avg_post_volume / surge_volume if surge_volume > 0 else 0
    volume_sustained = volume_sustain_rate >= VOLUME_SUSTAIN_RATIO

    # 株価維持チェック
    latest_close = float(post_data["Close"].iloc[-1])
    price_change_after_surge = (latest_close - surge_close) / surge_close
    price_sustained = latest_close >= surge_close * PRICE_SUSTAIN_RATIO

    stage1_pass = volume_sustained and price_sustained

    return {
        "volumeSustained": volume_sustained,
        "priceSustained": price_sustained,
        "stage1Pass": stage1_pass,
        "surgeVolume": surge_volume,
        "avgPostVolume": round(avg_post_volume, 0),
        "volumeSustainRate": round(volume_sustain_rate, 2),
        "surgeClose": surge_close,
        "latestClose": latest_close,
        "priceChangeAfterSurge": round(price_change_after_surge * 100, 2),
        "daysChecked": len(post_data),
        "reason": (
            "✅ 出来高・株価ともに継続" if stage1_pass
            else f"❌ {'出来高が低下' if not volume_sustained else '株価が急落'}"
        )
    }


# ========================================
# ステージ2: Claude APIによる構造的変化判定（一括処理）
# ========================================

def _analyze_structural_change_batch(stocks: list) -> dict:
    """
    複数銘柄をまとめて1回のClaude API呼び出しで構造的変化を判定する。
    銘柄ごとに個別呼び出しする代わりにバッチ化してトークン消費を削減。

    Args:
        stocks: [{"stockCode": str, "companyName": str}] のリスト

    Returns:
        dict: {stockCode: {"structuralChange": bool, "confidence": str,
                           "comment": str, "stage2Available": bool}}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    stock_codes = [s["stockCode"] for s in stocks]

    if not api_key:
        return {
            code: {
                "structuralChange": None,
                "confidence": None,
                "comment": "APIキー未設定のためスキップ",
                "stage2Available": False,
            }
            for code in stock_codes
        }

    stocks_text = "\n".join([
        f"- 銘柄コード: {s['stockCode']} / 会社名: {s['companyName']}"
        for s in stocks
    ])

    prompt = f"""あなたは日本株の投資アナリストです。
以下の{len(stocks)}銘柄が急騰しました。各銘柄について、短期的な加熱なのか、中長期で継続する構造的変化を伴った急騰なのかを判断してください。

【対象銘柄一覧】
{stocks_text}

以下の観点でweb検索して調べてください：
1. 直近のニュース・決算・開示情報
2. ビジネスモデルの変化・新市場参入・規制変化などの構造的要因
3. 一時的なイベント（仕手、空売り踏み上げ、単発ニュース）の可能性

必ず以下のJSON形式のみで回答してください（他のテキスト不要）：
{{
  "results": [
    {{
      "stockCode": "<銘柄コード>",
      "structuralChange": true or false,
      "confidence": "high" or "medium" or "low",
      "comment": "50文字以内の理由"
    }}
  ]
}}

resultsには対象の全{len(stocks)}銘柄を含めてください。"""

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content": prompt}]

        # web_search使用時は複数ターンになるためループで処理
        result_text = ""
        for _ in range(5):  # 最大5ターン（無限ループ防止）
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages
            )

            # アシスタントの返答を会話履歴に追加
            messages.append({"role": "assistant", "content": response.content})

            # stop_reason が end_turn になったら完了
            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        result_text += block.text
                break

            # tool_use の場合はツール結果を追加して継続
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": ""  # web_searchは自動実行されるため空でOK
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        # JSONパース（<cite>タグ・コードブロック除去後に最外周JSONを抽出）
        import re
        result_text = result_text.strip()
        result_text = re.sub(r'<cite[^>]*>', '', result_text)
        result_text = result_text.replace('</cite>', '')
        result_text = re.sub(r'```(?:json)?', '', result_text)
        start = result_text.find("{")
        end = result_text.rfind("}") + 1
        if start >= 0 and end > start:
            result_text = result_text[start:end]

        parsed = json.loads(result_text)

        # resultsリストを {stockCode: result} の辞書に変換
        batch_results = {}
        for item in parsed.get("results", []):
            code = str(item.get("stockCode", ""))
            batch_results[code] = {
                "structuralChange": item.get("structuralChange"),
                "confidence": item.get("confidence"),
                "comment": item.get("comment", ""),
                "stage2Available": True,
            }

        # Claudeが返さなかった銘柄にフォールバック値を設定
        for code in stock_codes:
            if code not in batch_results:
                logger.warning(f"バッチ判定: {code} の結果がレスポンスに含まれていません")
                batch_results[code] = {
                    "structuralChange": None,
                    "confidence": "low",
                    "comment": "判定結果なし",
                    "stage2Available": True,
                }

        return batch_results

    except json.JSONDecodeError:
        logger.warning("バッチ判定: JSONパースエラー")
        return {
            code: {
                "structuralChange": None,
                "confidence": "low",
                "comment": "レスポンス解析エラー",
                "stage2Available": True,
            }
            for code in stock_codes
        }
    except Exception as e:
        logger.warning(f"バッチ判定: Claude API呼び出しエラー: {e}")
        return {
            code: {
                "structuralChange": None,
                "confidence": None,
                "comment": f"APIエラー: {str(e)[:30]}",
                "stage2Available": False,
            }
            for code in stock_codes
        }


# ========================================
# メイン判定関数
# ========================================

def qualify_signals(signals: list, df_all: pd.DataFrame) -> list:
    """
    急騰シグナルリストに対して2段階モメンタム判定を実行する。

    Args:
        signals: SHORT_TERMスキャン結果のリスト
        df_all: 全銘柄の株価DataFrame

    Returns:
        list: 判定結果付きのシグナルリスト
    """
    if not signals:
        return []

    logger.info(f"モメンタム判定開始: {len(signals)}銘柄")

    results = []
    code_col = "Code" if "Code" in df_all.columns else "code"

    # ---- ステージ1: 全銘柄の出来高継続チェック ----
    stage1_map = {}
    for signal in signals:
        stock_code = signal.get("stockCode", "")
        surge_date = signal.get("scanDate", "")
        try:
            df_stock = df_all[df_all[code_col].astype(str) == stock_code].copy()
            stage1 = _check_volume_sustain(df_stock, surge_date)
        except Exception as e:
            logger.debug(f"{stock_code} ステージ1エラー: {e}")
            stage1 = {"stage1Pass": False, "reason": f"エラー: {e}"}
        stage1_map[stock_code] = stage1

    # ---- ステージ2: ステージ1通過銘柄を一括でClaude判定（API呼び出し1回） ----
    stage1_pass_signals = [
        s for s in signals
        if stage1_map.get(s.get("stockCode", ""), {}).get("stage1Pass", False)
    ]

    stage2_map = {}
    if stage1_pass_signals:
        stocks_for_batch = [
            {"stockCode": s.get("stockCode", ""), "companyName": s.get("companyName", "")}
            for s in stage1_pass_signals
        ]
        logger.info(f"  ステージ1通過: {len(stocks_for_batch)}銘柄 → Claude一括判定中...")
        stage2_map = _analyze_structural_change_batch(stocks_for_batch)

    # ---- 結果まとめ ----
    for signal in signals:
        stock_code = signal.get("stockCode", "")
        company_name = signal.get("companyName", "")
        result = {**signal}

        stage1 = stage1_map.get(stock_code, {"stage1Pass": False, "reason": "不明"})
        result["stage1"] = stage1

        if stage1.get("stage1Pass", False):
            stage2 = stage2_map.get(stock_code, {
                "structuralChange": None,
                "confidence": "low",
                "comment": "判定結果なし",
                "stage2Available": False,
            })
        else:
            logger.info(f"  {stock_code} {company_name}: ステージ1不通過（{stage1.get('reason', '')}）")
            stage2 = {
                "structuralChange": False,
                "confidence": None,
                "comment": "ステージ1不通過のためスキップ",
                "stage2Available": False,
            }

        result["stage2"] = stage2

        # ---- 総合判定 ----
        stage1_pass = stage1.get("stage1Pass", False)
        structural_change = stage2.get("structuralChange")

        if stage1_pass and structural_change is True:
            result["qualifyResult"] = "STRONG"   # 強シグナル（両方通過）
        elif stage1_pass and structural_change is None:
            result["qualifyResult"] = "WATCH"    # 要観察（ステージ2判定不能）
        elif stage1_pass and structural_change is False:
            result["qualifyResult"] = "WEAK"     # 弱シグナル（短期加熱の可能性）
        else:
            result["qualifyResult"] = "NOISE"    # ノイズ（ステージ1不通過）

        results.append(result)

    # 結果をログに保存
    _save_qualify_log(results)

    strong = sum(1 for r in results if r["qualifyResult"] == "STRONG")
    watch = sum(1 for r in results if r["qualifyResult"] == "WATCH")
    logger.info(f"モメンタム判定完了: STRONG={strong}件 / WATCH={watch}件 / 計{len(results)}件")

    return results


# ========================================
# ログ保存・Slack通知フォーマット
# ========================================

def _save_qualify_log(results: list):
    """判定結果をJSONログに追記保存する。"""
    QUALIFY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if QUALIFY_LOG_PATH.exists():
        try:
            with open(QUALIFY_LOG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # 今回の結果に判定日時を付加して追記
    for r in results:
        r["qualifiedAt"] = datetime.now().isoformat()

    existing.extend(results)

    # 最新500件のみ保持
    existing = existing[-500:]

    with open(QUALIFY_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    logger.info(f"判定ログを保存しました: {QUALIFY_LOG_PATH}（累計{len(existing)}件）")


def format_qualify_result_for_slack(results: list) -> str:
    """
    判定結果をSlack通知用のテキストにフォーマットする。

    Args:
        results: qualify_signals()の戻り値

    Returns:
        str: Slack通知用テキスト
    """
    strong_results = [r for r in results if r["qualifyResult"] == "STRONG"]
    watch_results = [r for r in results if r["qualifyResult"] == "WATCH"]

    if not strong_results and not watch_results:
        return "🔍 構造的モメンタム候補なし（全シグナルがノイズ/短期加熱と判定）"

    lines = ["🔥 *構造的モメンタム判定結果*\n"]

    if strong_results:
        lines.append("*【STRONG】構造的変化あり*")
        for r in strong_results:
            s1 = r.get("stage1", {})
            s2 = r.get("stage2", {})
            vol_rate = s1.get("volumeSustainRate", 0)
            price_chg = s1.get("priceChangeAfterSurge", 0)
            comment = s2.get("comment", "")
            confidence = s2.get("confidence", "")

            vol_emoji = "📊" if s1.get("volumeSustained") else "📉"
            price_emoji = "✅" if s1.get("priceSustained") else "❌"

            lines.append(
                f"• *{r.get('stockCode')} {r.get('companyName', '')}*\n"
                f"  {vol_emoji} 出来高維持率:{vol_rate:.0%}  "
                f"{price_emoji} 急騰後株価:{price_chg:+.1f}%\n"
                f"  🤖 Claude({confidence}): {comment}"
            )

    if watch_results:
        lines.append("\n*【WATCH】要観察（APIキー未設定）*")
        for r in watch_results:
            s1 = r.get("stage1", {})
            vol_rate = s1.get("volumeSustainRate", 0)
            price_chg = s1.get("priceChangeAfterSurge", 0)
            lines.append(
                f"• {r.get('stockCode')} {r.get('companyName', '')} "
                f"出来高維持:{vol_rate:.0%} 株価:{price_chg:+.1f}%"
            )

    return "\n".join(lines)
