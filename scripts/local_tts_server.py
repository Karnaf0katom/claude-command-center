#!/usr/bin/env python3
"""Local neural voice (Kokoro, ONNX) behind a tiny loopback HTTP server.

Run with the venv python from ~/.ccc/local-tts (kokoro-onnx + soundfile
installed there; the dashboard itself stays stdlib-only). CCC starts it on
demand (ccc_server/free_runtime.py) and talks to it over 127.0.0.1 only.

    POST /speak {"text": "...", "voice": "af_heart"}  ->  audio/wav
    GET  /health                                       ->  {"ok": true}
"""
import io
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.join(os.path.expanduser("~"), ".ccc", "local-tts")
PORT = int(os.environ.get("CCC_LOCAL_TTS_PORT", "3019"))
MAX_CHARS = 2000

import soundfile as sf
from kokoro_onnx import Kokoro

_kokoro = Kokoro(os.path.join(HOME, "kokoro.int8.onnx"), os.path.join(HOME, "voices.bin"))
_voices = set(_kokoro.get_voices())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self):
        if self.path != "/speak":
            self._json(404, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length)) if 0 < length <= 64 * 1024 else {}
            text = str(req.get("text") or "").strip()[:MAX_CHARS]
            voice = req.get("voice") if req.get("voice") in _voices else "af_heart"
            if not text:
                self._json(400, {"ok": False})
                return
            samples, rate = _kokoro.create(text, voice=voice, speed=1.0, lang="en-gb" if voice.startswith("b") else "en-us")
            buf = io.BytesIO()
            sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
        except Exception as exc:  # keep serving; the caller falls back
            self._json(500, {"ok": False, "error": type(exc).__name__})
            return
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
