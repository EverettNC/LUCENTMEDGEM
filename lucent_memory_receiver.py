#!/usr/bin/env python3
"""MEMORY Five-Element sight receiver + LIVE VIDEO Viewer for LUCENT (Protocol TCAP-V-0044)."""

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
LIVE_PREVIEW = STATE_DIR / "frames" / "live_preview.jpg"
LIVE_FAULT = STATE_DIR / "live_fault.json"
LOOK_TRIGGER = STATE_DIR / "look.trigger"

_lock = threading.Lock()
_ring: deque[dict[str, Any]] = deque(maxlen=8)
_prev_sha256 = "0" * 64

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LUCENT — Live On-Device Sight</title>
<style>
  body { background:#010608; color:#f3f4f6; font-family:-apple-system,BlinkMacSystemFont,sans-serif; margin:0; padding:24px; }
  .wrap { max-width:1150px; margin:0 auto; }
  .top { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(230,200,117,.3); padding-bottom:14px; margin-bottom:20px; flex-wrap:wrap; gap:12px; }
  .title { font-size:20px; letter-spacing:2px; color:#e6c875; font-weight:700; }
  .pill { font-family:monospace; font-size:12px; padding:5px 12px; border-radius:999px; border:1px solid rgba(0,240,255,.4); color:#00f0ff; }
  .pill.fault { border-color:#ef4444; color:#fca5a5; background:rgba(239,68,68,.12); }
  .btn { background:#02161c; color:#e6c875; border:1px solid #e6c875; border-radius:8px; padding:9px 15px; font-size:13px; cursor:pointer; font-weight:700; }
  .btn.primary { background:#e6c875; color:#010608; }
  .btn.on { background:#00f0ff; color:#010608; border-color:#00f0ff; }
  .grid { display:grid; grid-template-columns:1.15fr 1fr; gap:20px; }
  @media (max-width:850px){ .grid { grid-template-columns:1fr; } }
  .card { background:#020d11; border:1px solid rgba(230,200,117,.25); border-radius:14px; padding:18px; }
  .label { font-family:monospace; font-size:11px; color:#00f0ff; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:10px; display:flex; justify-content:space-between; }
  img.frame { width:100%; border-radius:10px; border:1px solid rgba(0,240,255,.35); background:#000; display:block; }
  .obs { font-size:18px; line-height:1.55; color:#ffffff; white-space:pre-wrap; margin-top:8px; }
  .meta { font-family:monospace; font-size:11px; color:#9ca3af; margin-top:12px; word-break:break-all; line-height:1.6; }
  .fault-box { border:1px dashed #ef4444; color:#fca5a5; border-radius:10px; padding:48px 16px; text-align:center; font-family:monospace; font-size:14px; }
  .ring { margin-top:14px; display:flex; flex-direction:column; gap:8px; }
  .row { background:#01090d; border:1px solid rgba(230,200,117,.15); border-radius:8px; padding:10px 14px; cursor:pointer; display:flex; justify-content:space-between; gap:12px; font-size:13px; }
  .row:hover, .row.active { border-color:#00f0ff; }
  .mono { font-family:monospace; color:#e6c875; }
  .thumb { width:160px; border-radius:6px; border:1px solid rgba(230,200,117,.3); margin-top:12px; display:block; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <div class="title">LUCENT // LIVE ON-DEVICE SIGHT</div>
      <div style="font-size:12px;color:#9ca3af;margin-top:4px;">Protocol TCAP-V-0044 · 127.0.0.1 only · left_machine: false</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <span class="pill" id="live-pill">LIVE</span>
      <button class="btn primary" id="btn-look">Look Now</button>
      <button class="btn" id="btn-speak">Speak Now</button>
      <button class="btn" id="btn-auto">Auto-Speak: OFF</button>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label"><span>Live Camera Feed (C++ Cornea)</span><span id="fps-label">S.4 GATED</span></div>
      <img id="live-img" class="frame" alt="Live camera feed">
      <div id="live-fault" class="fault-box" style="display:none;"></div>
    </div>
    <div class="card">
      <div class="label"><span>MedGemma Observation (On-Device)</span><span id="sealed-count">0 SEALED</span></div>
      <div id="obs-box" class="obs">Click "Look Now" (or move in front of the camera) for MedGemma to describe what it sees.</div>
      <img id="sealed-thumb" class="thumb" style="display:none;" alt="Sealed frame">
      <div class="meta" id="model-meta"></div>
    </div>
  </div>

  <div class="card" style="margin-top:20px;">
    <div class="label"><span>MEMORY Ring (Newest 8 Sealed Sight Events)</span></div>
    <div id="ring-list" class="ring"></div>
  </div>
</div>

<script>
let events = [];
let selectedIdx = 0;
let autoSpeak = false;
let lastSpokenHash = "";

function speak(text) {
  if (!text || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  window.speechSynthesis.speak(u);
}

document.getElementById("btn-look").onclick = async () => {
  const b = document.getElementById("btn-look");
  b.textContent = "Looking...";
  try { await fetch("/api/lucent/look", { method: "POST" }); } catch (e) {}
  setTimeout(() => { b.textContent = "Look Now"; }, 1200);
};

document.getElementById("btn-auto").onclick = () => {
  autoSpeak = !autoSpeak;
  const b = document.getElementById("btn-auto");
  b.textContent = autoSpeak ? "Auto-Speak: ON" : "Auto-Speak: OFF";
  b.classList.toggle("on", autoSpeak);
};

document.getElementById("btn-speak").onclick = () => {
  const rec = events[selectedIdx];
  const obs = rec && rec.event && rec.event.medgemma && rec.event.medgemma.observation;
  if (obs) speak(obs);
};

// Live ~10 FPS camera loop with real S.4 fault display
const liveImg = document.getElementById("live-img");
const liveFault = document.getElementById("live-fault");
const livePill = document.getElementById("live-pill");

async function refreshLive() {
  try {
    const res = await fetch("/api/lucent/live.jpg?t=" + Date.now(), { cache: "no-store" });
    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const old = liveImg.dataset.objUrl;
      liveImg.src = url;
      liveImg.dataset.objUrl = url;
      if (old) URL.revokeObjectURL(old);
      liveImg.style.display = "block";
      liveFault.style.display = "none";
      livePill.textContent = "LIVE · S.4 LOCKED";
      livePill.classList.remove("fault");
    } else {
      let msg = "NO_SIGNAL (start lucent_vision_loop.py)";
      try {
        const j = await res.json();
        if (j.sensor_state) msg = "S.4 FAULT: " + j.sensor_state + " — OUTPUT DROPPED";
      } catch (e) {}
      liveImg.style.display = "none";
      liveFault.style.display = "block";
      liveFault.textContent = msg;
      livePill.textContent = "S.4 DROPPED";
      livePill.classList.add("fault");
    }
  } catch (e) {}
  setTimeout(refreshLive, 110);
}
refreshLive();

function renderRing() {
  const ringEl = document.getElementById("ring-list");
  ringEl.innerHTML = "";
  document.getElementById("sealed-count").textContent = events.length + " SEALED";
  if (!events.length) return;

  if (selectedIdx >= events.length) selectedIdx = 0;
  const rec = events[selectedIdx];
  const ev = rec.event || {};
  const mg = ev.medgemma || {};

  document.getElementById("obs-box").textContent =
    mg.observation || mg.error || "No MedGemma observation on this event.";

  const thumb = document.getElementById("sealed-thumb");
  if (ev.frame_path) {
    thumb.src = "/api/lucent/frame/" + selectedIdx + "?h=" + encodeURIComponent(rec.entry_sha256);
    thumb.style.display = "block";
  } else {
    thumb.style.display = "none";
  }

  document.getElementById("model-meta").textContent =
    "model: " + (mg.server_reported_model || "n/a") +
    (mg.elapsed_sec != null ? " (" + mg.elapsed_sec + "s)" : "") +
    "\\nframe_sha256: " + (ev.frame_sha256 || "n/a") +
    "\\nledger_sha256: " + (rec.entry_sha256 || "n/a");

  events.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "row" + (idx === selectedIdx ? " active" : "");
    const left = document.createElement("span");
    left.className = "mono";
    left.textContent = (item.received_utc || "").slice(11, 19) + " · " + ((item.event && item.event.kind) || "event");
    const right = document.createElement("span");
    const txt = (item.event && item.event.medgemma && item.event.medgemma.observation) || "";
    right.textContent = txt.slice(0, 75) + (txt.length > 75 ? "..." : "");
    row.appendChild(left);
    row.appendChild(right);
    row.onclick = () => { selectedIdx = idx; renderRing(); };
    ringEl.appendChild(row);
  });
}

async function pollRing() {
  try {
    const res = await fetch("/api/lucent/ring", { cache: "no-store" });
    const data = await res.json();
    const newest = (data.events && data.events[0]) || null;
    const prevTop = events[0] ? events[0].entry_sha256 : "";
    events = data.events || [];
    if (newest && newest.entry_sha256 !== prevTop) {
      selectedIdx = 0;
      const obs = newest.event && newest.event.medgemma && newest.event.medgemma.observation;
      if (autoSpeak && obs && newest.entry_sha256 !== lastSpokenHash) {
        lastSpokenHash = newest.entry_sha256;
        speak(obs);
      }
      renderRing();
    } else if (events.length && !document.getElementById("ring-list").children.length) {
      renderRing();
    }
  } catch (e) {}
}
pollRing();
setInterval(pollRing, 1500);
</script>
</body>
</html>
"""


def _load_existing() -> str:
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
                    _ring.appendleft(rec)
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
    server_version = "LUCENT-MEMORY/1.2"

    def _send(self, status: int, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            raw = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/health":
            self._send(200, {
                "status": "ok", "organ": "LUCENT", "consumer": "MEMORY",
                "protocol": "TCAP-V-0044", "execution_locality": "on_device",
                "ledger": str(LEDGER_PATH), "ring_count": len(_ring),
            })
        elif path == "/api/lucent/live.jpg":
            if LIVE_FAULT.is_file():
                try:
                    self._send(503, json.loads(LIVE_FAULT.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    self._send(503, {"sensor_state": "NO_SIGNAL"})
                return
            if not LIVE_PREVIEW.is_file():
                self._send(404, {"sensor_state": "NO_SIGNAL"})
                return
            try:
                img = LIVE_PREVIEW.read_bytes()
            except OSError:
                self._send(404, {"sensor_state": "NO_SIGNAL"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(img)
        elif path == "/api/lucent/ring":
            with _lock:
                items = list(_ring)
            self._send(200, {"status": "ok", "count": len(items), "events": items})
        elif path.startswith("/api/lucent/frame/"):
            idx_str = path.rsplit("/", 1)[-1]
            if not idx_str.isdigit():
                self._send(400, {"status": "bad_request"})
                return
            idx = int(idx_str)
            with _lock:
                rec = _ring[idx] if 0 <= idx < len(_ring) else None
            fp_raw = rec.get("event", {}).get("frame_path") if rec else None
            if not fp_raw:
                self._send(404, {"status": "not_found"})
                return
            fp = Path(fp_raw).expanduser().resolve()
            if not fp.is_file() or not fp.is_relative_to(STATE_DIR):
                self._send(403, {"status": "refused"})
                return
            img = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(img)
        else:
            self._send(404, {"status": "not_found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/lucent/look":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LOOK_TRIGGER.write_text("1", encoding="utf-8")
            self._send(200, {"status": "triggered"})
            return
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
    _prev_sha256 = _load_existing()
    httpd = ThreadingHTTPServer((HOST, PORT), LucentHandler)
    httpd.daemon_threads = True
    print(f"LUCENT Live Sight View: http://{HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLUCENT MEMORY receiver stopped.", flush=True)


if __name__ == "__main__":
    main()
