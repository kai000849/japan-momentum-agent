"""
agents/edinet_analyzer.py
EDINET決算書類をClaude APIで読解・スコアリングするモジュール

【処理フロー】
1. EDINET APIから決算短信PDFをダウンロード
2. pdfplumberで先頭15ページをテキスト抽出（base64渡し廃止・トークン節約）
3. 分析結果キャッシュを確認（同日中の重複分析を防止・コスト削減）
4. キャッシュ未ヒット時のみClaude APIで分析：
   - 売上・営業利益の前年比・予想比
   - ポジ/ネガスコア（-100〜+100）
   - 理由コメント（2〜3行）
   - 構造的変化の有無
5. スコア上位（ポジ）・下位（ネガ）をランキング化して返す

作者: Japan Momentum Agent
"""

import io
import json
import logging
import os
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ========================================
# 定数
# ========================================

EDINET_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

# Claude APIエンドポイント
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"  # 複雑な文脈理解が必要なためSonnet使用
MAX_TOKENS = 1500

# PDFから抽出するページ数の上限（決算短信の重要数字は冒頭に集中）
PDF_MAX_PAGES = 15


# スコアリング対象の書類種別（決算短信のみ）
TARGET_DOC_TYPES = ["180", "130", "140", "030"]

# PDFダウンロード保存先
PDF_CACHE_DIR = Path(__file__).parent.parent / "data" / "raw" / "edinet_pdfs"

# 分析結果キャッシュファイル
ANALYSIS_CACHE_PATH = Path(__file__).parent.parent / "data" / "processed" / "edinet_analysis_cache.json"

# キャッシュ有効期間（日数）
CACHE_TTL_DAYS = 7


# ========================================
# APIキー取得
# ========================================

from agents.utils import get_anthropic_key as _get_anthropic_key


def _get_edinet_key() -> str:
    """EDINET APIキーを取得する。"""
    key = os.environ.get("EDINET_API_KEY", "")
    if key:
        return key
    try:
        import yaml
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config.get("edinet", {}).get("api_key", "")
    except Exception:
        pass
    return ""


# ========================================
# 分析結果キャッシュ
# ========================================

def _load_analysis_cache() -> dict:
    """
    分析結果キャッシュをファイルから読み込む。

    Returns:
        dict: {docID: {result: dict, cached_at: str}} 形式のキャッシュ
    """
    if not ANALYSIS_CACHE_PATH.exists():
        return {}
    try:
        with open(ANALYSIS_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"キャッシュ読み込みエラー: {e} → 空キャッシュで続行")
        return {}


def _save_analysis_cache(cache: dict) -> None:
    """
    分析結果キャッシュをファイルに保存する。
    """
    ANALYSIS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(ANALYSIS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"キャッシュ保存エラー: {e}")


def _purge_expired_cache(cache: dict) -> dict:
    """
    有効期限切れのキャッシュエントリを削除して返す。

    Args:
        cache (dict): 現在のキャッシュ

    Returns:
        dict: 期限切れエントリを除いたキャッシュ
    """
    cutoff = datetime.now() - timedelta(days=CACHE_TTL_DAYS)
    purged = {}
    removed = 0
    for doc_id, entry in cache.items():
        try:
            cached_at = datetime.fromisoformat(entry.get("cached_at", ""))
            if cached_at >= cutoff:
                purged[doc_id] = entry
            else:
                removed += 1
        except Exception:
            # 日付パース失敗のエントリは削除
            removed += 1
    if removed > 0:
        logger.info(f"期限切れキャッシュを削除: {removed}件")
    return purged


def _get_cached_result(doc_id: str, cache: dict) -> dict | None:
    """
    キャッシュから分析結果を取得する。

    Args:
        doc_id (str): EDINETの書類ID
        cache (dict): キャッシュデータ

    Returns:
        dict: キャッシュされた分析結果。未ヒット時はNone
    """
    entry = cache.get(doc_id)
    if not entry:
        return None
    return entry.get("result")


def _set_cached_result(doc_id: str, result: dict, cache: dict) -> None:
    """
    分析結果をキャッシュに追加する（インプレース更新）。

    Args:
        doc_id (str): EDINETの書類ID
        result (dict): Claude APIの分析結果
        cache (dict): キャッシュデータ（インプレース更新）
    """
    cache[doc_id] = {
        "result": result,
        "cached_at": datetime.now().isoformat()
    }


# ========================================
# PDFダウンロード
# ========================================

def download_earnings_pdf(doc_id: str) -> bytes | None:
    """
    EDINET APIから決算短信PDFをダウンロードする。

    Args:
        doc_id (str): EDINETの書類ID（例: "S100XXXX"）

    Returns:
        bytes: PDFバイナリ。失敗時はNone
    """
    # キャッシュ確認
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = PDF_CACHE_DIR / f"{doc_id}.pdf"
    if cache_path.exists():
        logger.info(f"PDFキャッシュ使用: {doc_id}")
        return cache_path.read_bytes()

    api_key = _get_edinet_key()
    url = f"{EDINET_BASE_URL}/documents/{doc_id}"
    params = {
        "type": 2,
        "Subscription-Key": api_key
    }

    try:
        logger.info(f"ドキュメントダウンロード中: {doc_id}")
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()

        content = resp.content
        if len(content) < 100:
            logger.warning(f"ドキュメント取得失敗（レスポンス小さすぎ）: {doc_id}")
            return None

        # PDFシグネチャで直接PDF判定
        if content[:4] == b'%PDF':
            cache_path.write_bytes(content)
            logger.info(f"PDF直接取得完了: {doc_id} ({len(content):,} bytes)")
            return content

        # ZIPファイルの場合、中からPDFを取り出す
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                pdf_files = [n for n in zf.namelist() if n.lower().endswith('.pdf')]
                if pdf_files:
                    pdf_bytes = zf.read(pdf_files[0])
                    cache_path.write_bytes(pdf_bytes)
                    logger.info(f"ZIP内PDF取得完了: {doc_id} / {pdf_files[0]} ({len(pdf_bytes):,} bytes)")
                    return pdf_bytes
                # ZIP内にPDFなし → ファイル一覧をログ出力
                logger.warning(f"ZIP内にPDFなし: {doc_id} (ファイル: {zf.namelist()[:5]})")
                return None
        except zipfile.BadZipFile:
            pass

        logger.warning(f"PDF取得失敗（未対応フォーマット）: {doc_id} content-type={resp.headers.get('Content-Type','')}")
        return None

    except Exception as e:
        logger.warning(f"PDFダウンロードエラー {doc_id}: {e}")
        return None


# ========================================
# PDFテキスト抽出（pdfplumber使用）
# ========================================

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    pdfplumberを使ってPDFから先頭ページのテキストを抽出する。
    決算短信の重要数値（売上・利益・予想比）は冒頭に集中しているため、
    先頭PDF_MAX_PAGESページのみ抽出してトークンを節約する。

    Args:
        pdf_bytes (bytes): PDFバイナリ

    Returns:
        str: 抽出テキスト（最大PDF_MAX_CHARS文字）
    """
    try:
        import pdfplumber
        import io

        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages)
            pages_to_read = min(PDF_MAX_PAGES, total_pages)
            logger.info(f"PDFテキスト抽出: {pages_to_read}/{total_pages}ページ")

            for i, page in enumerate(pdf.pages[:pages_to_read]):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- {i+1}ページ ---\n{page_text}")

        full_text = "\n".join(text_parts)

        logger.info(f"テキスト抽出完了: {len(full_text)}文字")
        return full_text

    except ImportError:
        logger.warning("pdfplumberが未インストール。pip install pdfplumber を実行してください。")
        return ""
    except Exception as e:
        logger.warning(f"PDFテキスト抽出エラー: {e}")
        return ""


# ========================================
# Claude APIで決算分析
# ========================================

def analyze_earnings_pdf(
    pdf_bytes: bytes,
    company_name: str,
    doc_description: str
) -> dict:
    """
    決算短信PDFをClaude APIで分析してスコアリングする。
    PDFはbase64渡しせず、pdfplumberで事前テキスト抽出してから渡す（トークン節約）。

    Args:
        pdf_bytes (bytes): PDFバイナリ
        company_name (str): 会社名
        doc_description (str): 書類概要

    Returns:
        dict: {
            "score": int,           # -100〜+100（ポジ/ネガ）
            "summary": str,         # 2〜3行の要約コメント
            "positive_points": list,# ポジティブな点（最大3件）
            "negative_points": list,# ネガティブな点（最大3件）
            "structural_change": bool,  # 構造的変化の有無
            "structural_comment": str,  # 構造的変化のコメント
            "revenue_yoy": str,     # 売上前年比（例: "+12%"）
            "profit_yoy": str,      # 営業利益前年比
            "vs_forecast": str,     # 予想比（例: "上振れ+5%"）
            "error": str            # エラー時のみ
        }
    """
    api_key = _get_anthropic_key()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY未設定", "score": 0}

    pdf_text = extract_text_from_pdf(pdf_bytes)
    if not pdf_text:
        return {"error": "PDFテキスト抽出失敗", "score": 0}

    # 過去精度フィードバック（5件以上のoutcome10dがある場合のみ追加）
    accuracy_context = ""
    try:
        from agents.earnings_momentum_scanner import get_earnings_patterns
        patterns = get_earnings_patterns()
        if not patterns.get("insufficient", True) and patterns.get("total", 0) >= 5:
            lines = [
                f"参考情報：過去の決算シグナル判定実績（{patterns['total']}件・10日後リターン基準）"
            ]
            mp_data = patterns.get("by_momentum_potential", {})
            if mp_data:
                parts = [
                    f"{k}→勝率{v['win_rate']:.0f}%/平均{v['avg_return']:+.1f}%({v['count']}件)"
                    for k, v in mp_data.items()
                ]
                lines.append(f"  momentum_potential別: {' / '.join(parts)}")
            ct_data = patterns.get("by_catalyst_type", {})
            if ct_data:
                parts = [
                    f"{k}→勝率{v['win_rate']:.0f}%({v['count']}件)"
                    for k, v in sorted(ct_data.items(), key=lambda x: x[1]["win_rate"], reverse=True)
                ]
                lines.append(f"  catalystType別: {' / '.join(parts)}")
            lines.append(
                "→ 上記の実績を踏まえて momentum_potential と score の判断を行ってください。"
            )
            accuracy_context = "\n".join(lines) + "\n\n"
    except Exception:
        pass

    prompt = f"""{accuracy_context}あなたは日本株投資の決算分析専門家です。
以下の決算書類テキスト（{company_name} / {doc_description}）を読み、投資判断に役立つ分析を行ってください。

【決算書類テキスト（先頭{PDF_MAX_PAGES}ページ抜粋）】
{pdf_text}

【出力形式】
必ず以下のJSON形式のみで回答してください。前置きや説明は不要です。

{{
  "score": <-100から+100の整数。強いポジはプラス大、強いネガはマイナス大>,
  "summary": "<2〜3文で決算の核心をまとめる。数字を必ず含める>",
  "positive_points": ["<ポジティブな点1>", "<ポジティブな点2>"],
  "negative_points": ["<ネガティブな点1>", "<ネガティブな点2>"],
  "structural_change": <true/false。事業構造・競争環境に大きな変化があればtrue>,
  "structural_comment": "<structural_changeがtrueの場合のみ、変化の内容を1文で>",
  "revenue_yoy": "<売上の前年同期比。例: +12.3%、-5.1%、不明>",
  "profit_yoy": "<営業利益の前年同期比。例: +34.2%、-41.0%、不明>",
  "vs_forecast": "<会社予想比または市場予想比。例: 上振れ+8%、下振れ-12%、概ね一致、不明>",
  "momentum_potential": "<high/medium/low。この開示を受けて株価の上昇モメンタムが中長期(1〜3ヶ月)で継続する可能性>",
  "entry_timing": "<寄り付き反応確認後がベスト / 材料出尽くし注意 / 見送り推奨 のいずれか>",
  "catalyst_type": "<上方修正 / 増収増益 / 新規事業・提携 / 自社株買い・増配 / 復配・特別配当 / その他 のいずれか>"
}}

【スコア基準】
+80〜+100: 大幅上振れ・強気ガイダンス・構造的好転
+40〜+79: 増益・予想超え・ポジティブな変化
+10〜+39: 軽微なポジティブ
-10〜+9: ほぼ中立
-10〜-39: 軽微なネガティブ
-40〜-79: 減益・下方修正・懸念あり
-80〜-100: 大幅下振れ・業績悪化・構造的問題

【momentum_potential 判定基準】
high: 業績の上方修正幅が大きい・ガイダンスが強気・構造的変化あり・新規大型契約
medium: 小幅増益・予想超え・方向感ポジティブだが継続性は不確か
low: 一時的要因による増益・横ばい・下方修正懸念残り
"""

    try:
        import anthropic
        import re

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )

        raw_text = response.content[0].text.strip()

        raw_text = re.sub(r'```(?:json)?', '', raw_text)
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start >= 0 and end > start:
            raw_text = raw_text[start:end]

        result = json.loads(raw_text)
        logger.info(f"Claude分析完了: {company_name} スコア={result.get('score', 0)}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSONパースエラー ({company_name}): {e}\n生テキスト: {raw_text[:200]}")
        return {"error": f"JSONパースエラー: {e}", "score": 0}
    except Exception as e:
        logger.error(f"Claude API呼び出しエラー ({company_name}): {e}")
        return {"error": str(e), "score": 0}


# ========================================
# メイン：決算銘柄を一括分析
# ========================================

def analyze_earnings_batch(earnings_list: list) -> list:
    """
    決算発表銘柄リストを一括分析してスコア付きで返す。
    同一docIDはキャッシュから返すため、朝・夕の2回実行でも重複課金されない。

    Args:
        earnings_list (list): edinet_fetcher.get_earnings_announcements()の戻り値

    Returns:
        list: スコア付き決算分析結果リスト
    """
    if not earnings_list:
        logger.info("分析対象の決算書類がありません。")
        return []

    # 決算短信（180）を優先、その他は後回し
    priority_types = ["180"]
    other_types = ["130", "140", "030"]

    sorted_earnings = (
        [e for e in earnings_list if e.get("docTypeCode") in priority_types] +
        [e for e in earnings_list if e.get("docTypeCode") in other_types]
    )

    # 重複排除（同一銘柄の複数書類は1件だけ処理）
    seen_codes = set()
    unique_earnings = []
    for e in sorted_earnings:
        code = e.get("secCode", "")
        if code not in seen_codes:
            seen_codes.add(code)
            unique_earnings.append(e)

    logger.info(f"決算分析開始: {len(unique_earnings)}銘柄（重複除去後）")

    # キャッシュ読み込み＆期限切れ削除
    cache = _load_analysis_cache()
    cache = _purge_expired_cache(cache)
    cache_hits = 0

    results = []
    api_key = _get_anthropic_key()

    for i, earning in enumerate(unique_earnings):
        company_name = earning.get("companyName", "不明")
        doc_id = earning.get("docID", "")
        doc_type = earning.get("docTypeCode", "")
        doc_desc = earning.get("docDescription", "")
        sec_code = earning.get("secCode", "")

        # ---- キャッシュ確認 ----
        if doc_id:
            cached = _get_cached_result(doc_id, cache)
            if cached is not None:
                logger.info(f"[{i+1}/{len(unique_earnings)}] {company_name} → キャッシュヒット（API節約）")
                results.append({
                    "stockCode": sec_code,
                    "companyName": company_name,
                    "docTypeCode": doc_type,
                    "docDescription": doc_desc,
                    **cached,
                    "analyzed": "error" not in cached,
                    "from_cache": True
                })
                cache_hits += 1
                continue

        logger.info(f"[{i+1}/{len(unique_earnings)}] {company_name} ({sec_code}) 分析中...")

        # APIキーまたはdoc_idがない場合はスキップ
        if not api_key or not doc_id:
            reason = "APIキー未設定のため未分析" if not api_key else "書類ID未取得のため未分析"
            logger.warning(f"{company_name}: {reason} (api_key={'有' if api_key else '無'}, doc_id='{doc_id}')")
            results.append({
                "stockCode": sec_code,
                "companyName": company_name,
                "docTypeCode": doc_type,
                "docDescription": doc_desc,
                "score": 0,
                "summary": reason,
                "positive_points": [],
                "negative_points": [],
                "structural_change": False,
                "structural_comment": "",
                "revenue_yoy": "不明",
                "profit_yoy": "不明",
                "vs_forecast": "不明",
                "analyzed": False,
                "skip_reason": "no_api_key" if not api_key else "no_doc_id"
            })
            continue

        # PDFダウンロード
        pdf_bytes = download_earnings_pdf(doc_id)
        if not pdf_bytes:
            logger.warning(f"PDF取得失敗: {company_name} → スキップ")
            results.append({
                "stockCode": sec_code,
                "companyName": company_name,
                "docTypeCode": doc_type,
                "docDescription": doc_desc,
                "score": 0,
                "summary": "PDF取得失敗",
                "positive_points": [],
                "negative_points": [],
                "structural_change": False,
                "structural_comment": "",
                "revenue_yoy": "不明",
                "profit_yoy": "不明",
                "vs_forecast": "不明",
                "analyzed": False
            })
            continue

        # Claude APIで分析
        analysis = analyze_earnings_pdf(pdf_bytes, company_name, doc_desc)

        # 成功した分析結果をキャッシュに保存
        if "error" not in analysis and doc_id:
            _set_cached_result(doc_id, analysis, cache)

        results.append({
            "stockCode": sec_code,
            "companyName": company_name,
            "docTypeCode": doc_type,
            "docDescription": doc_desc,
            "score": analysis.get("score", 0),
            "summary": analysis.get("summary", ""),
            "positive_points": analysis.get("positive_points", []),
            "negative_points": analysis.get("negative_points", []),
            "structural_change": analysis.get("structural_change", False),
            "structural_comment": analysis.get("structural_comment", ""),
            "revenue_yoy": analysis.get("revenue_yoy", "不明"),
            "profit_yoy": analysis.get("profit_yoy", "不明"),
            "vs_forecast": analysis.get("vs_forecast", "不明"),
            "momentum_potential": analysis.get("momentum_potential", "medium"),
            "entry_timing": analysis.get("entry_timing", ""),
            "catalyst_type": analysis.get("catalyst_type", ""),
            "analyzed": "error" not in analysis,
            "from_cache": False,
            "error": analysis.get("error", "")
        })

        # APIレート制限対策（1秒待機）
        if i < len(unique_earnings) - 1:
            time.sleep(1.0)

    # キャッシュをファイルに書き戻す
    _save_analysis_cache(cache)
    logger.info(f"決算分析完了: {len(results)}件（キャッシュヒット: {cache_hits}件・API呼び出し: {len(results) - cache_hits}件）")

    return results


def get_best_worst_earnings(analyzed_results: list, top_n: int = 10) -> dict:
    """
    分析済み結果からベスト/ワーストをランキング化する。

    Args:
        analyzed_results (list): analyze_earnings_batch()の戻り値
        top_n (int): 取得件数（デフォルト10件）

    Returns:
        dict: {"best": [...], "worst": [...]}
    """
    analyzed = [r for r in analyzed_results if r.get("analyzed", False)]
    unanalyzed = [r for r in analyzed_results if not r.get("analyzed", False)]

    sorted_results = sorted(analyzed, key=lambda x: x.get("score", 0), reverse=True)

    best = [r for r in sorted_results if r.get("score", 0) > 0][:top_n]
    worst = [r for r in reversed(sorted_results) if r.get("score", 0) < 0][:top_n]

    cache_count = sum(1 for r in analyzed_results if r.get("from_cache", False))
    logger.info(f"ベスト: {len(best)}件 / ワースト: {len(worst)}件 / 未分析: {len(unanalyzed)}件 / キャッシュ利用: {cache_count}件")

    return {
        "best": best,
        "worst": worst,
        "unanalyzed": unanalyzed
    }
