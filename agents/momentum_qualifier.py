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
10営業日後の実際の株価結果も自動記録（フェーズ3精度検証用）。
APIキー未設定時はステージ1のみ実行し、ステージ2はスキップ。

作者: Japan Momentum Agent
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

VOLUME_SUSTAIN_RATIO = 0.5
PRICE_SUSTAIN_RATIO = 0.97
SUSTAIN_CHECK_DAYS = 3
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
QUALIFY_LOG_PATH = Path(__file__).parent.parent / "memory" / "qualify_log.json"

# フェーズ3: 結果記録の基準（何営業日後に検証するか）
OUTCOME_CHECK_DAYS = 10


# ========================================
# ステージ1: 出来高継続パターン判定
# ========================================

def _check_volume_sustain(df_stock: pd.DataFrame, surge_date: str) -> dict:
    """
    急騰後の出来高・株価継続性を判定する。
    """
    surge_dt = pd.to_datetime(surge_date)
    df_stock = df_stock.sort_values("Date").reset_index(drop=True)

    surge_mask = df_stock["Date"] == surge_dt
    if not surge_mask.any():
        return {"stage1Pass": False, "reason": "急騰日のデータなし"}

    surge_idx = df_stock[surge_mask].index[-1]
    surge_row = df_stock.loc[surge_idx]
    surge_volume = float(surge_row.get("Volume", 0) or 0)
    surge_close = float(surge_row.get("Close", 0) or 0)

    if surge_volume <= 0 or surge_close <= 0:
        return {"stage1Pass": False, "reason": "急騰日データ不正"}

    post_data = df_stock.loc[surge_idx + 1: surge_idx + SUSTAIN_CHECK_DAYS]

    if len(post_data) == 0:
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

    post_volumes = post_data["Volume"].astype(float)
    avg_post_volume = float(post_volumes.mean())
    volume_sustain_rate = avg_post_volume / surge_volume if surge_volume > 0 else 0
    volume_sustained = volume_sustain_rate >= VOLUME_SUSTAIN_RATIO

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
    """
    from agents.utils import get_anthropic_key
    api_key = get_anthropic_key()
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

        result_text = ""
        for _ in range(5):
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        result_text += block.text
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": ""
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        import re
        result_text = result_text.strip()
        result_text = re.sub(r'<cite[^>]*>', '', result_text)
        result_text = result_text.replace('</cite>', '')
        result_text = re.sub(r'```(?:json)?', '', result_text)

        depth = 0
        best_s = best_e = cur_s = 0
        in_str = esc = False
        for k, ch in enumerate(result_text):
            if esc:
                esc = False; continue
            if ch == '\\' and in_str:
                esc = True; continue
            if ch == '"':
                in_str = not in_str; continue
            if in_str:
                continue
            if ch == '{':
                if depth == 0:
                    cur_s = k
                depth += 1
            elif ch == '}' and depth > 0:
                depth -= 1
                if depth == 0 and k + 1 - cur_s > best_e - best_s:
                    best_s, best_e = cur_s, k + 1
        if best_e > best_s:
            result_text = result_text[best_s:best_e]

        parsed = json.loads(result_text)

        batch_results = {}
        for item in parsed.get("results", []):
            code = str(item.get("stockCode", ""))
            batch_results[code] = {
                "structuralChange": item.get("structuralChange"),
                "confidence": item.get("confidence"),
                "comment": item.get("comment", ""),
                "stage2Available": True,
            }

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
            code: {"structuralChange": None, "confidence": "low",
                   "comment": "レスポンス解析エラー", "stage2Available": True}
            for code in stock_codes
        }
    except Exception as e:
        logger.warning(f"バッチ判定: Claude API呼び出しエラー: {e}")
        return {
            code: {"structuralChange": None, "confidence": None,
                   "comment": f"APIエラー: {str(e)[:30]}", "stage2Available": False}
            for code in stock_codes
        }


# ========================================
# フェーズ3: 10営業日後の結果記録
# ========================================

def record_outcomes(df_all: pd.DataFrame) -> int:
    """
    qualify_log.json の中で「10営業日以上経過・outcome未記録」のエントリに
    実際の株価結果を記録する。（フェーズ3 判定精度検証用）

    処理の流れ:
      1. qualify_log.json を読み込む
      2. outcome が null のエントリを抽出
      3. 判定日から10営業日以上経過しているか確認
      4. 急騰日終値 → 10営業日後終値のリターンを計算して記録

    Args:
        df_all: 全銘柄の株価DataFrame（load_latest_quotes()の戻り値）

    Returns:
        int: 今回新たに記録したエントリ数
    """
    if not QUALIFY_LOG_PATH.exists():
        return 0

    try:
        with open(QUALIFY_LOG_PATH, "r", encoding="utf-8") as f:
            log_entries = json.load(f)
    except Exception as e:
        logger.warning(f"qualify_log.json 読み込みエラー: {e}")
        return 0

    if not log_entries:
        return 0

    code_col = "Code" if "Code" in df_all.columns else "code"
    today = pd.Timestamp.now().normalize()
    updated_count = 0

    for entry in log_entries:
        # outcome が既に記録済みならスキップ
        if entry.get("outcome") is not None:
            continue

        qualified_at_str = entry.get("qualifiedAt", "")
        if not qualified_at_str:
            continue

        try:
            qualified_at = pd.to_datetime(qualified_at_str)
        except Exception:
            continue

        # 判定日から何営業日経過したか確認
        bdays_elapsed = len(pd.bdate_range(start=qualified_at, end=today)) - 1
        if bdays_elapsed < OUTCOME_CHECK_DAYS:
            continue  # まだ10営業日経っていない

        # 銘柄の株価データを取得
        stock_code = entry.get("stockCode", "")
        surge_date_str = entry.get("scanDate", "")
        if not stock_code or not surge_date_str:
            continue

        try:
            df_stock = df_all[df_all[code_col].astype(str) == stock_code].copy()
            df_stock = df_stock.sort_values("Date").reset_index(drop=True)

            surge_dt = pd.to_datetime(surge_date_str)

            # 急騰日の終値を取得
            surge_row = df_stock[df_stock["Date"] == surge_dt]
            if surge_row.empty:
                entry["outcome"] = {"status": "no_data", "reason": "急騰日データなし"}
                updated_count += 1
                continue

            entry_price = float(surge_row.iloc[0]["Close"])

            # 急騰日以降のデータを取得し、10営業日後の終値を取得
            future_df = df_stock[df_stock["Date"] > surge_dt].reset_index(drop=True)
            if len(future_df) < OUTCOME_CHECK_DAYS:
                # データがまだ揃っていない（市場休場等）→ スキップ
                continue

            exit_row = future_df.iloc[OUTCOME_CHECK_DAYS - 1]
            exit_price = float(exit_row["Close"])
            exit_date = exit_row["Date"].strftime("%Y-%m-%d")
            return_pct = round((exit_price - entry_price) / entry_price * 100, 2)

            entry["outcome"] = {
                "status": "recorded",
                "entryPrice": entry_price,
                "exitPrice": exit_price,
                "exitDate": exit_date,
                "returnPct": return_pct,
                "isWin": return_pct > 0,
                "recordedAt": datetime.now().isoformat(),
            }
            updated_count += 1
            logger.info(
                f"結果記録: {stock_code} qualifyResult={entry.get('qualifyResult')} "
                f"→ {return_pct:+.1f}% ({exit_date})"
            )

        except Exception as e:
            logger.warning(f"{stock_code} 結果記録エラー: {e}")
            entry["outcome"] = {"status": "error", "reason": str(e)[:50]}
            updated_count += 1

    # 更新があれば保存
    if updated_count > 0:
        with open(QUALIFY_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log_entries, f, ensure_ascii=False, indent=2)
        logger.info(f"outcome記録完了: {updated_count}件更新 → {QUALIFY_LOG_PATH}")

    return updated_count


def get_outcome_stats() -> dict:
    """
    qualify_log.json からSTRONG/WEAK/NOISEごとの勝率・平均リターンを集計する。
    フェーズ3の判定精度検証に使用。

    Returns:
        dict: {
            "STRONG": {"count": int, "win_rate": float, "avg_return": float},
            "WEAK":   {...},
            "WATCH":  {...},
            "NOISE":  {...},
            "total_recorded": int,
        }
    """
    if not QUALIFY_LOG_PATH.exists():
        return {}

    try:
        with open(QUALIFY_LOG_PATH, "r", encoding="utf-8") as f:
            log_entries = json.load(f)
    except Exception:
        return {}

    stats = {
        label: {"count": 0, "wins": 0, "returns": []}
        for label in ["STRONG", "WATCH", "WEAK", "NOISE"]
    }
    total_recorded = 0

    for entry in log_entries:
        outcome = entry.get("outcome")
        if not outcome or outcome.get("status") != "recorded":
            continue

        label = entry.get("qualifyResult", "NOISE")
        if label not in stats:
            continue

        total_recorded += 1
        ret = outcome.get("returnPct", 0)
        stats[label]["count"] += 1
        stats[label]["returns"].append(ret)
        if outcome.get("isWin", False):
            stats[label]["wins"] += 1

    result = {"total_recorded": total_recorded}
    for label, s in stats.items():
        if s["count"] > 0:
            result[label] = {
                "count": s["count"],
                "win_rate": round(s["wins"] / s["count"] * 100, 1),
                "avg_return": round(sum(s["returns"]) / len(s["returns"]), 2),
            }
        else:
            result[label] = {"count": 0, "win_rate": None, "avg_return": None}

    return result


# ========================================
# メイン判定関数
# ========================================

def qualify_signals(signals: list, df_all: pd.DataFrame) -> list:
    """
    急騰シグナルリストに対して2段階モメンタム判定を実行する。
    実行のたびに過去エントリの10営業日後結果も自動記録する。
    """
    if not signals:
        return []

    logger.info(f"モメンタム判定開始: {len(signals)}銘柄")

    # フェーズ3: 過去エントリの結果記録（毎回実行）
    try:
        recorded = record_outcomes(df_all)
        if recorded > 0:
            logger.info(f"過去エントリの結果記録: {recorded}件更新")
    except Exception as e:
        logger.warning(f"結果記録エラー（スキップ）: {e}")

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
            result["qualifyResult"] = "STRONG"
        elif stage1_pass and structural_change is None:
            result["qualifyResult"] = "WATCH"
        elif stage1_pass and structural_change is False:
            result["qualifyResult"] = "WEAK"
        else:
            result["qualifyResult"] = "NOISE"

        # フェーズ3: outcome フィールドを null で初期化（後で record_outcomes が埋める）
        result["outcome"] = None

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

    for r in results:
        r["qualifiedAt"] = datetime.now().isoformat()

    existing.extend(results)
    existing = existing[-500:]

    with open(QUALIFY_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    logger.info(f"判定ログを保存しました: {QUALIFY_LOG_PATH}（累計{len(existing)}件）")


def format_qualify_result_for_slack(results: list) -> str:
    """
    判定結果をSlack通知用のテキストにフォーマットする。
    outcome記録があれば精度サマリーも追加する。
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

    # フェーズ3: 判定精度サマリーを追記（記録件数が5件以上あれば表示）
    try:
        stats = get_outcome_stats()
        total = stats.get("total_recorded", 0)
        if total >= 5:
            lines.append(f"\n📊 *判定精度サマリー（{total}件記録済み）*")
            for label in ["STRONG", "WEAK", "NOISE"]:
                s = stats.get(label, {})
                if s.get("count", 0) > 0:
                    lines.append(
                        f"  {label}: 勝率{s['win_rate']}% / 平均{s['avg_return']:+.1f}%"
                        f"（{s['count']}件）"
                    )
    except Exception:
        pass

    return "\n".join(lines)
