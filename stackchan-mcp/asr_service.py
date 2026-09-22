#!/usr/bin/env python3
"""HTTP-сервис распознавания речи для виртуального Stack-chan.

Держит модель GigaAM (sherpa-onnx) загруженной и принимает WAV на POST /transcribe.
Нарезка и декодирование переиспользуют transcribe_file.py из этого же каталога.

Запуск из каталога asr-tests:
    .venv\\Scripts\\python.exe asr_service.py
"""
import argparse
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from transcribe_file import MODELS, build_recognizer, load_audio, split_by_vad


class AsrEngine:
    def __init__(self, model_dir: Path, kind: str, threads: int) -> None:
        self.model_dir = model_dir
        self.kind = kind
        self.lock = threading.Lock()
        t0 = time.perf_counter()
        self.recognizer = build_recognizer(model_dir, threads, kind)
        print(f"модель загружена: {model_dir.name} ({kind}) за {time.perf_counter() - t0:.1f}s", flush=True)

    def transcribe(self, wav_bytes: bytes) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = Path(tmp.name)
        try:
            samples = load_audio(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        duration = len(samples) / 16000
        spans = split_by_vad(samples, 16000, 25.0, 5.0)
        parts = []
        with self.lock:
            for start, end in spans:
                stream = self.recognizer.create_stream()
                stream.accept_waveform(16000, samples[start:end])
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                if text:
                    parts.append(text)
        return {"text": " ".join(parts), "duration": duration, "language": "ru"}


class Handler(BaseHTTPRequestHandler):
    engine: AsrEngine = None

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": self.engine.model_dir.name})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        wav_bytes = self.rfile.read(length)
        try:
            t0 = time.perf_counter()
            result = self.engine.transcribe(wav_bytes)
        except Exception as exc:  # noqa: BLE001 - сервис должен отвечать JSON'ом
            print(f"ошибка распознавания: {exc}", flush=True)
            self._send(500, {"error": str(exc)})
            return
        elapsed = time.perf_counter() - t0
        print(
            f"[{time.strftime('%H:%M:%S')}] {result['duration']:.1f}s аудио -> "
            f"{elapsed:.1f}s | {result['text'][:80]}",
            flush=True,
        )
        self._send(200, result)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base class signature
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default="gigaam-v2-ctc", help="каталог модели в models/")
    ap.add_argument("--kind", default="ctc", choices=["ctc", "transducer"])
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    Handler.engine = AsrEngine(MODELS / args.model, args.kind, args.threads)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ASR готов: http://{args.host}:{args.port}/transcribe (Ctrl+C для выхода)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
