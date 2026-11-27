#!/usr/bin/env python3
"""MEMORY Five-Element sight receiver for LUCENT (Protocol TCAP-V-0044)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
PORT = int(os.getenv("LUCENT_PORT", "8790"))
STATE_DIR = Path(os.getenv("LUCENT_STATE_DIR", Path.home() / ".lucent")).expanduser().resolve()
LEDGER_PATH = STATE_DIR / "memory_sight_ledger.jsonl"

_lock = threading.Lock()
_ring: deque[dict[str, Any]] = deque(maxlen=8)
_prev_sha256 = "0" * 64


def _load_chain_head() -> str:
    if not LEDGER_PATH.exists():
        return "0" * 64
    last_hash = "0" * 64
    with LEDGER_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec.get("entry_sha256"), str):
                    last_hash = rec["entry_sha256"]
            except json.JSONDecodeError:
                continue
    return last_hash


def _validate_event(ev: Any) -> str | None:
    if not isinstance(ev, dict):
        return "payload must be a JSON object"
    if ev.get("organ") != "LUCENT":
        return "refused: organ must be 'LUCENT'"
    if ev.get("protocol") != "TCAP-V-0044":
        return "refused: protocol must be 'TCAP-V-0044'"
    if ev.get("execution_locality") != "on_device" or ev.get("left_machine") is not False:
        return "refused: provenance must declare execution_locality='on_device' and left_machine=false"
    h = ev.get("frame_sha256", "")
    if not isinstance(h, str) or len(h) != 64:
        return "refused: missing 64-char frame_sha256 digest"
    return None


def _append_record(ev: dict[str, Any]) -> dict[str, Any]:
    global _prev_sha256
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        body = {
            "received_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "stream": "fifth_stream_sight",
            "consumer": "MEMORY_five_element",
            "prev_sha256": _prev_sha256,
            "event": ev,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        record = {
            **body,
            "entry_sha256": hashlib.sha256(canonical).hexdigest(),
            "entry_sha512": hashlib.sha512(canonical).hexdigest(),
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _prev_sha256 = record["entry_sha256"]
        _ring.appendleft(record)
        return record


class LucentHandler(BaseHTTPRequestHandler):
    server_version = "LUCENT-MEMORY/1.0"

    def _send(self, status: int, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._send(200, {
                "status": "ok",
                "organ": "LUCENT",
                "consumer": "MEMORY",
                "protocol": "TCAP-V-0044",
                "execution_locality": "on_device",
                "ledger": str(LEDGER_PATH),
                "ring_count": len(_ring),
            })
        elif path == "/api/lucent/ring":
            with _lock:
                items = list(_ring)
            self._send(200, {"status": "ok", "count": len(items), "events": items})
        else:
            self._send(404, {"status": "not_found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/lucent":
            self._send(404, {"status": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 2 * 1024 * 1024:
            self._send(413, {"status": "refused", "error": "invalid payload size"})
            return
        try:
            ev = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"status": "bad_request", "error": str(exc)})
            return
        err = _validate_event(ev)
        if err:
            self._send(422, {"status": "refused", "error": err})
            return
        rec = _append_record(ev)
        self._send(201, {"status": "sealed", "entry_sha256": rec["entry_sha256"]})

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    global _prev_sha256
    _prev_sha256 = _load_chain_head()
    httpd = ThreadingHTTPServer((HOST, PORT), LucentHandler)
    print(f"LUCENT MEMORY receiver listening on http://{HOST}:{PORT}/lucent", flush=True)
    print(f"  ring:   http://{HOST}:{PORT}/api/lucent/ring", flush=True)
    print(f"  ledger: {LEDGER_PATH}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLUCENT MEMORY receiver stopped.", flush=True)


if __name__ == "__main__":
    main()
