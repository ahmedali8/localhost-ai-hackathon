# Setup — reproduce from scratch (macOS, Apple Silicon)

Everything needed to build + run the capture terminal on a fresh Mac. Tracks exact tooling
so the project can be stood up on another laptop end-to-end.

## 0. Hardware
- **M5Stack Cardputer** (this unit is the **ADV / StampS3A** — note: **no usable PSRAM**, so audio is streamed, not buffered).
- A **data** USB-C cable (charge-only cables won't enumerate — this bit us once).
- Apple-Silicon Mac (tested on macOS 26, M4 Pro). No serial driver needed: the ESP32-S3 is native USB-CDC and appears as `/dev/cu.usbmodem*`. No macOS security/approval needed.

## 1. CLI tooling (Homebrew)
```bash
# Homebrew assumed installed (https://brew.sh)
brew install uv            # Python env/dep manager
brew install arduino-cli   # Cardputer firmware build/flash
brew install ollama        # local LLM (only needed for the later contradiction phase)
# brew install python@3.12 # optional fallback python (see §2)
```
Versions seen: `arduino-cli` (latest), `ollama` (latest), `uv` (latest).

## 2. Python environment (uv)
The repo is a uv project (`pyproject.toml`, `requires-python = ">=3.13"`).
```bash
cd <repo>
uv add pyserial      # host serial bridge
uv add mlx-whisper   # Apple-Silicon speech-to-text (M2 voice)
```
Run anything with `uv run python <script>` (uses the project `.venv`).
Note: the host needs **no torch/chromadb** for capture. If `mlx-whisper` ever lacks a wheel for
the pinned Python, pin to 3.12: `uv venv --python 3.12` then `uv add ...`.

## 3. Cardputer firmware toolchain (arduino-cli)
```bash
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32          # installed: 3.3.10
arduino-cli lib install "M5Cardputer" "ArduinoJson"   # M5Cardputer 1.1.1, ArduinoJson 7.4.3
```
Board FQBN: `esp32:esp32:m5stack_cardputer`. Defaults already correct: **USB Mode = Hardware CDC+JTAG**, **CDC On Boot = Enabled** (so USB serial works out of the box — no extra build flags).

### Build + flash
```bash
PORT=$(ls /dev/cu.usbmodem* | head -1)
arduino-cli compile -b esp32:esp32:m5stack_cardputer --upload -p "$PORT" firmware/cardputer
```
(Flashing is reversible; the ESP32-S3 ROM bootloader means you can't brick it. Reflash stock via M5Burner anytime.)

## 4. Models
- **Whisper (M2 voice):** first `transcribe` downloads `mlx-community/whisper-small` from Hugging Face (online once, then offline). Override with `WHISPER_MODEL`.
- **Ollama (later contradiction phase, optional now):**
  ```bash
  ollama pull llama3.1:8b     # primary
  ollama pull llama3.2:3b     # fast fallback
  ```

## 5. Run
```bash
PORT=$(ls /dev/cu.usbmodem* | head -1)
# (flash first, see §3) then:
uv run python host/bridge.py        # auto-detects the port; type a note, or record with Enter/G0
```
Captures land in `data/captures.md` (and the last recording in `data/last_recording.wav`).

## 6. Gotchas (learned the hard way)
- **Power-only USB-C cable** → device never enumerates (`ls /dev/cu.usbmodem*` empty). Use a data cable.
- **No PSRAM** on this unit → don't allocate big audio buffers; stream chunks (the firmware does this).
- **Two Ollama servers** (brew CLI + desktop app) fight over `:11434`. Run only one.
- Python **3.14 is too new** for some ML wheels; uv pins the project python — keep it ≥3.13 (or 3.12 fallback).

## Quick reproduce (fresh Mac, in order)
1. `brew install uv arduino-cli ollama`
2. `arduino-cli` core + libs (§3)
3. `cd <repo> && uv add pyserial mlx-whisper`
4. flash firmware (§3), then `uv run python host/bridge.py`
