#!/usr/bin/env python3
"""Проверка ElevenLabs: доступность, TTS и STT на русском (самые дешёвые модели).

Ключ: https://elevenlabs.io/app/settings/api-keys (профиль → API Keys).
Положить в переменную окружения ELEVENLABS_API_KEY или в stackchan-mcp/.env строкой
    ELEVENLABS_API_KEY="sk_..."

Запуск:
    .venv\\Scripts\\python.exe elevenlabs_check.py
    .venv\\Scripts\\python.exe elevenlabs_check.py --audio C:\\path\\to\\file.wav
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BASE = "https://api.elevenlabs.io/v1"
SITE = "https://elevenlabs.io/"
TTS_MODEL = "eleven_flash_v2_5"  # самый дешёвый/быстрый, русский поддерживает
STT_MODEL = "scribe_v1"  # единственная модель распознавания ElevenLabs
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel: на моделях v2.5 говорит по-русски
TEST_TEXT = "Привет! Это проверка синтеза и распознавания речи на русском языке."
AUDIO_DIR = Path(os.environ.get("TEMP", "/tmp")) / "elevenlabs_check"


def load_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def check_site() -> bool:
    print("1) Сайт https://elevenlabs.io/ …", end=" ", flush=True)
    started = time.perf_counter()
    try:
        resp = requests.get(
            SITE,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except requests.RequestException as exc:
        print(f"НЕДОСТУПЕН ({exc.__class__.__name__}: {exc})")
        print("   → похоже на проблему сети/DNS на этой машине (edge-tts падает по той же причине)")
        return False
    print(f"сеть ок: HTTP {resp.status_code} за {time.perf_counter() - started:.1f}с")
    return True


def check_account(key: str) -> str | None:
    print("2) API-ключ и аккаунт …", end=" ", flush=True)
    try:
        resp = requests.get(f"{BASE}/user", headers={"xi-api-key": key}, timeout=15)
    except requests.RequestException as exc:
        print(f"СЕТЬ: {exc}")
        return None
    if resp.status_code == 401:
        detail = ""
        try:
            detail = str(resp.json().get("detail", {}).get("message", ""))
        except ValueError:
            pass
        if "permission" in detail.lower():
            # Ключ валиден, но без права user_read — для TTS/STT это не помеха.
            print(f"ключ валиден, но без права на чтение профиля ({detail[:70]}…)")
            print("   → проверку аккаунта пропускаю (в UI ключа можно включить User → Read)")
            return DEFAULT_VOICE
        print("КЛЮЧ НЕ ПОДОШЁЛ (401) — проверь ELEVENLABS_API_KEY")
        return None
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"ответ не в формате JSON (HTTP {resp.status_code}, {len(resp.content)} байт) — проверки продолжаю")
        return DEFAULT_VOICE
    sub = data.get("subscription", {})
    used = sub.get("character_count", "?")
    limit = sub.get("character_limit", "?")
    print(f"ок | тариф: {sub.get('tier', '?')} | символов использовано: {used}/{limit}")
    return DEFAULT_VOICE


def pick_voice(key: str, wanted: str | None) -> str | None:
    if wanted:
        return wanted
    try:
        resp = requests.get(f"{BASE}/voices", headers={"xi-api-key": key}, timeout=15)
    except requests.RequestException as exc:
        print(f"   (список голосов недоступен: {exc}, беру {DEFAULT_VOICE})")
        return DEFAULT_VOICE
    if resp.status_code != 200:
        print(f"   (список голосов недоступен: HTTP {resp.status_code}, беру {DEFAULT_VOICE})")
        return DEFAULT_VOICE
    try:
        voices = resp.json().get("voices", [])
    except ValueError:
        voices = []
    if voices:
        first = voices[0]
        print(f"   голос: {first.get('name')} ({first.get('voice_id')})")
        return first.get("voice_id")
    return DEFAULT_VOICE


def tts(key: str, voice_id: str, text: str, out_path: Path, model: str) -> Path | None:
    print(f"3) TTS ({model}, голос {voice_id[:12]}…) …", end=" ", flush=True)
    started = time.perf_counter()
    try:
        resp = requests.post(
            f"{BASE}/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model},
            timeout=60,
        )
    except requests.RequestException as exc:
        print(f"СЕТЬ: {exc}")
        return None
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        print(f"ОШИБКА HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    out_path.write_bytes(resp.content)
    print(f"ок | {len(resp.content) / 1024:.0f} КБ за {elapsed:.1f}с → {out_path}")
    return out_path


def stt(key: str, audio_path: Path, model: str) -> str | None:
    print(f"4) STT ({model}) по файлу {audio_path.name} …", end=" ", flush=True)
    started = time.perf_counter()
    try:
        with audio_path.open("rb") as fh:
            resp = requests.post(
                f"{BASE}/speech-to-text",
                headers={"xi-api-key": key},
                data={"model_id": model, "language_code": "ru"},
                files={"file": (audio_path.name, fh, "audio/mpeg")},
                timeout=120,
            )
    except requests.RequestException as exc:
        print(f"СЕТЬ: {exc}")
        return None
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        print(f"ОШИБКА HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    text = str(resp.json().get("text", "")).strip()
    print(f"ок за {elapsed:.1f}с")
    print(f'   распознано: «{text}»')
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка ElevenLabs (TTS+STT, русский)")
    ap.add_argument("--audio", default="", help="свой файл для STT (по умолчанию — сгенерированный)")
    ap.add_argument("--voice-id", default="", help="голос ElevenLabs (по умолчанию — первый из аккаунта)")
    ap.add_argument("--text", default=TEST_TEXT, help="фраза для синтеза")
    ap.add_argument("--model", default=TTS_MODEL, help="модель синтеза")
    args = ap.parse_args()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    print("=== Проверка ElevenLabs ===\n")

    site_ok = check_site()

    key = load_key()
    if not key:
        print("\nКлюча нет. Возьми его здесь: https://elevenlabs.io/app/settings/api-keys")
        print('Добавь в stackchan-mcp/.env строку: ELEVENLABS_API_KEY="sk_..."')
        return 2 if not site_ok else 3

    voice_id = check_account(key)
    if voice_id is None:
        return 4
    voice_id = pick_voice(key, args.voice_id or None)

    generated = tts(key, voice_id, args.text, AUDIO_DIR / "tts_test.mp3", args.model)
    audio_path = Path(args.audio) if args.audio else generated
    if audio_path is None or not audio_path.exists():
        print("\nSTT пропущен: нет аудиофайла")
        return 5

    text = stt(key, audio_path, STT_MODEL)
    print("\n=== Итог ===")
    print(f"сайт: {'ок' if site_ok else 'проблема'} | TTS: {'ок' if generated else 'ошибка'} | STT: {'ок' if text else 'ошибка'}")
    if text:
        print(f"исходная фраза: «{args.text}»")
        print(f"обратно распознано: «{text}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
