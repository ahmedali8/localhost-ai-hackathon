# Capture Terminal

Jot thoughts on an M5Stack Cardputer — **typed or spoken** — they land in a local Markdown file
on your Mac, get indexed into a **local knowledge graph**, and you can **chat with your own
notes** ("what did I do today?"). Fully offline — local Whisper, local LLM (Exo), local graph
(Cognee). (Working name TBD.)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it fits together.

## Status
- ✅ **M1** — keyboard note → `data/captures.md` (+ on-screen confirmation)
- ✅ **M2** — Cardputer mic → on-device record → Whisper on Mac → `(voice)` capture
- ✅ **M3** — notes → Cognee knowledge graph → terminal chat answered by local Exo LLM
- ⏭ **M4** — screen UX polish

## Install (run these yourself)
```bash
# Python deps (uv project)
uv add pyserial            # M1
uv add mlx-whisper         # M2 (Apple-Silicon Whisper)

# Firmware toolchain (one-time)
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install "M5Cardputer" "ArduinoJson"
```

## Run
```bash
# 1. Flash the Cardputer (USB CDC On Boot must be enabled)
PORT=$(ls /dev/cu.usbmodem* | head -1)
arduino-cli compile -b esp32:esp32:m5stack_cardputer \
  --build-property build.extra_flags=-DARDUINO_USB_CDC_ON_BOOT=1 firmware/cardputer
arduino-cli upload -b esp32:esp32:m5stack_cardputer -p "$PORT" firmware/cardputer

# 2. Start the host bridge, then type / record on the Cardputer
uv run python host/bridge.py
```
Captures appear in `data/captures.md`, on the Cardputer screen, and (in the background) get
indexed into the Cognee graph.

## Chat with your notes
Needs the **Exo** LLM and the **cognee-mcp** server running — see
[`docs/setup.md`](docs/setup.md) for the memory stack + the full 4-terminal **demo runbook**.
```bash
uv run python host/chat.py "what did I do today?"   # one-shot
uv run python host/chat.py                           # interactive
# rebuild the graph from the store at any time:
uv run python host/cognee_ingest.py --reset
```
