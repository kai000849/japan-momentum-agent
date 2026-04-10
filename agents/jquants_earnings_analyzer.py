"""
agents/jquants_earnings_analyzer.py
J-Quants /fins/summary の決算速報をHaikuで分析して正サプライズ銘柄を抽出するモジュール

【役割】
- J-Quants が 18:00頃配信する当日の決算速報データを分析
- 営業利益YoY変化率・通期進捗率を計算して定量フィルタ
- Claude Haiku で「モメンタムにつながる正サプライズか？」を判定
- EDINETベースの既存パイプライン（Sonnetによるqualify）とは独立して動作
- 夕方18:30スキャンに組み込んで即日通知を実現

【モデル選定の根拠】
構造化された数値データを処理するため Haiku で十分（コスト最適化）。
EDINETのPDF全文解析はSonnetが担当（複雑な文書理解が必要）。

作者: Japan Momentum Agent
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd

logger = logging.getLogger(__name__)

# Haikuモデル（コスト最適化。構造化数値データの分析は高精度モデル不要）
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ========================================
# 定量フィルタ閾値
# ========================================

# 営業利益YoY変化率（前期比）の下限（%）
# これ以上の増益 or これ以下の減益でフィルタ
OP_YOY_THRESHOLD_POSITIVE = 20.0   # +20%以上の増益を正サプライズ候補とする
OP_YOY_THRESHOLD_NEGATIVE = -50.0  # -50%以下の大幅減益は除外

# 通期進捗率（累計実績 / 通期予想）の下限（%）
# 通期進捗が高いほどモメンタム継続の期待が高い
PROGRESS_RATE_THRESHOLD = 70.0  # 通期進捗70%以上を優先

# 最低売上規模（億円）: 超小型株ノイズ排除
MIN_SALES_BILLION = 10.0  # 売上10億円未満は対象外（百万円単位のAPIデータ）

# Haiku分析の最大バッチサイズ（1回の呼び出しで処理する最大銘柄数）
HAIKU_BATCH_SIZE = 10


# ========================================
# メイン分析関数
# ========================================

def analyze_todays_earnings(df: pd.DataFrame, target_date: Optional[str] = None) -> list[dict]:
    """
    J-Quants決算速報DataFrameを受け取り、正サプライズ銘柄リストを返す。

    処理フロー:
    1. DocType フィルタ（通常決算・四半期決算のみ）
    2. 定量計算（YoY変化率・通期進捗率）
    3. 定量フィルタ（閾値でスクリーニング）
    4. Claude Haiku でバッチ判定

    Args:
        df: get_todays_earnings() が返す決算速報DataFrame
        target_date: "YYYY-MM-DD" 形式の対象日（省略時は今日）

    Returns:
        list[dict]: 正サプライズ候補のシグナルリスト
                    各dict: stockCode, companyName, signal, score, reason,
                            op_yoy, progress_rate, doc_type, cur_per_type
    """
    if df is None or df.empty:
        logger.info("決算速報データなし → 分析スキップ")
        return []

    today_str = target_date or date.today().isoformat()

    # ---- Step 1: DocTypeフィルタ ----
    # DocType: "120"=決算短信（通期）, "130"=四半期決算短信
    # "140"=修正, "150"=配当修正 等は除外（モメンタム起点として不適）
    VALID_DOC_TYPES = {"120", "130", "140"}  # 140=業績修正も含める
    if "DocType" in df.columns:
        df = df[df["DocType"].isin(VALID_DOC_TYPES)].copy()

    if df.empty:
        logger.info("決算速報: 有効なDocTypeなし → スキップ")
        return []

    # ---- Step 2: 定量計算 ----
    records = _compute_metrics(df)

    if not records:
        logger.info("決算速報: 指標計算後のレコードなし")
        return []

    # ---- Step 3: 定量フィルタ ----
    candidates = _apply_quantitative_filter(records)
    logger.info(f"決算速報: {len(df)}件 → 定量フィルタ通過 {len(candidates)}件")

    if not candidates:
        return []

    # ---- Step 4: Haiku判定 ----
    signals = _analyze_with_haiku(candidates, today_str)
    logger.info(f"決算速報: Haiku判定完了 → {len(signals)}件の正サプライズシグナル")

    return signals


# ========================================
# 内部ヘルパー関数
# ========================================

def _compute_metrics(df: pd.DataFrame) -> list[dict]:
    """DataFrameから各銘柄の指標を計算してdict化する。"""
    records = []
    for _, row in df.iterrows():
        try:
            code = str(row.get("Code", "")).strip()
            if not code:
                continue

            # 売上（百万円）→ 億円換算でノイズ除去
            sales = _to_float(row.get("Sales"))
            if sales is None or sales < MIN_SALES_BILLION * 100:  # 百万円単位
                continue

            # 営業利益YoY変化率
            op = _to_float(row.get("OP"))
            nc_op = _to_float(row.get("NCOP"))  # 前期実績
            op_yoy = None
            if op is not None and nc_op is not None and nc_op != 0:
                op_yoy = (op - nc_op) / abs(nc_op) * 100.0
            elif op is not None and nc_op is not None and nc_op == 0 and op > 0:
                op_yoy = 999.0  # 前期赤字→今期黒字転換

            # 通期進捗率（累計OP / 通期予想OP）
            f_op = _to_float(row.get("FOP"))  # 通期予想
            progress_rate = None
            if op is not None and f_op is not None and f_op > 0:
                progress_rate = op / f_op * 100.0

            # 売上YoY
            nc_sales = _to_float(row.get("NCSales"))
            sales_yoy = None
            if sales is not None and nc_sales is not None and nc_sales != 0:
                sales_yoy = (sales - nc_sales) / abs(nc_sales) * 100.0

            # 純利益YoY
            np_val = _to_float(row.get("NP"))
            nc_np = _to_float(row.get("NCNP"))
            np_yoy = None
            if np_val is not None and nc_np is not None and nc_np != 0:
                np_yoy = (np_val - nc_np) / abs(nc_np) * 100.0

            record = {
                "stockCode": code,
                "discDate": str(row.get("DiscDate", "")),
                "discTime": str(row.get("DiscTime", "")),
                "docType": str(row.get("DocType", "")),
                "curPerType": str(row.get("CurPerType", "")),
                "sales_millon": sales,
                "op_million": op,
                "nc_op_million": nc_op,
                "op_yoy": op_yoy,
                "progress_rate": progress_rate,
                "sales_yoy": sales_yoy,
                "np_yoy": np_yoy,
            }
            records.append(record)

        except Exception as e:
            logger.debug(f"指標計算エラー ({row.get('Code', '?')}): {e}")
            continue

    return records


def _apply_quantitative_filter(records: list[dict]) -> list[dict]:
    """定量条件でフィルタリング。正サプライズ候補のみ返す。"""
    candidates = []
    for r in records:
        op_yoy = r.get("op_yoy")
        progress_rate = r.get("progress_rate")

        # 大幅減益は除外
        if op_yoy is not None and op_yoy < OP_YOY_THRESHOLD_NEGATIVE:
            continue

        # 正サプライズ条件: 増益20%以上 OR 進捗率70%以上
        is_positive = False
        if op_yoy is not None and op_yoy >= OP_YOY_THRESHOLD_POSITIVE:
            is_positive = True
        if progress_rate is not None and progress_rate >= PROGRESS_RATE_THRESHOLD:
            is_positive = True

        if is_positive:
            candidates.append(r)

    return candidates


def _analyze_with_haiku(candidates: list[dict], scan_date: str) -> list[dict]:
    """Claude Haikuでバッチ分析して正サプライズシグナルを返す。"""
    if not candidates:
        return []

    api_key = _get_api_key()
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY未設定 → Haiku分析スキップ（定量フィルタ結果のみ返却）")
        # APIキーなしでも定量データだけで簡易シグナルを返す
        return [_make_simple_signal(c, scan_date) for c in candidates]

    client = anthropic.Anthropic(api_key=api_key)
    signals = []

    # バッチ分割して順次処理
    for i in range(0, len(candidates), HAIKU_BATCH_SIZE):
        batch = candidates[i:i + HAIKU_BATCH_SIZE]
        batch_signals = _run_haiku_batch(client, batch, scan_date)
        signals.extend(batch_signals)

    return signals


def _run_haiku_batch(client: anthropic.Anthropic, batch: list[dict], scan_date: str) -> list[dict]:
    """1バッチ分のHaiku分析を実行する。"""
    # 銘柄データをコンパクトなJSON文字列に変換
    batch_data = []
    for c in batch:
        item = {
            "code": c["stockCode"],
            "docType": c["docType"],
            "curPerType": c["curPerType"],
            "op_yoy": round(c["op_yoy"], 1) if c.get("op_yoy") is not None else None,
            "progress": round(c["progress_rate"], 1) if c.get("progress_rate") is not None else None,
            "sales_yoy": round(c["sales_yoy"], 1) if c.get("sales_yoy") is not None else None,
            "np_yoy": round(c["np_yoy"], 1) if c.get("np_yoy") is not None else None,
        }
        batch_data.append(item)

    prompt = f"""以下は{scan_date}に発表された決算速報データです。各銘柄について、翌営業日以降のモメンタム（株価上昇トレンド継続）につながる正サプライズかどうかを判定してください。

決算データ:
{json.dumps(batch_data, ensure_ascii=False, indent=2)}

各フィールドの説明:
- op_yoy: 営業利益の前期比変化率（%）
- progress: 通期予想に対する累計進捗率（%）
- sales_yoy: 売上高の前期比変化率（%）
- np_yoy: 純利益の前期比変化率（%）
- docType: 120=通期決算, 130=四半期決算, 140=業績修正
- curPerType: 決算期タイプ（FY=通期, Q1/Q2/Q3=四半期）

判定基準（モメンタム起点になりやすい決算の特徴）:
1. 営業利益が大幅増益（+30%以上）かつ売上も増収
2. 四半期ベースで加速している（Q1<Q2<Q3など右肩上がり）
3. 通期進捗率が高水準（80%以上）で上振れ期待あり
4. 業績修正（DocType=140）で大幅上方修正

各銘柄に対して以下のJSON形式で回答してください:
[
  {{
    "code": "銘柄コード",
    "score": 1〜5の整数（5=強い正サプライズ、1=微妙）,
    "signal": "STRONG_POSITIVE" | "POSITIVE" | "NEUTRAL" | "NEGATIVE",
    "reason": "判定理由を1〜2文で（日本語）"
  }}
]

score 4以上かつsignal=STRONG_POSITIVEまたはPOSITIVEの銘柄のみ返してください。
該当なしの場合は空配列 [] を返してください。"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=len(batch) * 150 + 200,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text.strip()

        # JSON抽出
        parsed = _extract_json(content)
        if not isinstance(parsed, list):
            logger.warning(f"Haiku返却がリストでない: {content[:200]}")
            return []

        # シグナルを構築
        code_to_candidate = {c["stockCode"]: c for c in batch}
        signals = []
        for item in parsed:
            code = str(item.get("code", ""))
            if code not in code_to_candidate:
                continue
            cand = code_to_candidate[code]
            signal = {
                "stockCode": code,
                "scanDate": scan_date,
                "signalType": "JQUANTS_EARNINGS",
                "score": item.get("score", 3),
                "signal": item.get("signal", "POSITIVE"),
                "reason": item.get("reason", ""),
                "op_yoy": cand.get("op_yoy"),
                "progress_rate": cand.get("progress_rate"),
                "sales_yoy": cand.get("sales_yoy"),
                "np_yoy": cand.get("np_yoy"),
                "docType": cand.get("docType"),
                "curPerType": cand.get("curPerType"),
                "discTime": cand.get("discTime"),
            }
            signals.append(signal)

        return signals

    except Exception as e:
        logger.warning(f"Haiku分析エラー: {e}")
        return [_make_simple_signal(c, scan_date) for c in batch]


def _make_simple_signal(c: dict, scan_date: str) -> dict:
    """APIキーなし・エラー時のフォールバック: 定量データのみのシグナルを返す。"""
    op_yoy = c.get("op_yoy")
    progress_rate = c.get("progress_rate")

    score = 3
    if op_yoy is not None and op_yoy >= 50:
        score = 4
    if progress_rate is not None and progress_rate >= 80:
        score = max(score, 4)

    reason_parts = []
    if op_yoy is not None:
        reason_parts.append(f"営業利益{op_yoy:+.1f}%")
    if progress_rate is not None:
        reason_parts.append(f"通期進捗{progress_rate:.0f}%")

    return {
        "stockCode": c["stockCode"],
        "scanDate": scan_date,
        "signalType": "JQUANTS_EARNINGS",
        "score": score,
        "signal": "POSITIVE",
        "reason": "・".join(reason_parts) if reason_parts else "定量条件クリア",
        "op_yoy": c.get("op_yoy"),
        "progress_rate": c.get("progress_rate"),
        "sales_yoy": c.get("sales_yoy"),
        "np_yoy": c.get("np_yoy"),
        "docType": c.get("docType"),
        "curPerType": c.get("curPerType"),
        "discTime": c.get("discTime"),
    }


def _extract_json(text: str) -> list:
    """テキストからJSONリストを抽出する。"""
    import re
    # ```json ... ``` ブロックを除去
    text = re.sub(r"```(?:json)?", "", text).strip()
    # [ ... ] を抽出
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _to_float(val) -> Optional[float]:
    """値をfloatに変換。変換できない場合はNone。"""
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _get_api_key() -> Optional[str]:
    """Anthropic APIキーを取得（環境変数 or config.yaml）。"""
    # 環境変数から取得
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    # config.yaml から取得
    try:
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            key = str(config.get("anthropic", {}).get("api_key", "")).strip()
            if key and key != "YOUR_ANTHROPIC_API_KEY":
                return key
    except Exception:
        pass
    return None
