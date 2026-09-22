#!/usr/bin/env python3
"""Read-only диагностика неттопа (Aspire, Linux) для голосового ассистента.

Все команды — фиксированный набор, произвольный shell со стороны LLM невозможен.
Использование:
    .venv\\Scripts\\python.exe nettop_status.py --section all
    .venv\\Scripts\\python.exe nettop_status.py --section containers
"""
from __future__ import annotations

import argparse
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "root@100.88.215.104"

# Только чтение; команды зафиксированы и не собираются из пользовательского ввода.
SECTIONS: dict[str, str] = {
    "all": (
        "echo '== uptime =='; uptime; "
        "echo '== temperature =='; for z in /sys/class/thermal/thermal_zone*; do "
        "printf '%s: ' \"$(cat $z/type 2>/dev/null)\"; "
        "awk '{printf \"%.1f C\\n\", $1/1000}' $z/temp 2>/dev/null; done; "
        "echo '== containers =='; docker ps --format '{{.Names}} | {{.Status}}'; "
        "echo '== disk =='; df -h / /home 2>/dev/null | tail -n +2; "
        "echo '== memory =='; free -h | head -2"
    ),
    "temp": (
        "for z in /sys/class/thermal/thermal_zone*; do "
        "printf '%s: ' \"$(cat $z/type 2>/dev/null)\"; "
        "awk '{printf \"%.1f C\\n\", $1/1000}' $z/temp 2>/dev/null; done; "
        "command -v sensors >/dev/null && sensors 2>/dev/null | head -20"
    ),
    "load": "uptime; echo; nproc --all",
    "containers": "docker ps --format '{{.Names}} | {{.Status}}'",
    "disk": "df -h | grep -vE 'tmpfs|loop'",
    "memory": "free -h",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only статус неттопа")
    ap.add_argument("--section", default="all", choices=sorted(SECTIONS))
    args = ap.parse_args()

    command = SECTIONS[args.section]
    try:
        completed = subprocess.run(  # noqa: S603 - фиксированные аргументы, без shell
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                HOST,
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"неттоп недоступен: {exc}")
        return 2

    if completed.returncode != 0:
        print(f"неттоп недоступен (ssh {completed.returncode}): {completed.stderr.strip()[:200]}")
        return 2
    print(completed.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
