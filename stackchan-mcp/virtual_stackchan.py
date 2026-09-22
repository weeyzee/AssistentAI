#!/usr/bin/env python3
"""Виртуальный Stack-chan: эмулятор HTTP API прошивки + веб-страница для телефона.

MCP-сервер общается с этим процессом как с настоящим роботом (device API),
а роль железа — динамик, микрофон, камера, мордочки, тач-полоса — играет
браузер телефона, открытый на https-странице /.

Запуск:  .venv\\Scripts\\python.exe virtual_stackchan.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
FACES_DIR = ROOT / "faces"
UI_FILE = ROOT / "virtual_ui.html"
STATE_DIR = Path(tempfile.gettempdir()) / "virtual_stackchan"
OUT_DIR = STATE_DIR / "out"
CERT_FILE = STATE_DIR / "cert.pem"
KEY_FILE = STATE_DIR / "key.pem"

VALID_FACES = ("calm", "thinking", "happy", "sleepy", "shy", "smug", "pouty")

# /play качает только с локального аудио-сервера MCP (защита от SSRF)
ALLOWED_PLAY_HOSTS = {"127.0.0.1", "localhost"} | {os.environ.get("MAC_IP", "").strip()}


# ---------------------------------------------------------------- state


class State:
    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.events: list[dict] = []
        self.last_id = 0
        self.face = "calm"
        self.pet_count = 0
        self.rec_requests = 0
        self.recording: tuple[str, bytes, str] | None = None
        self.frame: bytes | None = None
        self.playing = False
        self.started_ms = 0
        self.current_bytes = 0
        self.play_name: str | None = None
        self.pcm: dict[str, bytearray] = {}

    def push(self, **event: object) -> int:
        with self.lock:
            self.last_id += 1
            event["id"] = self.last_id
            self.events.append(event)
            if len(self.events) > 500:
                del self.events[:100]
            self.lock.notify_all()
            return self.last_id

    def wait_events(self, since: int, timeout: float = 20.0) -> tuple[list[dict], int]:
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                pending = [e for e in self.events if e["id"] > since]
                if pending:
                    return pending, self.last_id
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], self.last_id
                self.lock.wait(remaining)

    def enqueue_playback(self, name: str, size: int) -> None:
        with self.lock:
            self.playing = True
            self.started_ms = int(time.time() * 1000)
            self.current_bytes = size
            self.play_name = name
        self.push(type="play", url=f"/audio_out/{name}", name=name)
        log(f"воспроизведение: {name} ({size} байт)")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------- helpers


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def blob_to_wav16k(blob: bytes) -> bytes:
    """Любой формат из браузера (mp4/aac, webm/opus, wav) -> 16 кГц моно WAV."""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        if blob[:4] == b"RIFF":
            return blob
        raise RuntimeError("ffmpeg не найден в PATH, а браузер прислал не-WAV звук")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp_in:
        tmp_in.write(blob)
        in_path = Path(tmp_in.name)
    out_path = in_path.with_suffix(".wav")
    try:
        proc = subprocess.run(  # noqa: S603 - фиксированный бинарь, без shell
            [ffmpeg, "-y", "-i", str(in_path), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(out_path)],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:])
        return out_path.read_bytes()
    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def face_file(name: str) -> Path | None:
    if name not in VALID_FACES:
        return None
    hits = sorted(FACES_DIR.glob(f"*_{name}_320x240.png"))
    return hits[0] if hits else None


def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def find_openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def tailscale_ip() -> str | None:
    exe = shutil.which("tailscale")
    if exe is None:
        candidate = r"C:\Program Files\Tailscale\tailscale.exe"
        if Path(candidate).exists():
            exe = candidate
    if exe is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - фиксированный путь tailscale, без shell
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        ip = line.strip()
        if ip:
            return ip
    return None


def ensure_cert() -> bool:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    openssl = find_openssl()
    if openssl is None:
        return False
    san = [f"IP:{lan_ip()}", "IP:127.0.0.1", "DNS:localhost"]
    ts_ip = tailscale_ip()
    if ts_ip:
        san.append(f"IP:{ts_ip}")
    try:
        subprocess.run(  # noqa: S603 - фиксированный путь openssl, без shell
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                "-days", "825", "-nodes",
                "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
                "-subj", "/CN=virtual-stackchan",
                "-addext", f"subjectAltName={','.join(san)}",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"не удалось создать сертификат: {exc}")
        return False
    log(f"самоподписанный сертификат создан: {', '.join(san)}")
    return True


# ---------------------------------------------------------------- device API (порт робота)


class DeviceHandler(BaseHTTPRequestHandler):
    state: State = None  # type: ignore[assignment]
    server_version = "VirtualStackchan/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # -- GET ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        state = self.state
        try:
            if path == "/audio/status":
                with state.lock:
                    rec = state.recording
                    return self._json(
                        {
                            "ready": rec is not None,
                            "mode": "mcp",
                            "source": rec[2] if rec else "none",
                        }
                    )
            if path == "/audio":
                with state.lock:
                    rec = state.recording
                    state.recording = None
                if rec is None:
                    return self._json({"success": False, "error": "no recording"}, 404)
                return self._bytes(rec[1], "audio/wav")
            if path == "/playback/status":
                with state.lock:
                    playing = state.playing
                    started_ms = state.started_ms
                    current_bytes = state.current_bytes
                return self._json(
                    {
                        "playing": playing,
                        "started_ms": started_ms,
                        "kind": "wav" if started_ms else "idle",
                        "current_bytes": current_bytes,
                        "deadline_ms": 0,
                        "queued_pcm_segments": 0,
                        "queued_pcm_bytes": 0,
                        "audio_queue_depth": 0,
                        "download_queue_depth": 0,
                        "download_in_flight": False,
                        "download_age_ms": 0,
                        "download_watchdog_ms": 15000,
                        "mic_state": "idle",
                        "gesture": "none",
                        "free_heap": 180000,
                        "free_psram": 4000000,
                        "udp_audio_session": "",
                        "udp_audio_active": False,
                    }
                )
            if path == "/face":
                with state.lock:
                    return self._json({"face": state.face})
            if path == "/env":
                tick = time.time() / 30.0
                return self._json(
                    {
                        "success": True,
                        "temperature": round(24.0 + 1.5 * _wave(tick), 1),
                        "humidity": round(45.0 + 5.0 * _wave(tick + 1.7), 1),
                        "pressure": round(1013.0 + 1.0 * _wave(tick + 3.1), 1),
                        "sensors": {"sht31": True, "qmp6988": True},
                    }
                )
            if path == "/servo/status":
                return self._json({"success": True, "yaw": 0.0, "pitch": 45.0, "torque": True})
            if path == "/touch/status":
                with state.lock:
                    pets = state.pet_count
                    requests = state.rec_requests
                return self._json(
                    {
                        "available": True,
                        "suspended": False,
                        "front": 0,
                        "middle": 0,
                        "back": 0,
                        "last_event": "none",
                        "touch_pet_count": pets,
                        "recording_request_count": requests,
                        "recording_failure_count": 0,
                        "resume_failure_count": 0,
                    }
                )
            if path == "/snapshot":
                with state.lock:
                    frame = state.frame
                if frame is None:
                    return self._json(
                        {
                            "success": False,
                            "error": "кадров нет: включи камеру на веб-странице телефона",
                        },
                        503,
                    )
                state.push(type="image", url="/web/frame.jpg")
                return self._bytes(frame, "image/jpeg")
            if path == "/env/debug":
                return self._json({"success": False, "error": "no env sensor detected"})
            if path == "/":
                return self._bytes(
                    b"virtual stack-chan device api: /audio/status /audio /playback/status "
                    b"/face /env /servo/status /touch/status /snapshot",
                    "text/plain; charset=utf-8",
                )
            return self._json({"success": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - эмулятор не должен падать от запроса
            log(f"device GET {path}: {exc}")
            return self._json({"success": False, "error": str(exc)}, 500)

    # -- POST --------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        state = self.state
        try:
            if path == "/play":
                return self._handle_play()
            if path == "/play/pcm":
                return self._handle_play_pcm(parsed.query)
            if path == "/mode":
                with state.lock:
                    state.recording = None
                return self._json({"success": True, "mode": "mcp"})
            if path == "/face":
                body = self._json_body()
                name = str(body.get("face", ""))
                if name not in VALID_FACES:
                    return self._json({"success": False, "error": f"unknown face {name!r}"}, 400)
                with state.lock:
                    state.face = name
                state.push(type="face", face=name)
                return self._json({"success": True, "face": name})
            if path == "/move":
                body = self._json_body()
                state.push(type="move", x=body.get("x", 0), y=body.get("y", 0))
                return self._json({"success": True, "x": body.get("x", 0), "y": body.get("y", 0)})
            if path in {"/nod", "/shake", "/home"}:
                state.push(type=path[1:])
                return self._json({"success": True})
            return self._json({"success": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            log(f"device POST {path}: {exc}")
            return self._json({"success": False, "error": str(exc)}, 500)

    def _handle_play(self) -> None:
        body = self._json_body()
        url = str(body.get("voice_url", ""))
        if not url:
            return self._json({"success": False, "error": "voice_url required"}, 400)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_PLAY_HOSTS:
            return self._json(
                {"success": False, "error": "voice_url must point at the local audio server"},
                400,
            )
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - url задаёт хост-сервер MCP
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            return self._json({"success": False, "error": f"download failed: {exc}"}, 502)
        name = f"say_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.wav"
        (OUT_DIR / name).write_bytes(data)
        self.state.enqueue_playback(name, len(data))
        return self._json({"success": True, "queued": True, "bytes": len(data)})

    def _handle_play_pcm(self, query: str) -> None:
        params = parse_qs(query)
        session = params.get("session", ["default"])[0] or "default"
        final = params.get("final", ["0"])[0] in {"1", "true", "True"}
        data = self._body()
        state = self.state
        if len(data) % 2 != 0:
            return self._json({"success": False, "error": "invalid PCM size"}, 400)
        with state.lock:
            buffer = state.pcm.setdefault(session, bytearray())
            buffer.extend(data)
        if not final:
            return self._json({"success": True, "staged": True, "session": session, "bytes": len(data)})
        with state.lock:
            pcm = bytes(state.pcm.pop(session, bytearray()))
        wav_bytes = pcm_to_wav(pcm)
        name = f"pcm_{int(time.time() * 1000)}.wav"
        (OUT_DIR / name).write_bytes(wav_bytes)
        state.enqueue_playback(name, len(wav_bytes))
        return self._json({"success": True, "staged": False, "session": session, "segments": 1})


def _wave(t: float) -> float:
    import math

    return math.sin(t)


# ---------------------------------------------------------------- web API (телефон)


class WebHandler(BaseHTTPRequestHandler):
    state: State = None  # type: ignore[assignment]
    server_version = "VirtualStackchanWeb/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        state = self.state
        try:
            if path in {"/", "/index.html"}:
                html = UI_FILE.read_bytes()
                return self._bytes(html, "text/html; charset=utf-8")
            if path == "/face_engine.js":
                script = (ROOT / "face_engine.js").read_bytes()
                return self._bytes(script, "application/javascript; charset=utf-8")
            if path.startswith("/faces/"):
                name = path.rsplit("/", 1)[-1].removesuffix(".png")
                target = face_file(name) or face_file("calm")
                if target is None:
                    return self._json({"error": "no faces"}, 404)
                return self._bytes(target.read_bytes(), "image/png")
            if path.startswith("/audio_out/"):
                name = path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[\w.-]+", name):
                    return self._json({"error": "bad name"}, 400)
                target = OUT_DIR / name
                if not target.exists():
                    return self._json({"error": "not found"}, 404)
                return self._bytes(target.read_bytes(), "audio/wav")
            if path == "/web/frame.jpg":
                with state.lock:
                    frame = state.frame
                if frame is None:
                    return self._json({"error": "no frame"}, 404)
                return self._bytes(frame, "image/jpeg")
            if path == "/web/poll":
                since = int(parse_qs(parsed.query).get("since", ["0"])[0])
                events, last = state.wait_events(since)
                with state.lock:
                    face = state.face
                return self._json({"events": events, "next": last, "face": face})
            if path == "/web/state":
                with state.lock:
                    return self._json(
                        {
                            "face": state.face,
                            "playing": state.playing,
                            "pet_count": state.pet_count,
                            "has_frame": state.frame is not None,
                        }
                    )
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            log(f"web GET {path}: {exc}")
            return self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        state = self.state
        try:
            if path == "/web/mic":
                blob = self._body()
                if not blob:
                    return self._json({"ok": False, "error": "empty"}, 400)
                try:
                    wav_bytes = blob_to_wav16k(blob)
                except Exception as exc:  # noqa: BLE001
                    log(f"mic: конвертация не удалась: {exc}")
                    return self._json({"ok": False, "error": str(exc)}, 500)
                name = f"rec_{int(time.time() * 1000)}.wav"
                with state.lock:
                    state.recording = (name, wav_bytes, "touch")
                state.push(type="recording", ok=True)
                log(f"запись с телефона: {name} ({len(wav_bytes)} байт)")
                return self._json({"ok": True, "bytes": len(wav_bytes)})
            if path == "/web/frame":
                frame = self._body()
                if not frame:
                    return self._json({"ok": False, "error": "empty"}, 400)
                with state.lock:
                    state.frame = frame
                return self._json({"ok": True, "bytes": len(frame)})
            if path == "/web/played":
                with state.lock:
                    state.playing = False
                    state.play_name = None
                return self._json({"ok": True})
            if path == "/web/pet":
                with state.lock:
                    state.pet_count += 1
                    count = state.pet_count
                state.push(type="pet", count=count)
                log(f"погладили! (всего {count})")
                return self._json({"ok": True, "count": count})
            if path == "/web/tap":
                with state.lock:
                    state.rec_requests += 1
                state.push(type="tap")
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            log(f"web POST {path}: {exc}")
            return self._json({"error": str(exc)}, 500)


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Виртуальный Stack-chan")
    ap.add_argument("--device-port", type=int, default=8090, help="порт HTTP API робота")
    ap.add_argument("--web-port", type=int, default=8443, help="порт страницы для телефона")
    ap.add_argument("--no-tls", action="store_true", help="отдать страницу по http (микрофон в Safari не заработает)")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    state = State()
    DeviceHandler.state = state
    WebHandler.state = state

    if _ffmpeg() is None:
        log("ВНИМАНИЕ: ffmpeg не найден — запись с телефона работать не будет")

    # Device API нужен только локальным процессам (MCP-сервер и ассистент) — наружу не слушаем.
    device_server = ThreadingHTTPServer(("127.0.0.1", args.device_port), DeviceHandler)
    threading.Thread(target=device_server.serve_forever, daemon=True).start()
    log(f"device API: http://127.0.0.1:{args.device_port} (для MCP-сервера)")

    web_server = ThreadingHTTPServer(("0.0.0.0", args.web_port), WebHandler)  # noqa: S104
    scheme = "http"
    if not args.no_tls:
        if ensure_cert():
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
            web_server.socket = context.wrap_socket(web_server.socket, server_side=True)
            scheme = "https"
        else:
            log("openssl не найден — страница будет по http (микрофон/камера в Safari заблокируются)")

    ip = lan_ip()
    log(f"страница для телефона: {scheme}://{ip}:{args.web_port}/")
    log("открой этот адрес в Safari на iPhone (и один раз прими предупреждение о сертификате)")
    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
