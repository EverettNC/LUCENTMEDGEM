#!/usr/bin/env python3
"""Live Camera -> LUCENT Cornea (C++) -> MedGemma (:8791) -> MEMORY Ledger (:8790).

Zero external dependencies (Python stdlib only). Strict loopback enforcement.
Keys while running:
  v + Enter  ->  instant S.4-verified camera look through MedGemma
  s + Enter  ->  seat room baseline (requires >= 3 locked frames)
  q + Enter  ->  clean shutdown
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LUCENT_BIN = HERE / "lucent_live"
DEFAULT_LLM = "http://127.0.0.1:8791"
DEFAULT_MEMORY = "http://127.0.0.1:8790/lucent"
DEFAULT_FRAMES = Path.home() / ".lucent" / "frames"


def _require_loopback(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"refused non-loopback endpoint '{url}'")
    return url.rstrip("/")


def _http_json(
    url: str,
    body: dict[str, Any] | None = None,
    *,
    api_key: str = "",
    timeout: int = 180,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:500]}


def _query_model_id(llm_url: str, api_key: str) -> str | None:
    try:
        st, data = _http_json(f"{llm_url}/v1/models", api_key=api_key, timeout=5)
        if st == 200 and isinstance(data.get("data"), list) and data["data"]:
            return data["data"][0].get("id")
    except (OSError, urllib.error.URLError):
        pass
    return None


def _analyze_event(
    ev: dict[str, Any],
    *,
    llm_url: str,
    memory_url: str,
    prompt: str,
    max_tokens: int,
    api_key: str,
) -> None:
    frame_path = Path(ev.get("frame_path", ""))
    if not frame_path.is_file():
        print(json.dumps({"organ": "LUCENT_MED", "status": "refused",
                          "reason": "verified frame file missing on disk"}), flush=True)
        return

    jpeg_bytes = frame_path.read_bytes()
    sha256_disk = hashlib.sha256(jpeg_bytes).hexdigest()
    sha512_disk = hashlib.sha512(jpeg_bytes).hexdigest()

    # Chain-of-custody check: file on disk must match the hash Cornea stamped.
    if ev.get("frame_sha256") and ev["frame_sha256"] != sha256_disk:
        print(json.dumps({"organ": "LUCENT_MED", "status": "refused",
                          "reason": "frame SHA-256 mismatch between Cornea and disk"}), flush=True)
        return

    reported_model = _query_model_id(llm_url, api_key)
    b64_img = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "model": reported_model or "local-medgemma",
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are LUCENT, the on-device cornea of The Christman AI Project "
                    "(Protocol TCAP-V-0044). Describe ONLY what is directly visible in this live "
                    "camera frame. If unclear or dark, say so plainly and stop. Never guess."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    {"type": "text", "text": prompt},
                ],
            },
        ],
    }

    t0 = time.monotonic()
    try:
        status, reply = _http_json(
            f"{llm_url}/v1/chat/completions", payload, api_key=api_key, timeout=180
        )
    except (OSError, urllib.error.URLError) as exc:
        ev["medgemma"] = {"status": "error", "error": f"The sight-model failed ({exc})"}
        status = 0
        reply = {}

    elapsed = round(time.monotonic() - t0, 2)
    if status == 200:
        choice = (reply.get("choices") or [{}])[0]
        obs = (choice.get("message") or {}).get("content", "").strip()
        ev["medgemma"] = {
            "status": "ok",
            "server_reported_model": reply.get("model") or reported_model,
            "elapsed_sec": elapsed,
            "observation": obs,
        }
    elif status != 0:
        ev["medgemma"] = {
            "status": "error",
            "http_status": status,
            "error": f"The sight-model failed ({status})",
        }

    ev["frame_sha512"] = sha512_disk

    # Post sealed SightEvent + MedGemma observation to MEMORY receiver (:8790).
    if memory_url:
        try:
            m_status, m_reply = _http_json(memory_url, ev, timeout=5)
            ev["memory_ledger"] = {"http_status": m_status, **m_reply}
        except (OSError, urllib.error.URLError) as exc:
            ev["memory_ledger"] = {"status": "unreachable", "detail": str(exc)}

    print(json.dumps(ev, indent=2, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="LUCENT Live Camera -> MedGemma -> MEMORY Loop")
    parser.add_argument("--llm", default=DEFAULT_LLM, help="Local llama-server URL (loopback only)")
    parser.add_argument("--memory", default=DEFAULT_MEMORY, help="MEMORY receiver URL (or '' to disable)")
    parser.add_argument("--frames-dir", default=str(DEFAULT_FRAMES))
    parser.add_argument("--prompt", default="Briefly describe what is visible in this live camera frame.")
    parser.add_argument("--cooldown", type=float, default=8.0, help="Seconds between auto motion looks")
    parser.add_argument("--manual-only", action="store_true", help="Only call MedGemma on 'v + Enter'")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--api-key", default=os.getenv("LUCENT_API_KEY", ""))
    args = parser.parse_args()

    try:
        llm_url = _require_loopback(args.llm)
        memory_url = _require_loopback(args.memory) if args.memory.strip() else ""
    except ValueError as exc:
        print(json.dumps({"organ": "LUCENT", "status": "refused", "error": str(exc)}), file=sys.stderr)
        return 2

    if not LUCENT_BIN.is_file():
        print(f"Missing binary: {LUCENT_BIN}", file=sys.stderr)
        return 1

    proc = subprocess.Popen(
        [str(LUCENT_BIN), "--save-frames", args.frames_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stop_flag = threading.Event()

    def _shutdown(_sig: int, _frame: Any) -> None:
        stop_flag.set()
        if proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Forward Cornea stderr (OPEN / S.4 faults / SEATED) untouched.
    def _pipe_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    # Forward keyboard commands (v = look now, s = seat room, q = quit).
    def _pipe_stdin() -> None:
        assert proc.stdin is not None
        try:
            for line in sys.stdin:
                if proc.poll() is not None:
                    break
                proc.stdin.write(line)
                proc.stdin.flush()
                if line.strip().lower() == "q":
                    stop_flag.set()
                    break
        except (OSError, ValueError):
            pass

    threading.Thread(target=_pipe_stderr, daemon=True).start()
    threading.Thread(target=_pipe_stdin, daemon=True).start()

    print("LUCENT live vision loop active.", file=sys.stderr, flush=True)
    print("  v + Enter = instant camera look | s + Enter = seat room | q + Enter = quit",
          file=sys.stderr, flush=True)

    busy_lock = threading.Lock()
    last_auto_ts = 0.0

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if stop_flag.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            is_manual = ev.get("trigger") == "manual_look"
            if args.manual_only and not is_manual:
                continue

            now = time.monotonic()
            if not is_manual and (now - last_auto_ts) < args.cooldown:
                continue

            if not busy_lock.acquire(blocking=False):
                if is_manual:
                    print(json.dumps({"organ": "LUCENT_MED", "status": "busy",
                                      "note": "MedGemma still processing previous frame"}),
                          file=sys.stderr, flush=True)
                continue

            if not is_manual:
                last_auto_ts = now

            def _worker(event_copy: dict[str, Any]) -> None:
                try:
                    _analyze_event(
                        event_copy,
                        llm_url=llm_url,
                        memory_url=memory_url,
                        prompt=args.prompt,
                        max_tokens=args.max_tokens,
                        api_key=args.api_key,
                    )
                finally:
                    busy_lock.release()

            threading.Thread(target=_worker, args=(ev,), daemon=True).start()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
