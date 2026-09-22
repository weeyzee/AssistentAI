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
import queue
import shutil
import subprocess
import sys
import threading
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
# Если модель «задумалась» дольше этого — прерываем и пробуем ещё раз (обычный ответ ~4 с).
CLAUDE_TIMEOUT_SEC = float(os.environ.get("STACKCHAN_ASSISTANT_TIMEOUT", "45"))
# Ротация тёплого процесса: контекст не должен расти бесконечно, иначе ответы замедляются.
MAX_TURNS = int(os.environ.get("STACKCHAN_ASSISTANT_MAX_TURNS", "80"))
MAX_SESSION_AGE_SEC = float(os.environ.get("STACKCHAN_ASSISTANT_MAX_AGE", "10800"))
SYSTEM_PROMPT = (
    "Тебя зовут Марк — ты голосовой ассистент в телефоне-роботе Stack-chan. "
    "Человек говорит с тобой голосом через телефон. "
    "Твой ответ будет озвучен синтезом речи, поэтому отвечай коротко: 1-3 предложения, "
    "живой устной речью, без markdown, списков, смайликов и ссылок. Отвечай по-русски. "
    "Ещё ты умеешь проверять домашний сервер — неттоп «Aspire» (Linux): температуру, "
    "загрузку, docker-контейнеры, диски, память. Для этого вызови Bash ровно такой командой: "
    f"{NETTOP_SCRIPT} --section <раздел>, "
    "где <раздел> — одно из: all, temp, load, containers, disk, memory. "
    "Команда только читает; ничего не меняй на сервере. "
    "Ещё у тебя есть камера: если человек спрашивает, что ты видишь (или просит посмотреть), "
    "вызови инструмент mcp__stackchan__stackchan_see и опиши кадр. Кадр — актуальный на момент "
    "вопроса. Если инструмент вернул ошибку (камера выключена) — так и скажи."
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


class WarmClaude:
    """Один процесс claude держится на связи: ответы без перезапуска (~2-3с вместо ~5с).

    Протокол — stream-json: сообщения уходят строкой в stdin, ответ приходит событием
    type=result в stdout. Диалог живёт внутри процесса; session_id сохраняем, чтобы
    после перезапуска можно было продолжить разговор через --resume.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.session_id: str | None = None
        self.turns = 0
        self.started_at = 0.0
        self.last_activity = time.time()
        self.binary = shutil.which("claude")
        if self.binary is None:
            raise RuntimeError("claude не найден в PATH")

    def _command(self) -> list[str]:
        command = [
            self.binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            # Подключаем только наш MCP-сервер: камера (stackchan_see) по голосовой просьбе.
            "--strict-mcp-config",
            "--mcp-config",
            str(ROOT.parent / ".mcp.json"),
            # Голосовому ассистенту нужен быстрый ответ, а не агентные действия:
            # инструменты без явного разрешения автоматически отклоняются.
            "--permission-mode",
            "dontAsk",
            # Голосовой ответ должен быть быстрым — минимум рассуждений.
            "--effort",
            "low",
            # Разрешены только точные команды (без wildcard — суффикс мог бы унести shell-инъекцию).
            "--allowedTools",
            ",".join(
                [
                    *(f"Bash({NETTOP_SCRIPT} --section {section})" for section in NETTOP_SECTIONS),
                    "mcp__stackchan__stackchan_see",
                ]
            ),
        ]
        if self.session_id:
            command += ["--resume", self.session_id]
        else:
            command += ["--append-system-prompt", SYSTEM_PROMPT]
        return command

    def start(self, *, fresh_session: bool = False) -> None:
        self.stop()
        if fresh_session:
            self.session_id = None
        self.proc = subprocess.Popen(  # noqa: S603 - фиксированный бинарь, без shell
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",  # claude CLI пишет UTF-8, а не локальную кодировку Windows
            errors="replace",
            bufsize=1,
            cwd=str(ROOT.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Своя очередь на каждый процесс: старый читающий поток не подсунет чужие строки.
        self.lines = queue.Queue()
        proc, lines = self.proc, self.lines
        threading.Thread(target=self._read_lines, args=(proc, lines), daemon=True).start()
        self.turns = 0
        self.started_at = time.time()
        log("claude поднят (тёплый режим)" + ("" if self.session_id else " — новый диалог"))

    def _read_lines(self, proc: subprocess.Popen, lines: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # маркер конца именно этого процесса

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=5)  # дождаться, чтобы старый поток не мешал новому
            except subprocess.TimeoutExpired:
                pass

    def _should_rotate(self) -> bool:
        if self.session_id is None:
            return False
        idle = time.time() - self.last_activity
        return (
            self.turns >= MAX_TURNS
            or (time.time() - self.started_at) >= MAX_SESSION_AGE_SEC
            or idle > SESSION_TTL_SEC
        )

    def ask(self, text: str) -> str:
        for attempt in (1, 2):
            if self._should_rotate():
                log(f"диалог разросся ({self.turns} ходов) или долго молчал — начинаю новый")
                self.start(fresh_session=True)
            elif self.proc is None or self.proc.poll() is not None:
                self.start()
            proc, lines = self.proc, self.lines
            message = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except OSError:
                log(f"claude отвалился, поднимаю заново (попытка {attempt})")
                self.stop()
                continue

            started = time.perf_counter()
            deadline = time.monotonic() + CLAUDE_TIMEOUT_SEC
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log(f"модель молчит дольше {CLAUDE_TIMEOUT_SEC:.0f}с (попытка {attempt})")
                    self.stop()
                    break
                try:
                    line = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    log(f"claude завершился неожиданно (попытка {attempt})")
                    self.stop()
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "result":
                    continue
                if event.get("session_id"):
                    self.session_id = str(event["session_id"])
                if event.get("is_error"):
                    raise RuntimeError(f"claude error: {event.get('result')}")
                self.turns += 1
                self.last_activity = time.time()
                log(f"Claude ответил за {time.perf_counter() - started:.1f}s")
                return str(event.get("result", "")).strip()
        raise RuntimeError(f"модель не ответила за две попытки по {CLAUDE_TIMEOUT_SEC:.0f}с")


_warm = WarmClaude()


def ask_claude(text: str, session_id: str | None) -> tuple[str, str | None]:
    if session_id and session_id != _warm.session_id:
        _warm.session_id = session_id
    answer = _warm.ask(text)
    return answer, _warm.session_id


def speak(text: str, config, device: str = DEVICE) -> None:
    wav_path = generate_tts(text, "ru", config)
    validate_playback_wav(wav_path)
    url = audio_url(config.mac_ip, config.audio_serve_port, wav_path.name)
    result = http_post_json(f"{device}/play", {"voice_url": url})
    if not result.get("success"):
        log(f"play не принят: {result}")


def handle_recording(
    wav_bytes: bytes, config, session_id: str | None, device: str = DEVICE
) -> str | None:
    text = transcribe(wav_bytes)
    if not text:
        log("запись пустая после распознавания — пропускаю")
        return session_id
    log(f"услышал: «{text}»")
    try:
        answer, new_session = ask_claude(text, session_id)
    except Exception as exc:  # noqa: BLE001 - человек должен услышать хоть что-то
        log(f"ошибка Claude: {exc}")
        speak("Что-то я задумался. Повтори, пожалуйста.", config, device)
        return session_id
    if not answer:
        log("Claude вернул пустой ответ")
        return session_id
    log(f"отвечаю: «{answer[:120]}»")
    speak(answer, config, device)
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
            session_id = handle_recording(wav_bytes, config, session_id, args.device)
        except Exception as exc:  # noqa: BLE001 - фоновый цикл не должен падать
            log(f"ошибка обработки: {exc}")
        if args.once:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
