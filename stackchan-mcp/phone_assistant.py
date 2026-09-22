#!/usr/bin/env python3
"""Фоновый голосовой ассистент: запись с телефона -> GigaAM -> Claude -> озвучка в телефон.

Живёт на ПК отдельным процессом: пока он запущен, диалог работает без консоли Claude Code.
Запуск:  .venv\\Scripts\\python.exe phone_assistant.py
Разово (для проверки):  .venv\\Scripts\\python.exe phone_assistant.py --once
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp_server.audio_processing import generate_tts, validate_playback_wav  # noqa: E402
from mcp_server.audio_server import AUDIO_DIR, audio_url, start_audio_server  # noqa: E402
from mcp_server.stackchan_config import load_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEVICE = "http://127.0.0.1:8090"
SESSION_FILE = Path(__file__).resolve().parent / ".assistant_session.json"
# Диалог помним 2 часа; старые сессии раздуваются и заметно замедляют ответы.
SESSION_TTL_SEC = float(os.environ.get("STACKCHAN_ASSISTANT_SESSION_TTL", "7200"))
# Единственная разрешённая команда: read-only диагностика неттопа (фиксированный набор).
NETTOP_SCRIPT = f"{sys.executable} {ROOT / 'nettop_status.py'}"
NETTOP_SECTIONS = ("all", "temp", "load", "containers", "disk", "memory")
SYSTEM_PROMPT = (
    "Тебя зовут Марк — ты голосовой ассистент в телефоне-роботе Stack-chan. "
    "Человек говорит с тобой голосом через телефон. "
    "Твой ответ будет озвучен синтезом речи, поэтому отвечай коротко: 1-3 предложения, "
    "живой устной речью, без markdown, списков, смайликов и ссылок. Отвечай по-русски. "
    "Ещё ты умеешь проверять домашний сервер — неттоп «Aspire» (Linux): температуру, "
    "загрузку, docker-контейнеры, диски, память. Для этого вызови Bash ровно такой командой: "
    f"{NETTOP_SCRIPT} --section <раздел>, "
    "где <раздел> — одно из: all, temp, load, containers, disk, memory. "
    "Команда только читает; ничего не меняй на сервере."
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def http_get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - только локальный адрес
        return resp.read()


def http_post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - только локальный адрес
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def transcribe(wav_bytes: bytes) -> str:
    request = urllib.request.Request(  # noqa: S310 - только локальный адрес
        "http://127.0.0.1:8765/transcribe",
        data=wav_bytes,
        headers={"Content-Type": "audio/wav"},
    )
    with urllib.request.urlopen(request, timeout=120) as resp:
        return str(json.loads(resp.read()).get("text", "")).strip()


def load_session_id() -> str | None:
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(data.get("updated", 0)) > SESSION_TTL_SEC:
        log("старый диалог устарел — начинаю новый")
        return None
    session_id = str(data.get("session_id", ""))
    return session_id or None


def save_session_id(session_id: str) -> None:
    SESSION_FILE.write_text(
        json.dumps({"session_id": session_id, "updated": time.time()}), encoding="utf-8"
    )


def ask_claude(text: str, session_id: str | None) -> tuple[str, str | None]:
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise RuntimeError("claude не найден в PATH")
    command = [
        claude_bin,
        "-p",
        text,
        "--output-format",
        "json",
        "--strict-mcp-config",
        # Голосовому ассистенту нужен быстрый ответ, а не агентные действия:
        # инструменты без явного разрешения автоматически отклоняются.
        "--permission-mode",
        "dontAsk",
        # Голосовой ответ должен быть быстрым — минимум рассуждений.
        "--effort",
        "low",
        # Разрешены только точные команды (без wildcard — суффикс мог бы унести shell-инъекцию).
        "--allowedTools",
        ",".join(f"Bash({NETTOP_SCRIPT} --section {section})" for section in NETTOP_SECTIONS),
    ]
    if session_id:
        command += ["--resume", session_id]
    else:
        command += ["--append-system-prompt", SYSTEM_PROMPT]
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - фиксированный бинарь, без shell
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",  # claude CLI пишет UTF-8, а не локальную кодировку Windows
        errors="replace",
        timeout=240,
        cwd=str(ROOT.parent),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude exited {completed.returncode}: {completed.stderr[-300:]}")
    payload = json.loads(completed.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude error: {payload.get('result')}")
    answer = str(payload.get("result", "")).strip()
    log(f"Claude ответил за {time.perf_counter() - started:.1f}s")
    return answer, payload.get("session_id")


def speak(text: str, config) -> None:
    wav_path = generate_tts(text, "ru", config)
    validate_playback_wav(wav_path)
    url = audio_url(config.mac_ip, config.audio_serve_port, wav_path.name)
    result = http_post_json(f"{DEVICE}/play", {"voice_url": url})
    if not result.get("success"):
        log(f"play не принят: {result}")


def handle_recording(wav_bytes: bytes, config, session_id: str | None) -> str | None:
    text = transcribe(wav_bytes)
    if not text:
        log("запись пустая после распознавания — пропускаю")
        return session_id
    log(f"услышал: «{text}»")
    answer, new_session = ask_claude(text, session_id)
    if not answer:
        log("Claude вернул пустой ответ")
        return session_id
    log(f"отвечаю: «{answer[:120]}»")
    speak(answer, config)
    if new_session:
        save_session_id(new_session)
        return new_session
    return session_id


def main() -> int:
    ap = argparse.ArgumentParser(description="Голосовой ассистент для телефона")
    ap.add_argument("--once", action="store_true", help="обработать одну запись и выйти")
    ap.add_argument("--reset", action="store_true", help="начать новый диалог с Claude")
    ap.add_argument("--device", default=DEVICE)
    args = ap.parse_args()

    if args.reset:
        SESSION_FILE.unlink(missing_ok=True)

    config = load_config()
    start_audio_server(config.audio_serve_port)
    session_id = load_session_id()
    log(f"ассистент запущен | диалог: {session_id or 'новый'} | озвучка: {config.tts_engine}")

    while True:
        try:
            status = json.loads(http_get(f"{args.device}/audio/status", timeout=5))
        except OSError as exc:
            log(f"устройство недоступно: {exc}")
            time.sleep(2)
            continue
        if not status.get("ready"):
            time.sleep(1)
            continue
        try:
            wav_bytes = http_get(f"{args.device}/audio", timeout=30)
        except OSError as exc:
            log(f"не смог забрать запись: {exc}")
            time.sleep(1)
            continue
        try:
            session_id = handle_recording(wav_bytes, config, session_id)
        except Exception as exc:  # noqa: BLE001 - фоновый цикл не должен падать
            log(f"ошибка обработки: {exc}")
        if args.once:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
