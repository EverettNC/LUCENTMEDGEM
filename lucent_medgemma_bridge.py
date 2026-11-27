#!/usr/bin/env python3
"""LUCENT -> Local C++ llama-server (MedGemma 1.5 4B + MedSigLIP mmproj) Bridge.

Zero third-party dependencies (Python stdlib + macOS native 'sips' only).
Protocol: TCAP-V-0044 | Locality: strict loopback (127.0.0.1 / localhost only).
Enforces FDA S.4 input integrity, dual FIPS 180-4 frame digests, and Q25 server lineage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8791"


def _require_loopback(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"refused non-loopback endpoint '{url}' — LUCENT never sends clinical images off-device"
        )
    return url.rstrip("/")


def _sips_path() -> str:
    found = shutil.which("sips")
    if not found:
        raise RuntimeError("macOS 'sips' not found")
    return found


def _s4_check_and_prepare(image_path: Path, max_dim: int = 896) -> tuple[bytes | None, dict[str, Any]]:
    """FDA S.4 gate using native macOS sips -> uncompressed BMP pixel audit."""
    if not image_path.is_file():
        return None, {"sensor_state": "NO_SIGNAL", "reason": f"file not found: {image_path}"}
    raw_file = image_path.read_bytes()
    if not raw_file:
        return None, {"sensor_state": "NO_SIGNAL", "reason": "empty file (0 bytes)"}

    meta: dict[str, Any] = {
        "frame_sha256": hashlib.sha256(raw_file).hexdigest(),
        "frame_sha512": hashlib.sha512(raw_file).hexdigest(),
        "bytes": len(raw_file),
    }

    sips = _sips_path()
    with tempfile.TemporaryDirectory(prefix="lucent-s4-") as tmp:
        bmp_path = Path(tmp) / "audit.bmp"
        jpg_path = Path(tmp) / "vision.jpg"

        # 1. Decode to small uncompressed BMP for deterministic luma/contrast audit.
        audit_run = subprocess.run(
            [sips, "-s", "format", "bmp", "-Z", "128", str(image_path), "--out", str(bmp_path)],
            capture_output=True, text=True, timeout=20,
        )
        if audit_run.returncode != 0 or not bmp_path.is_file():
            return None, {
                **meta,
                "sensor_state": "NO_SIGNAL",
                "reason": f"undecodable image: {(audit_run.stderr or audit_run.stdout).strip()[:300]}",
            }

        bmp = bmp_path.read_bytes()
        if len(bmp) < 54 or bmp[:2] != b"BM":
            return None, {**meta, "sensor_state": "NO_SIGNAL", "reason": "invalid decoded pixel buffer"}

        px_offset = struct.unpack_from("<I", bmp, 10)[0]
        w = struct.unpack_from("<i", bmp, 18)[0]
        h = abs(struct.unpack_from("<i", bmp, 22)[0])
        bpp = struct.unpack_from("<H", bmp, 28)[0]
        step = bpp // 8
        if w < 4 or h < 4 or step not in (3, 4):
            return None, {**meta, "sensor_state": "NO_SIGNAL", "reason": f"unsupported BMP layout ({w}x{h}, {bpp}bpp)"}

        row_stride = ((w * step + 3) // 4) * 4
        if px_offset + row_stride * h > len(bmp):
            return None, {**meta, "sensor_state": "NO_SIGNAL", "reason": "truncated pixel buffer"}

        n = w * h
        luma_sum = 0.0
        luma_sq = 0.0
        dark_px = 0
        bright_px = 0

        for y in range(h):
            row_start = px_offset + y * row_stride
            for x in range(w):
                idx = row_start + x * step
                b, g, r = bmp[idx], bmp[idx + 1], bmp[idx + 2]
                y_val = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
                luma_sum += y_val
                luma_sq += y_val * y_val
                if y_val < 0.02:
                    dark_px += 1
                if y_val > 0.98:
                    bright_px += 1

        mean_luma = luma_sum / n
        contrast = math.sqrt(max(0.0, (luma_sq / n) - mean_luma * mean_luma))
        dark_ratio = dark_px / n
        bright_ratio = bright_px / n

        meta.update({
            "audit_width": w,
            "audit_height": h,
            "mean_luma": round(mean_luma, 4),
            "contrast": round(contrast, 4),
        })

        if dark_ratio > 0.96 or (mean_luma < 0.02 and contrast < 0.01):
            return None, {**meta, "sensor_state": "DARK_OCCLUDED", "reason": "frame is dark or occluded"}
        if bright_ratio > 0.96 or (mean_luma > 0.98 and contrast < 0.01):
            return None, {**meta, "sensor_state": "WASHED", "reason": "frame is washed out"}

        # 2. Prepare clean JPEG payload for llama-server (MedSigLIP expects up to 896/448px).
        prep_run = subprocess.run(
            [sips, "-s", "format", "jpeg", "-s", "formatOptions", "90",
             "-Z", str(max_dim), str(image_path), "--out", str(jpg_path)],
            capture_output=True, text=True, timeout=30,
        )
        if prep_run.returncode != 0 or not jpg_path.is_file():
            return None, {**meta, "sensor_state": "NO_SIGNAL", "reason": "failed to encode vision JPEG"}
        jpeg_bytes = jpg_path.read_bytes()

    meta["sensor_state"] = "LOCKED"
    return jpeg_bytes, meta


def _http_json(url: str, body: dict[str, Any] | None = None, timeout: int = 180) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:500]}


def main() -> int:
    parser = argparse.ArgumentParser(description="LUCENT -> Local C++ llama-server MedGemma Vision Bridge")
    parser.add_argument("image", help="Path to verified image file")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Local llama-server URL (loopback only)")
    parser.add_argument("--prompt", default="Describe the visible features conservatively and state any uncertainty.")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--max-dim", type=int, default=896)
    args = parser.parse_args()

    try:
        server = _require_loopback(args.server)
    except ValueError as exc:
        print(json.dumps({"organ": "LUCENT_MED", "status": "refused", "error": str(exc)}, indent=2))
        return 2

    jpeg_bytes, s4 = _s4_check_and_prepare(Path(args.image).expanduser().resolve(), max_dim=args.max_dim)
    packet: dict[str, Any] = {
        "organ": "LUCENT_MED",
        "protocol": "TCAP-V-0044",
        "runtime": "llama.cpp (pure C++)",
        "endpoint": server,
        "execution_locality": "on_device",
        "left_machine": False,
        "intended_use": "decision_aid_only_requires_carbon_liaison_review",
        "s4_input_integrity": s4,
    }

    # S.4 gate: dead/dark/washed frame NEVER reaches the model.
    if jpeg_bytes is None:
        packet["status"] = "refused"
        packet["output_dropped"] = True
        print(json.dumps(packet, indent=2))
        return 2

    # Q25 Item 4: ask the serving system itself what model is answering.
    try:
        m_status, models_info = _http_json(f"{server}/v1/models", timeout=10)
        reported_model = None
        if m_status == 200 and isinstance(models_info.get("data"), list) and models_info["data"]:
            reported_model = models_info["data"][0].get("id")
    except (OSError, urllib.error.URLError) as exc:
        packet["status"] = "error"
        packet["error"] = f"The sight-model failed (local llama-server unreachable at {server}: {exc})"
        print(json.dumps(packet, indent=2))
        return 1

    b64_img = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "model": reported_model or "local-medgemma",
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an on-device visual observation layer inside The Christman AI Project "
                    "(Protocol TCAP-V-0044). Describe ONLY what is directly supported by the pixels. "
                    "If the image is unclear or insufficient, say so plainly and stop. "
                    "Do not fabricate clinical findings. Requires Carbon Liaison / clinician review."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    {"type": "text", "text": args.prompt},
                ],
            },
        ],
    }

    try:
        status, reply = _http_json(f"{server}/v1/chat/completions", body=payload, timeout=180)
    except (OSError, urllib.error.URLError) as exc:
        packet["status"] = "error"
        packet["error"] = f"The sight-model failed ({exc})"
        print(json.dumps(packet, indent=2))
        return 1

    if status != 200:
        packet["status"] = "error"
        packet["http_status"] = status
        packet["error"] = f"The sight-model failed ({status})"
        packet["server_reply"] = reply
        print(json.dumps(packet, indent=2))
        return 1

    choice = (reply.get("choices") or [{}])[0]
    observation = (choice.get("message") or {}).get("content", "").strip()

    packet["status"] = "ok"
    packet["server_reported_model"] = reply.get("model") or reported_model
    packet["observation"] = observation
    packet["usage"] = reply.get("usage")
    print(json.dumps(packet, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
