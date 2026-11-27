# LUCENTMEDGEM

**The Christman AI Project and Robotics Division** · Protocol **TCAP-V-0044** · Sister organ to [CORTI](https://github.com/EverettNC/CORTI)

**Lucent is the cornea.** It sees on-device, or it says it did not lock.

Zero third-party Python dependencies. Zero PyTorch required on macOS. Zero outbound network calls.

> When the referent is missing, output drops to zero and the failure is reported.

## Architecture

1. `lucent_dsp.hpp` — Pure C++20 visual DSP core (luma/contrast, 3x3 Sobel edge energy, 4-angle directional texture, optical-flow motion lock at coherence >= 0.62, frozen-camera detector, bounded 360-frame tape, 8-event ring, cold human-triggered Room Seat).
2. `lucent_live.mm` — Native macOS AVFoundation + CoreVideo + CoreImage + CommonCrypto camera runner. Classifies camera faults loudly and saves SHA-256 verified frames atomically.
3. `lucent_vision_loop.py` — Pipes verified frames strictly over loopback (127.0.0.1:8791) to local pure-C++ llama-server running MedGemma 1.5 4B IT + MedSigLIP (mmproj), records server-reported model ID and dual SHA-256/SHA-512 digests, and posts to MEMORY.
4. `lucent_medgemma_bridge.py` — Single-image air-gapped CLI bridge using Python stdlib + macOS sips with built-in S.4 pixel audit.
5. `lucent_memory_receiver.py` — Loopback (127.0.0.1:8790) MEMORY Five-Element receiver with dual-hashed (SHA-256 + SHA-512), hash-chained JSONL ledger.
6. `lucent_holoscan_op.hpp` — NVIDIA Holoscan C++ Operator wrapping lucent::Cornea as an in-graph S.4 gate upstream of GPU inference.

## Build & Run (macOS)

Build native cornea:

    make

Start local C++ llama-server (MedGemma 1.5 4B + mmproj-F16.gguf) on loopback:

    ~/llama.cpp/build/bin/llama-server \
      -m ~/models/medgemma-1.5-4b-gguf/medgemma-1.5-4b-it-Q4_K_M.gguf \
      --mmproj ~/models/medgemma-1.5-4b-gguf/mmproj-F16.gguf \
      --host 127.0.0.1 --port 8791 -c 8192

Start MEMORY ledger receiver (Terminal 1):

    python3 lucent_memory_receiver.py

Start live camera -> MedGemma -> MEMORY loop (Terminal 2):

    python3 lucent_vision_loop.py

Controls while running:
- `v` + Enter — instant S.4-verified live camera look
- `s` + Enter — seat room baseline (requires >= 3 locked frames)
- `q` + Enter — clean shutdown

## Scope & Limits

- Decision-aid and observational tooling only; clinical use requires Carbon Liaison / clinician review.
- Refuses any non-loopback endpoint by construction (127.0.0.1 / localhost only).
- Does not invent visual descriptions over dark, occluded, washed-out, frozen, or missing frames.

© 2026 Everett Nathaniel Christman · The Christman AI Project and Robotics Division
