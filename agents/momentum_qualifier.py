"""
agents/momentum_qualifier.py
モメンタム判定モジュール

急騰シグナルが出た銘柄に対して「本物のモメンタムか？」を2段階で判定する。

ステージ1: 出来高継続パターン判定（数値）
  - 急騰後の出来高が高水準を維持しているか
  - 株価が急騰後に急落していないか

ステージ2: Claude APIによる構造的変化判定
  - 銘柄コード・会社名をもとに学習済み知識で判断（web_searchなし・コスト削減）
  - 「構造的変化あり/なし」＋理由コメントを出力

結果は memory/qualify_log.json に記録される。
10営業日後の実際の株価結果も自動記録（フェーズ3精度検証用）。
APIキー未設定時はステージ1のみ実行し、ステージ2はスキップ。

作者: Japan Momentum Agent
"""

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

VOLUME_SUSTAIN_RATIO = 0.5
PRICE_SUSTAIN_RATIO = 0.97
SUSTAIN_CHECK_DAYS = 3
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
QUALIFY_LOG_PATH = Path(__file__).parent.parent / "memory" / "qualify_log.json"
MOMENTUM_COMMENT_CACHE_PATH = Path(__file__).parent.parent / "data" / "processed" / "momentum_comments_cache.json"

# フェーズ3: 結果記録の基準（何営業日後に検証するか）
OUTCOME_CHECK_DAYS = 10


# ========================================
# 急騰理由取得: Google News RSS + Claude API
# ========================================

def _fetch_stock_news(company_name: str, stock_code: str, max_items: int = 4) -> list:
    """
    Google News RSSから銘柄関連ニュースを取得する。
    """
    query = urllib.parse.quote(f"{company_name} 株")
    url = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        resp = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return []
        titles = re.findall(
            r'<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>',
            resp.text
        )
        news = []
        for t in titles[1:max_items + 1]:  # 先頭はフィードタイトルのためスキップ
            title = (t[0] or t[1]).strip()
            if title and len(title) > 8:
                news.append(title)
        return news
    except Exception as e:
        logger.debug(f"{stock_code} ニュース取得エラー: {e}")
        return []


def _generate_surge_reasons_batch(stocks_with_info: list) -> dict:
    """
    銘柄の TDnet 開示 + Google News ニュースから Claude API で急騰理由を一括生成する。
    情報源の優先度: TDnet適時開示 > Google News > 推測（推測）表記付き

    Returns:
        dict: {stockCode: surgeReason}
    """
    from agents.utils import get_anthropic_key
    api_key = get_anthropic_key()
    if not api_key or not stocks_with_info:
        return {}

    stocks_text = ""
    for s in stocks_with_info:
        tdnet_list = s.get("tdnet_disclosures", [])
        news_list = s.get("news", [])

        info_lines = []
        if tdnet_list:
            info_lines.append("  【TDnet適時開示（最重要・優先使用）】")
            for d in tdnet_list[:3]:
                info_lines.append(f"  ・{d['time']} {d['title']}")
        if news_list:
            info_lines.append("  【Google Newsヘッドライン】")
            for n in news_list[:2]:
                info_lines.append(f"  ・{n}")
        if not info_lines:
            info_lines.append("  （情報取得なし）")

        stocks_text += (
            f"【{s['stockCode']} {s['companyName']}】"
            f" 前日比+{s.get('price_change_pct', 0):.1f}% / 出来高{s.get('volume_ratio', 0):.1f}倍\n"
            + "\n".join(info_lines) + "\n\n"
        )

    prompt = f"""以下の日本株が急騰しました。各銘柄の情報をもとに急騰の主因を50文字以内で答えてください。

【重要な指示】
- TDnet適時開示がある場合は必ずそれを主因として使用してください（最も信頼性の高い情報源）
- Google Newsヘッドラインのみの場合はそこから判断してください
- 情報取得なしの場合は会社名・事業内容から推測し、末尾に「（推測）」と付けてください
- 冒頭に「[TDnet]」「[ニュース]」「[推測]」のいずれかのタグを付けてください

{stocks_text}
必ず以下のJSON形式のみで回答してください：
{{
  "results": [
    {{
      "stockCode": "<銘柄コード>",
      "surgeReason": "<[タグ] 急騰の主因を50文字以内で>"
    }}
  ]
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )

        result_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                result_text += block.text
        result_text = result_text.strip()
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
        return {
            str(item["stockCode"]): item.get("surgeReason", "")
            for item in parsed.get("results", [])
        }

    except Exception as e:
        logger.warning(f"急騰理由生成エラー: {e}")
        return {}


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
    web_searchなし・学習済み知識のみで判断（コスト削減）。
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

判断の観点：
1. 会社名・事業内容から考えられる構造的変化の可能性（AI・EV・防衛・半導体など成長テーマとの関連）
2. 一時的なイベントになりやすい業種かどうか（仕手株・小型株・単発材料）
3. 事業の安定性・継続性

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

        # web_searchなし・1回呼び出し（コスト削減）
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )

        import re
        result_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                result_text += block.text
        result_text = result_text.strip()
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
        if entry.get("outcome") is not None:
            continue

        qualified_at_str = entry.get("qualifiedAt", "")
        if not qualified_at_str:
            continue

        try:
            qualified_at = pd.to_datetime(qualified_at_str)
        except Exception:
            continue

        bdays_elapsed = len(pd.bdate_range(start=qualified_at, end=today)) - 1
        if bdays_elapsed < OUTCOME_CHECK_DAYS:
            continue

        stock_code = entry.get("stockCode", "")
        surge_date_str = entry.get("scanDate", "")
        if not stock_code or not surge_date_str:
            continue

        try:
            df_stock = df_all[df_all[code_col].astype(str) == stock_code].copy()
            df_stock = df_stock.sort_values("Date").reset_index(drop=True)

            surge_dt = pd.to_datetime(surge_date_str)

            surge_row = df_stock[df_stock["Date"] == surge_dt]
            if surge_row.empty:
                entry["outcome"] = {"status": "no_data", "reason": "急騰日データなし"}
                updated_count += 1
                continue

            entry_price = float(surge_row.iloc[0]["Close"])

            future_df = df_stock[df_stock["Date"] > surge_dt].reset_index(drop=True)
            if len(future_df) < OUTCOME_CHECK_DAYS:
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

    if updated_count > 0:
        with open(QUALIFY_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log_entries, f, ensure_ascii=False, indent=2)
        logger.info(f"outcome記録完了: {updated_count}件更新 → {QUALIFY_LOG_PATH}")

    return updated_count


def get_outcome_stats() -> dict:
    """
    qualify_log.json からSTRONG/WEAK/NOISEごとの勝率・平均リターンを集計する。
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
# モメンタムシグナル: コメント生成（永続キャッシュ付き）
# ========================================

def generate_and_cache_momentum_comments(signals: list) -> dict:
    """
    MOメンタムシグナル銘柄に「なぜ今トレンドか」コメントをClaude APIで生成し、
    永続キャッシュに保存する。キャッシュ済みの銘柄はAPIを呼ばずに再利用。

    キャッシュ: data/processed/momentum_comments_cache.json
    キー: stockCode（有効期限なし。中長期トレンドのコメントは使い回し可）

    Returns:
        dict: {stockCode: comment}
    """
    from agents.utils import get_anthropic_key
    api_key = get_anthropic_key()
    if not api_key or not signals:
        return {}

    # キャッシュ読み込み
    cache = {}
    if MOMENTUM_COMMENT_CACHE_PATH.exists():
        try:
            with open(MOMENTUM_COMMENT_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    # 30日以上経過したコメントは期限切れ扱い（月1点検・再生成）
    today_str = datetime.now().strftime("%Y-%m-%d")
    def _is_expired(entry) -> bool:
        if isinstance(entry, str):   # 旧フォーマット互換（文字列のまま）→ 期限切れ扱い
            return True
        generated_at = entry.get("generatedAt", "")
        if not generated_at:
            return True
        try:
            delta = (datetime.now() - datetime.strptime(generated_at, "%Y-%m-%d")).days
            return delta >= 30
        except Exception:
            return True

    # キャッシュにない or 期限切れの銘柄だけ抽出
    new_signals = [
        s for s in signals
        if s.get("stockCode", "") not in cache or _is_expired(cache[s.get("stockCode", "")])
    ]

    if new_signals:
        stocks_text = ""
        for s in new_signals:
            code = s.get("stockCode", "")
            name = s.get("companyName", "")
            rsi = s.get("rsi14", 0)
            high_ratio = s.get("priceToHighRatio", 0)
            new_high = s.get("newHighCount", 0)
            vol_trend = s.get("volumeTrend", 1.0)
            stocks_text += (
                f"【{code} {name}】"
                f"RSI:{rsi:.0f} / 52週高値比:{high_ratio:.1f}% / 新高値:{new_high}回/20日 / 出来高トレンド:{vol_trend:.2f}x\n"
            )

        prompt = f"""あなたはモメンタム投資の専門家です。以下の銘柄のトレンド指標を見て、
各銘柄に「なぜ今モメンタムがあるか」を40文字以内で簡潔にコメントしてください。
学習済み知識のみで判断してください。

{stocks_text}
必ず以下のJSON形式のみで回答してください：
{{
  "comments": [
    {{
      "stockCode": "<銘柄コード>",
      "comment": "<40文字以内>"
    }}
  ]
}}"""

        try:
            import anthropic
            import re as _re
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.content[0].text.strip()
            raw = _re.sub(r'```(?:json)?', '', raw)
            s_idx = raw.find("{"); e_idx = raw.rfind("}") + 1
            if s_idx >= 0 and e_idx > s_idx:
                raw = raw[s_idx:e_idx]
            parsed = json.loads(raw)
            for item in parsed.get("comments", []):
                code = str(item.get("stockCode", ""))
                if code:
                    cache[code] = {"comment": item.get("comment", ""), "generatedAt": today_str}
            logger.info(f"モメンタムコメント生成: {len(new_signals)}銘柄（新規/期限切れ） → キャッシュ保存")
        except Exception as e:
            logger.warning(f"モメンタムコメント生成エラー: {e}")

        # キャッシュ保存
        try:
            MOMENTUM_COMMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(MOMENTUM_COMMENT_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"コメントキャッシュ保存エラー: {e}")
    else:
        logger.info(f"モメンタムコメント: 全{len(signals)}銘柄キャッシュ済み → API呼び出しなし")

    def _get_comment(entry) -> str:
        if isinstance(entry, str):
            return entry
        return entry.get("comment", "") if isinstance(entry, dict) else ""

    return {s.get("stockCode", ""): _get_comment(cache.get(s.get("stockCode", ""), "")) for s in signals}


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

    try:
        recorded = record_outcomes(df_all)
        if recorded > 0:
            logger.info(f"過去エントリの結果記録: {recorded}件更新")
    except Exception as e:
        logger.warning(f"結果記録エラー（スキップ）: {e}")

    results = []
    code_col = "Code" if "Code" in df_all.columns else "code"

    # ---- 急騰理由生成: TDnet適時開示 + Google News RSS → Claude API（全銘柄対象） ----
    surge_reason_map = {}
    try:
        from agents.tdnet_fetcher import get_disclosures_for_stock

        # 急騰日（スキャン日 or scanDate）を特定
        # 複数日ある場合も考慮して各銘柄ごとに取得
        logger.info("  TDnet適時開示を取得中...")
        stocks_with_info = []
        for s in signals:
            code = s.get("stockCode", "")
            name = s.get("companyName", "")
            surge_date = s.get("scanDate", datetime.now().strftime("%Y-%m-%d"))

            # TDnet: 当日 + 直前2日分を検索
            tdnet_matches = get_disclosures_for_stock(code, surge_date, look_back_days=2)

            # Google News: TDnet で情報が得られなかった銘柄のみフォールバック
            news = []
            if not tdnet_matches:
                news = _fetch_stock_news(name, code)
                time.sleep(0.3)

            stocks_with_info.append({
                "stockCode": code,
                "companyName": name,
                "price_change_pct": s.get("price_change_pct", 0),
                "volume_ratio": s.get("volume_ratio", 0),
                "tdnet_disclosures": tdnet_matches,
                "news": news,
            })

        tdnet_hit = sum(1 for s in stocks_with_info if s["tdnet_disclosures"])
        news_hit = sum(1 for s in stocks_with_info if s["news"])
        logger.info(f"  情報取得完了: TDnet={tdnet_hit}件 / Google News={news_hit}件")

        surge_reason_map = _generate_surge_reasons_batch(stocks_with_info)
        logger.info(f"  急騰理由生成完了: {len(surge_reason_map)}銘柄")
    except Exception as e:
        logger.warning(f"急騰理由生成エラー（スキップ）: {e}")

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
        result["surgeReason"] = surge_reason_map.get(stock_code, "")

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

        result["outcome"] = None
        results.append(result)

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

    new_keys = {(r.get("stockCode"), r.get("scanDate")) for r in results}
    existing = [
        e for e in existing
        if (e.get("stockCode"), e.get("scanDate")) not in new_keys
    ]

    existing.extend(results)
    existing = existing[-500:]

    with open(QUALIFY_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    logger.info(f"判定ログを保存しました: {QUALIFY_LOG_PATH}（累計{len(existing)}件）")


def format_qualify_result_for_slack(results: list) -> str:
    """判定結果をSlack通知用のテキストにフォーマットする。"""
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
            days_checked = s1.get("daysChecked", -1)
            comment = s2.get("comment", "")
            confidence = s2.get("confidence", "")
            surge_reason = r.get("surgeReason", "")
            vol_emoji = "📊" if s1.get("volumeSustained") else "📉"
            price_emoji = "✅" if s1.get("priceSustained") else "❌"
            if days_checked == 0:
                vol_str = "出来高:最新日（翌日以降確認）"
                price_str = "急騰後:継続確認中"
            else:
                vol_str = f"出来高維持率:{vol_rate:.0%}"
                price_str = f"急騰後株価:{price_chg:+.1f}%"
            reason_line = f"\n  💡 急騰理由: {surge_reason}" if surge_reason else ""
            lines.append(
                f"• *{r.get('stockCode')} {r.get('companyName', '')}*\n"
                f"  {vol_emoji} {vol_str}  "
                f"{price_emoji} {price_str}\n"
                f"  🤖 Claude({confidence}): {comment}"
                f"{reason_line}"
            )

    if watch_results:
        lines.append("\n*【WATCH】要観察（APIキー未設定）*")
        for r in watch_results:
            s1 = r.get("stage1", {})
            vol_rate = s1.get("volumeSustainRate", 0)
            price_chg = s1.get("priceChangeAfterSurge", 0)
            days_checked = s1.get("daysChecked", -1)
            if days_checked == 0:
                vol_str = "最新日（翌日以降確認）"
                price_str = "継続確認中"
            else:
                vol_str = f"{vol_rate:.0%}"
                price_str = f"{price_chg:+.1f}%"
            lines.append(
                f"• {r.get('stockCode')} {r.get('companyName', '')} "
                f"出来高維持:{vol_str} 株価:{price_str}"
            )

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
