# Plan — Capture Terminal (Cardputer ⇄ Mac), capture-first MVP

## Context
We reset the project to a clean, project-name-agnostic base so it can be renamed later.
The first goal is **basic Cardputer⇄Mac integration**: capture thoughts on the Cardputer —
**typed on the keyboard or spoken into its mic** — and land them in a local Markdown file on
the Mac, fully offline. The contradiction-detection "brain" and graph viz are **deferred**
(archived in `archive/`, reused in a later phase). This plan is the capture backbone everything
else will plug into.

Locked decisions (from user):
- **Voice source:** Cardputer SPM1423 mic → PCM over USB serial → Whisper on Mac (the authentic, harder path).
- **Interaction:** top **side button (G0)** toggles voice recording; the **keyboard** always types. No mode switching.
- **Storage:** single growing `data/captures.md`, each entry timestamped + tagged `(keyboard)` / `(voice)`.

## Architecture
```
Cardputer (ESP32-S3)                         Mac (M4 Pro)
  keyboard ─type─┐                             host/bridge.py  (serial loop, dispatch)
  side btn ─rec──┤  ──USB-CDC NDJSON──>         ├─ host/transcribe.py  (PCM → text, mlx-whisper)
  SPM1423 mic ───┘                             ├─ host/store.py       (append → data/captures.md)
  240x135 screen <──status/echo/count──         └─ data/captures.md    (the local file)
```
Cardputer = dumb I/O terminal (no AI on-device). Mac does transcription + storage. No network at runtime.

## File layout (generic names — no project name baked in)
| Path | What it is | What it does |
|---|---|---|
| `firmware/cardputer/cardputer.ino` | Cardputer firmware | keyboard capture, side-button mic, screen UI, serial I/O. (M1 keyboard scaffold already drafted; M2 adds mic.) |
| `host/bridge.py` | **the program you run** | opens serial, reads NDJSON, routes notes/audio, sends status back |
| `host/store.py` | storage helper | appends a timestamped entry to `data/captures.md`, returns total count |
| `host/transcribe.py` | STT helper | PCM16 bytes/WAV → text via `mlx-whisper` (Apple Silicon) |
| `data/captures.md` | output | the captured thoughts (human-readable) |
| `docs/ARCHITECTURE.md` | clarity doc | the diagram, wire protocol, UI, how each piece works |
| `README.md` | quickstart | install + run + flash, points to docs |
| `archive/` | old work (kept) | `janus_brain.py` (contradiction engine), `janus_viz.py` (graph) — reused in a later phase |

## Wire protocol (NDJSON, `\n`-terminated, both directions)
```
device → host:
  {"type":"note","src":"keyboard","text":"..."}              # typed note
  {"type":"rec_start"}                                       # side button pressed
  {"type":"audio","seq":N,"b64":"<pcm16le base64>","last":false}
  {"type":"rec_end"}                                         # side button pressed again
host → device:
  {"status":"idle|REC|TRANSCRIBING|SAVED|ERR"}               # ≤18 chars, shown in status bar
  {"echo":"<text just saved/transcribed>"}                   # shown in LAST pane
  {"count":N}                                                # total captures
```
Audio: **16 kHz mono int16**, recorded to PSRAM on the device, sent **after** recording stops
(record-then-send, not real-time streaming) in base64 NDJSON chunks. Recording capped at ~30 s
(≈960 KB PSRAM, well within 8 MB).

## Cardputer screen UI (240×135, ~40 cols)
```
┌──────────────────────────────────────┐  status bar: notes:N            <state>
│ notes:3                       ● REC   │  (state: idle / ● REC 0:04 / TRANSCRIBING / SAVED)
├──────────────────────────────────────┤
│ LAST:                                  │  body: most recent capture, word-wrapped
│ remote work kills my focus lately      │
│                                        │
├──────────────────────────────────────┤
│ > buy milk and eggs_                   │  input line: what you're typing
└──────────────────────────────────────┘
```
Keyboard → type, Enter saves a `(keyboard)` note. Side button (G0) → toggle `(voice)` recording;
while recording the bar shows `● REC` + elapsed seconds; on stop, `TRANSCRIBING` then `SAVED` +
the transcript appears in LAST.

## Milestones (each independently verifiable — build in order)
- **M0 — Serial handshake.** Flash a minimal sketch; `host/bridge.py` connects to `/dev/cu.usbmodem*`.
  *Verify:* bridge prints a received line when you press a key on the Cardputer.
- **M1 — Keyboard → file (pipeline backbone).** Type a note → `bridge.py` appends to `data/captures.md`,
  sends `SAVED`+`count`; screen shows it in LAST. *Verify:* typed "hello" appears in `captures.md` with a timestamp and `(keyboard)`; screen shows `SAVED`, `notes:1`.
- **M2 — Cardputer mic → transcript (the hard part).** Side button records SPM1423 via M5 `Mic_Class`
  HAL → PCM16 to PSRAM → base64 NDJSON to Mac → `transcribe.py` (mlx-whisper) → append `(voice)`.
  *Verify:* press button, speak a sentence, press again → correct-ish transcript in `captures.md` and on screen.
- **M3 — UX polish.** Live `● REC` + elapsed timer, `TRANSCRIBING` state, clean wrapping, error frames.
  *Verify:* full type+speak session reads cleanly on the tiny screen; no garbled audio.

## Key technical decisions
- **Mic:** use M5 `M5Cardputer.Mic` (`Mic_Class` HAL) — **not** raw `ESP_I2S` (known-broken for SPM1423 PDM).
- **STT engine:** `mlx-whisper` (Apple-Silicon native, GPU, fast) with `mlx-community/whisper-small`.
  Fallback: `faster-whisper` (CPU). Pick small/base for speed.
- **Build flag:** `ARDUINO_USB_CDC_ON_BOOT=1` (USB-C is native CDC, not a UART bridge). Libs: `M5Cardputer`, `ArduinoJson` v7.
- **Serial:** `115200` is fine for keyboard; bump to **921600** for the audio phase (both ends).
- **Python:** capture MVP deps are light — `pyserial` + `mlx-whisper`. If `mlx-whisper` lacks a 3.13 wheel,
  pin the venv to 3.12 (currently `requires-python = ">=3.13"`).

## Risks & mitigations
- **Audio-over-serial is the hard part** → record-then-send (not streaming), cap 30 s, base64 NDJSON; M1 keyboard path proves the pipeline first so the mic plugs into known-good plumbing.
- **Whisper model needs network once** → pre-download the model while online; runtime is then offline.
- **PDM mic quirks** → M5 HAL only; test a 3 s clip round-trip before building UX.
- **Flashing fear** → reversible; ESP32-S3 has a ROM bootloader, reflash stock via M5Burner anytime.

## Install / deps (USER runs these — you don't install)
```bash
uv add pyserial mlx-whisper            # capture MVP deps (run in the project)
# firmware (one-time): arduino-cli core install esp32:esp32 ; arduino-cli lib install "M5Cardputer" "ArduinoJson"
# whisper model: pre-cache once online (first transcribe downloads mlx-community/whisper-small)
```

## End-to-end verification
1. Flash `firmware/cardputer/cardputer.ino`; confirm the `notes:0` UI on the device.
2. `python host/bridge.py` on the Mac (auto-detects `/dev/cu.usbmodem*`).
3. Type "hello world" + Enter → it appears in `data/captures.md` `(keyboard)` and on screen → **M1 done**.
4. Press side button, speak, press again → transcript in `data/captures.md` `(voice)` and on screen → **M2 done**.
5. Pull Wi-Fi and repeat step 4 → still works (offline) → demo-ready.

## Deferred (not in this plan)
Contradiction detection, knowledge graph, graph viz, Exo cluster, Cognee, reminders, TTS — all live in
`archive/` and return in a later phase once capture is solid.
