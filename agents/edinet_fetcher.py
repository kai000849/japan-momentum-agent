"""
agents/edinet_fetcher.py
EDINET APIから開示書類・決算発表情報を取得するモジュール

【EDINETとは】
金融庁が提供する電子開示システム。上場企業の決算短信・有価証券報告書・
大量保有報告書などの開示書類を検索・取得できる。

【主な書類種別コード】
- "030": 有価証券報告書（年次）
- "130": 四半期報告書
- "140": 半期報告書
- "180": 決算短信（最重要：四半期・通期の決算発表）
- "220": 大量保有報告書
- "160": 臨時報告書

作者: Japan Momentum Agent
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ========================================
# 設定ファイルの読み込み
# ========================================

def load_config() -> dict:
    """
    config.yamlから設定を読み込む。

    Returns:
        dict: 設定内容（APIキーなど）

    Raises:
        FileNotFoundError: config.yamlが存在しない場合
    """
    config_path = Path(__file__).parent.parent / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"エラー: 設定ファイルが見つかりません: {config_path}\n"
            "config.yaml を作成し、EDINET APIキーを設定してください。"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


# ========================================
# EDINET API 基本設定
# ========================================

# EDINET API のベースURL
EDINET_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

# EDINET書類種別コード（実測値に基づく正確なマッピング）
# ※ 決算短信はEDINETに存在しない。TDnet（東証）に提出される。
# ※ 以下は実際のEDINET V2 APIレスポンスで確認したコード体系。
EARNINGS_DOC_TYPES = [
    "120",  # 有価証券報告書（年次・本決算。5〜7月に多数提出）
    "180",  # 臨時報告書（M&A・業績修正・株主総会決議等の重要開示）
]

# 大量保有報告書の書類種別コード
LARGE_SHAREHOLDING_DOC_TYPES = [
    "220",  # 大量保有報告書
    "221",  # 大量保有報告書（特例対象株券等）
    "230",  # 変更報告書
    "231",  # 変更報告書（特例対象株券等）
]


# ========================================
# 開示書類一覧取得
# ========================================

def get_disclosure_list(date: str = None) -> list:
    """
    指定した日付の開示書類一覧を取得する。

    Args:
        date (str): 取得する日付（形式: "YYYY-MM-DD"）
                    Noneの場合は今日の日付を使用

    Returns:
        list: 開示書類のリスト。各要素はdict形式。
              主なキー: docID, filerName, edinetCode, docTypeCode,
                       docDescription, submitDateTime

    Raises:
        Exception: API呼び出し失敗時
    """
    # 日付が指定されていない場合は今日を使用
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # 設定からAPIキーを取得
    config = load_config()
    api_key = config.get("edinet", {}).get("api_key", "")

    # APIキーが初期値のままの場合の警告
    if api_key == "YOUR_EDINET_API_KEY":
        logger.warning(
            "警告: EDINET APIキーがデフォルト値です。\n"
            "config.yaml に実際のAPIキーを設定してください。\n"
            "（APIキーなしでも一部機能は動作しますが、制限があります）"
        )

    # APIエンドポイントとパラメータ
    url = f"{EDINET_BASE_URL}/documents.json"
    params = {
        "date": date,
        "type": 2,           # type=2: 書類一覧＋書類詳細を取得
        "Subscription-Key": api_key  # APIキー
    }

    try:
        logger.info(f"EDINET: {date}の開示書類一覧を取得中...")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        # レスポンス形式を確認
        results = data.get("results", [])
        metadata = data.get("metadata", {})

        total_count = metadata.get("resultset", {}).get("count", len(results))
        logger.info(f"EDINET: {date}の開示書類数: {total_count}件")

        # ローカルに保存
        _save_edinet_raw(results, date)

        return results

    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            raise Exception(
                f"認証エラー: EDINET APIキーが無効です。\n"
                f"config.yaml の edinet.api_key を確認してください。"
            )
        elif response.status_code == 404:
            logger.warning(f"警告: {date}の開示書類が見つかりませんでした（休日の可能性）。")
            return []
        raise Exception(f"APIエラー: EDINET書類一覧の取得に失敗しました。\n詳細: {e}")
    except requests.exceptions.ConnectionError:
        raise Exception(
            "接続エラー: EDINET APIに接続できませんでした。\n"
            "インターネット接続を確認してください。"
        )
    except Exception as e:
        logger.error(f"エラー: EDINET書類取得中に予期しないエラーが発生しました。\n詳細: {e}")
        return []


def _save_edinet_raw(data: list, date: str) -> Path:
    """
    EDINET開示データをJSONファイルに保存する（内部関数）。

    Args:
        data (list): 開示書類データ
        date (str): 日付（YYYY-MM-DD形式）

    Returns:
        Path: 保存ファイルパス
    """
    save_dir = Path(__file__).parent.parent / "data" / "raw" / "edinet"
    save_dir.mkdir(parents=True, exist_ok=True)

    # 日付のハイフンを除去してファイル名に使用
    date_str = date.replace("-", "")
    save_path = save_dir / f"{date_str}.json"

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.debug(f"EDINET生データを保存: {save_path}")
    return save_path


# ========================================
# 決算発表銘柄の抽出
# ========================================

def get_earnings_announcements(date: str = None) -> list:
    """
    指定日に決算発表を行った銘柄のリストを取得する。
    有価証券報告書・四半期報告書・臨時報告書から決算関連書類を抽出する。

    Args:
        date (str): 取得する日付（形式: "YYYY-MM-DD"）
                    Noneの場合は今日の日付を使用

    Returns:
        list: 決算発表銘柄のリスト。各要素はdict形式。
              主なキー: edinetCode, filerName, docTypeCode, docDescription,
                       submitDateTime, secCode（証券コード）
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"EDINET: {date}の決算発表銘柄を抽出中...")

    # 開示書類一覧を取得
    disclosures = get_disclosure_list(date)

    if not disclosures:
        logger.warning(f"{date}の開示書類が見つかりませんでした。")
        return []

    # ---- フィルタ設計（実測docTypeCodeに基づく）----
    # code=120: 有価証券報告書（年次決算）→ secCodeありの上場企業分をすべて対象
    # code=180: 臨時報告書 → docDescriptionが単に「臨時報告書」だけで
    #           業績キーワードを含まないことが多い（役員変更・合併等も同じdesc）。
    #           docDescriptionでは判別できないため「臨時報告書」を含む場合はすべて通す。
    #           （内容が不明なものはClaudeに判断させる。PDFなしの場合は後段でスキップ）
    # 有価証券届出書（組込方式・参照方式 etc）は増資・IPO目的で決算情報なし → スキップ
    SKIP_DESC_KEYWORDS = [
        "有価証券届出書", "確認書", "発行登録書", "訂正発行登録書", "変更報告書",
        "大量保有", "訂正有価証券報告書", "内国投資信託",
    ]

    earnings = []
    for doc in disclosures:
        doc_type = str(doc.get("docTypeCode", ""))

        # 対象コードに一致するものだけ処理
        if doc_type not in EARNINGS_DOC_TYPES:
            continue

        # 有価証券コード（証券コード）を取得 → なければ投資信託等なのでスキップ
        sec_code = doc.get("secCode", "")
        if not sec_code:
            continue

        doc_desc = doc.get("docDescription", "") or ""

        # 決算情報と関係ない書類種別はスキップ
        if any(kw in doc_desc for kw in SKIP_DESC_KEYWORDS):
            logger.debug(f"非決算書類をスキップ: {doc.get('filerName','')} [{doc_desc}]")
            continue

        earnings.append({
            "date": date,
            "secCode": sec_code,                          # 証券コード（例: "72030"）
            "edinetCode": doc.get("edinetCode", ""),      # EDINETコード
            "companyName": doc.get("filerName", ""),      # 会社名
            "docTypeCode": doc_type,                       # 書類種別コード
            "docDescription": doc_desc,                    # 書類概要
            "submitDateTime": doc.get("submitDateTime", ""),  # 提出日時
            "docID": doc.get("docID", "")                 # 書類ID
        })

    logger.info(f"決算発表銘柄: {len(earnings)}件を抽出しました。")

    # 証券コードでソート
    earnings.sort(key=lambda x: x.get("secCode", ""))

    return earnings


# ========================================
# 大量保有報告書の取得
# ========================================

def get_large_shareholding_reports(date: str = None) -> list:
    """
    指定日の大量保有報告書（5%ルール）を取得する。
    大量保有報告書は、特定の投資家が株式を5%以上取得した際に提出が義務付けられる。
    機関投資家の動向を掴む手がかりになる。

    Args:
        date (str): 取得する日付（形式: "YYYY-MM-DD"）
                    Noneの場合は今日の日付を使用

    Returns:
        list: 大量保有報告書のリスト。各要素はdict形式。
              主なキー: secCode, companyName, filerName（保有者名）,
                       docDescription, submitDateTime
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"EDINET: {date}の大量保有報告書を取得中...")

    # 開示書類一覧を取得（キャッシュを利用）
    disclosures = get_disclosure_list(date)

    if not disclosures:
        logger.warning(f"{date}の開示書類が見つかりませんでした。")
        return []

    # 大量保有報告書を絞り込む
    reports = []
    for doc in disclosures:
        doc_type = str(doc.get("docTypeCode", ""))

        if doc_type in LARGE_SHAREHOLDING_DOC_TYPES:
            sec_code = doc.get("secCode", "")
            if not sec_code:
                continue

            reports.append({
                "date": date,
                "secCode": sec_code,
                "edinetCode": doc.get("edinetCode", ""),
                "targetCompanyName": doc.get("filerName", ""),  # 保有対象企業名
                "docTypeCode": doc_type,
                "docDescription": doc.get("docDescription", ""),
                "submitDateTime": doc.get("submitDateTime", ""),
                "docID": doc.get("docID", "")
            })

    logger.info(f"大量保有報告書: {len(reports)}件を抽出しました。")
    return reports


# ========================================
# 開示情報ログへの保存
# ========================================

def save_disclosure_log(disclosures: list, date: str = None) -> None:
    """
    開示情報をmemory/disclosure_log.jsonに追記保存する。
    翌日の株価変動確認のために、開示日時と銘柄情報を記録しておく。

    Args:
        disclosures (list): 開示情報のリスト（get_earnings_announcements()の戻り値等）
        date (str): 開示日（形式: "YYYY-MM-DD"）。Noneの場合は今日の日付を使用
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # ログファイルのパス
    log_path = Path(__file__).parent.parent / "memory" / "disclosure_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 既存のログを読み込む
    existing_log = {"disclosures": [], "last_updated": None}
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                existing_log = json.load(f)
        except json.JSONDecodeError:
            logger.warning("警告: disclosure_log.jsonが破損しています。新規作成します。")
            existing_log = {"disclosures": [], "last_updated": None}

    # 新しい開示情報を追加
    for disclosure in disclosures:
        # 既に同じ開示IDが登録されていればスキップ（重複防止）
        existing_doc_ids = [d.get("docID") for d in existing_log["disclosures"]]
        if disclosure.get("docID") in existing_doc_ids:
            continue

        # 翌営業日の株価確認フラグを追加
        disclosure["priceChecked"] = False      # 翌日株価確認済みフラグ
        disclosure["nextDayReturn"] = None       # 翌日リターン（%）
        existing_log["disclosures"].append(disclosure)

    # 最終更新日時を記録
    existing_log["last_updated"] = datetime.now().isoformat()

    # ログファイルに書き込む
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(existing_log, f, ensure_ascii=False, indent=2)

    logger.info(f"開示ログを更新しました: {log_path}（合計{len(existing_log['disclosures'])}件）")


def get_recent_disclosures(days: int = 7) -> list:
    """
    過去N日分の開示情報を一括取得する。

    Args:
        days (int): 取得する日数（デフォルト: 7日）

    Returns:
        list: 開示情報のリスト
    """
    all_disclosures = []

    for i in range(days):
        # 過去の日付を計算（今日から遡る）
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")

        try:
            disclosures = get_disclosure_list(target_date)
            if disclosures:
                all_disclosures.extend(disclosures)
            # APIレート制限への配慮
            time.sleep(1)

        except Exception as e:
            logger.warning(f"{target_date}の開示情報取得をスキップしました: {e}")

    return all_disclosures


# ========================================
# メイン（単体テスト用）
# ========================================

if __name__ == "__main__":
    """
    このファイルを直接実行した場合のテスト処理
    使い方: python agents/edinet_fetcher.py
    """
    print("=" * 50)
    print("EDINET Fetcher - 動作テスト")
    print("=" * 50)

    # テスト対象の日付（直近の平日を使用）
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        # 開示書類一覧取得テスト
        print(f"\n[テスト1] 開示書類一覧取得（{today}）...")
        disclosures = get_disclosure_list(today)
        print(f"  ✓ 取得成功: {len(disclosures)}件")

        # 決算発表銘柄抽出テスト
        print(f"\n[テスト2] 決算発表銘柄抽出（{today}）...")
        earnings = get_earnings_announcements(today)
        print(f"  ✓ 抽出成功: {len(earnings)}件")
        for e in earnings[:3]:  # 最初の3件を表示
            print(f"    - {e.get('secCode', 'N/A')} {e.get('companyName', 'N/A')}: {e.get('docDescription', 'N/A')}")

        # 大量保有報告書取得テスト
        print(f"\n[テスト3] 大量保有報告書取得（{today}）...")
        reports = get_large_shareholding_reports(today)
        print(f"  ✓ 取得成功: {len(reports)}件")
        for r in reports[:3]:  # 最初の3件を表示
            print(f"    - {r.get('secCode', 'N/A')} {r.get('targetCompanyName', 'N/A')}")

        # 開示ログ保存テスト
        print(f"\n[テスト4] 開示ログ保存テスト...")
        save_disclosure_log(earnings, today)
        print("  ✓ 保存成功")

        print("\n全テスト完了！EDINET APIは正常に動作しています。")

    except Exception as e:
        print(f"\n✗ エラーが発生しました: {e}")
        print("\nconfig.yaml のEDINET APIキー設定を確認してください。")
