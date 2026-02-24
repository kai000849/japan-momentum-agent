"""
scheduler_setup.py
朝8:00・夜20:00の自動通知スケジューラー設定スクリプト

Windows: タスクスケジューラに登録
Mac:     crontab に登録

使い方:
  python scheduler_setup.py          # セットアップ（OS自動判定）
  python scheduler_setup.py --remove # 登録削除
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

# このスクリプトがあるディレクトリ（プロジェクトルート）
PROJECT_DIR = Path(__file__).parent.resolve()
PYTHON = sys.executable  # 現在のPython実行ファイルのパス
NOTIFIER = PROJECT_DIR / "agents" / "notifier.py"


def _create_bat_file(report_type: str, webhook_url: str) -> Path:
    """
    タスクスケジューラから呼び出す .bat ファイルを作成する。
    Windowsの /TR オプションは261文字制限があるため、
    実行内容を .bat ファイルに書き出して回避する。
    """
    bat_dir = PROJECT_DIR / "scripts"
    bat_dir.mkdir(exist_ok=True)
    bat_path = bat_dir / f"run_{report_type}.bat"

    content = f"""@echo off
set DISCORD_WEBHOOK_URL={webhook_url}
"{PYTHON}" "{NOTIFIER}" --report {report_type}
"""
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(content)

    return bat_path


def setup_windows():
    """Windowsのタスクスケジューラに朝・夜の通知タスクを登録する。"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("⚠️  先に環境変数を設定してください:")
        print("   set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        print("   （設定後にこのスクリプトを再実行してください）")
        return False

    tasks = [
        ("JMA_MorningReport", "07:30", "morning"),
        ("JMA_EveningReport", "17:30", "evening"),
    ]

    for task_name, time_str, report_type in tasks:
        # .batファイルを作成してURLの長さ問題を回避
        bat_path = _create_bat_file(report_type, webhook_url)

        cmd = [
            "schtasks", "/Create", "/F",
            "/TN", task_name,
            "/TR", str(bat_path),
            "/SC", "DAILY",
            "/ST", time_str,
            "/RL", "HIGHEST"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ タスク登録成功: {task_name} ({time_str})")
        else:
            print(f"❌ タスク登録失敗: {task_name}")
            print(f"   エラー: {result.stderr}")
            return False

    print("\n登録されたタスクの確認:")
    print("  タスクスケジューラ → 'JMA_MorningReport' / 'JMA_EveningReport'")
    return True


def remove_windows():
    """Windowsのタスクスケジューラからタスクを削除する。"""
    for task_name in ["JMA_MorningReport", "JMA_EveningReport"]:
        result = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", task_name],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ タスク削除成功: {task_name}")
        else:
            print(f"⚠️  タスクが見つかりません（既に削除済み？）: {task_name}")


def setup_mac():
    """MacのcrontabにDiscord通知ジョブを追加する。"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("⚠️  先に環境変数を設定してください:")
        print("   export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        print("   （~/.zshrc または ~/.bash_profile に追記することをおすすめします）")
        return False

    # 既存のcrontabを取得
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    # JMAタスクが既に登録されていれば削除してから追加
    lines = [l for l in existing.splitlines() if "JMA_" not in l]

    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    # cron形式で追加（毎日 8:00 と 20:00）
    new_lines = [
        f'# JMA_MorningReport',
        f'0 8 * * * DISCORD_WEBHOOK_URL="{webhook_url}" {PYTHON} {NOTIFIER} --report morning >> {log_dir}/morning.log 2>&1',
        f'# JMA_EveningReport',
        f'0 20 * * * DISCORD_WEBHOOK_URL="{webhook_url}" {PYTHON} {NOTIFIER} --report evening >> {log_dir}/evening.log 2>&1',
    ]

    new_crontab = "\n".join(lines + new_lines) + "\n"

    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if proc.returncode == 0:
        print("✅ crontab 登録成功！")
        print("   朝8:00 と 夜20:00 に自動送信されます")
        print(f"   ログ出力先: {log_dir}/")
        return True
    else:
        print(f"❌ crontab 登録失敗: {proc.stderr}")
        return False


def remove_mac():
    """MacのcrontabからJMAタスクを削除する。"""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        print("crontabにエントリがありません")
        return

    lines = [l for l in result.stdout.splitlines() if "JMA_" not in l]
    new_crontab = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True)
    print("✅ crontabからJMAタスクを削除しました")


def main():
    os_name = platform.system()
    is_remove = "--remove" in sys.argv

    print("=" * 50)
    print("Japan Momentum Agent - スケジューラーセットアップ")
    print(f"OS: {os_name}")
    print("=" * 50)

    if os_name == "Windows":
        if is_remove:
            remove_windows()
        else:
            setup_windows()
    elif os_name == "Darwin":  # Mac
        if is_remove:
            remove_mac()
        else:
            setup_mac()
    else:
        print("Linux環境の場合はcronを手動で設定してください:")
        print(f"  0 8  * * * DISCORD_WEBHOOK_URL='URL' {PYTHON} {NOTIFIER} --report morning")
        print(f"  0 20 * * * DISCORD_WEBHOOK_URL='URL' {PYTHON} {NOTIFIER} --report evening")


if __name__ == "__main__":
    main()
