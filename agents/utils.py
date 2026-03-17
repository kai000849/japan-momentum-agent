"""
agents/utils.py
共通ユーティリティ関数

各エージェントで共通して使う処理をここに集約する。
"""

import os
from pathlib import Path


def get_anthropic_key() -> str:
    """
    ANTHROPIC_API_KEYを環境変数から取得する。
    GitHub ActionsではSecretsから自動注入される。
    ローカルではconfig.yamlから読み込む。

    Returns:
        str: APIキー。取得できない場合は空文字列。
    """
    # 環境変数を最優先（GitHub Actions）
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key

    # ローカル実行時はconfig.yamlから読む
    try:
        import yaml
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            key = config.get("anthropic", {}).get("api_key", "")
            if key and key != "YOUR_ANTHROPIC_API_KEY":
                return key
    except Exception:
        pass

    return ""
