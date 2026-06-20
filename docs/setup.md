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
uv add mlx-whisper   # Apple-Silicon speech-to-text (voice)
uv add mcp           # MCP client → talks to the cognee-mcp graph (chat.py, cognee_ingest.py)
# cognee + fastembed are also pinned (pyproject) for the in-process fallback path (brain.py); the
# primary path runs Cognee inside the cognee-mcp server (§5), so the project venv only needs `mcp`.
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
- **Whisper (M2 voice):** first `transcribe` downloads `mlx-community/whisper-small-mlx` from Hugging Face (online once, then offline). Note the **`-mlx`** suffix — `whisper-small` (no suffix) 404s. Bigger/more accurate option: `WHISPER_MODEL=mlx-community/whisper-large-v3-turbo`.
- **Exo (the chat LLM):** the [Exo](https://github.com/exo-explore/exo) cluster serves an
  OpenAI-compatible endpoint at `http://127.0.0.1:52415/v1`, model `mlx-community/Qwen3.5-2B-MLX-8bit`.
  Start it before chatting. Sanity: `curl -s http://127.0.0.1:52415/v1/models`.
- **fastembed (embeddings):** first ingest downloads `BAAI/bge-small-en-v1.5` (~130 MB ONNX, online
  once). Exo has **no** `/embeddings` endpoint, so embeddings run locally via fastembed.

## 5. Memory stack (Cognee graph + Exo LLM)

Captures are indexed into a local **Cognee** knowledge graph and queried with **Exo**. The demo
runs the official **cognee-mcp** server from a *sibling* checkout of the Cognee repo (not inside
this repo).
```bash
# one-time: clone + install cognee-mcp, add local embeddings
git clone https://github.com/topoteretes/cognee   # creates ./cognee — referred to as <cognee> below
cd cognee/cognee-mcp && uv sync && uv pip install fastembed
```
Configure `<cognee>/.env` (the Cognee repo root) so Cognee uses Exo + fastembed, fully local:
```ini
LLM_PROVIDER="openai"
LLM_MODEL="openai/mlx-community/Qwen3.5-2B-MLX-8bit"   # "openai/" prefix — litellm needs a provider
LLM_ENDPOINT="http://127.0.0.1:52415/v1"
LLM_API_KEY="x"                                        # Exo ignores it; litellm requires one
LLM_INSTRUCTOR_MODE="json_mode"                        # small MLX model can't do tool/json_schema mode
COGNEE_SKIP_CONNECTION_TEST="true"                     # Exo TTFT ~14s sometimes trips the 30s probe
EMBEDDING_PROVIDER="fastembed"                          # local ONNX; Exo has no /embeddings
EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS="384"
ENABLE_BACKEND_ACCESS_CONTROL="false"                  # single local user, skip multi-tenant auth
```
Start it (serves `http://127.0.0.1:8000/mcp`):
```bash
cd <cognee>/cognee-mcp && uv run cognee-mcp --transport http
```
**Why `chat.py` answers with Exo itself** instead of Cognee's one-shot `recall` synthesis:
Cognee's OpenAI adapter forces instructor `json_schema_mode` → tool-calls Exo's small model can't
emit. So `chat.py` uses `recall` only for retrieval (`CHUNKS`) and hands the notes to Exo. A
fallback path (`host/brain.py`) runs the same Cognee config in-process, no server — see ARCHITECTURE.

## 6. Run

**Capture** (flash first, §3):
```bash
PORT=$(ls /dev/cu.usbmodem* | head -1)
uv run python host/bridge.py        # auto-detects the port; type a note, or record with Enter/G0
```
Captures land in `data/captures.md` (last recording in `data/last_recording.wav`) and are indexed
into the graph in the background (`[cognee] indexed (...)`).

**Chat** (needs Exo §4 + cognee-mcp §5 running):
```bash
uv run python host/chat.py "what did I do today?"   # one-shot
uv run python host/chat.py                           # interactive (Ctrl-D to exit)
uv run python host/cognee_ingest.py --reset          # wipe + rebuild the graph from captures.md
```

### Full demo runbook (4 terminals)
1. **Exo** — local LLM at `:52415` (start the Exo app/cluster).
2. **cognee-mcp** — `cd <cognee>/cognee-mcp && uv run cognee-mcp --transport http`
3. **bridge** — `uv run python host/bridge.py` → type a note, then hold the side-button for a
   voice note. Wait for `[cognee] indexed (keyboard)` and `[cognee] indexed (voice)`.
4. **chat** — `uv run python host/chat.py` → ask "what did I do today?"

If auto-index lags or you re-record, run `uv run python host/cognee_ingest.py --reset` (terminal 4)
before chatting — it rebuilds the graph from `captures.md` in one pass.

## 7. Gotchas (learned the hard way)
- **Power-only USB-C cable** → device never enumerates (`ls /dev/cu.usbmodem*` empty). Use a data cable.
- **No PSRAM** on this unit → don't allocate big audio buffers; stream chunks (the firmware does this).
- Python **3.14 is too new** for some ML wheels; uv pins the project python — keep it ≥3.13 (or 3.12 fallback).
- **cognee-mcp runs in its own venv** (the cognee-mcp checkout's `.venv`) → install `fastembed`
  *there*, not in this repo. Any `.env` change needs a **server restart** to take effect.
- Cognee error **`EmbeddingException ... (422)` / `EMBEDDING_ENDPOINT='None'`** → `.env` is missing
  `EMBEDDING_PROVIDER=fastembed`, so it fell back to OpenAI (no key). Exo has no embeddings.
- Cognee error **`LLM Provider NOT provided`** → `LLM_MODEL` needs the `openai/` prefix.
- Cognee error **`LLM connection test timed out after 30s`** → Exo TTFT is slow; set
  `COGNEE_SKIP_CONNECTION_TEST=true`.
- Cognee error **`Instructor does not support multiple tool calls`** → its `recall` synthesis can't
  run on Exo's small model; `chat.py` sidesteps it (CHUNKS retrieval + Exo answer). Don't "fix" it
  by switching `chat.py` to `GRAPH_COMPLETION`.

## Quick reproduce (fresh Mac, in order)
1. `brew install uv arduino-cli`
2. `arduino-cli` core + libs (§3)
3. `cd <repo> && uv add pyserial mlx-whisper mcp`
4. flash firmware (§3), then `uv run python host/bridge.py`
5. memory stack (§5): clone cognee-mcp, write `.env`, start Exo + `uv run cognee-mcp --transport http`
6. `uv run python host/chat.py "what did I do today?"`
