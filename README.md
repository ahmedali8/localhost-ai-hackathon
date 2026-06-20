# Janus — the agent that argues with your past self

> *Janus: Roman god of two faces — one looking forward, one looking back.*
> Janus catches you contradicting yourself, then puts it in your face.

**Project track:** Cosine offline-agent bounty + Cognee graph bounty @ Overmind Localhost Agentic Hackathon (20 June 2026)

**What it is:** A fully-offline contradiction-detection agent. You log decisions, beliefs, and claims (keyboard-first on the M5Cardputer, voice later). Janus builds a graph of your stated positions and **proactively detects when a new entry contradicts, drifts from, or supersedes a past one**, then surfaces it on the Cardputer screen: `CONTRADICTS: you said X 3wk ago`.

**What it is NOT:** a generic note-taker or second-brain. The core value proposition is *argument*, not *archive*.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Janus System                                │
│                                                                      │
│  M5Cardputer (ESP32-S3)                                              │
│  ┌─────────────────────┐                                             │
│  │ 240×135 LCD         │  type claim/decision                        │
│  │ 56-key keyboard     │──────────────────────────────────────┐      │
│  │ SPM1423 mic (later) │  USB-CDC NDJSON (\n delimited)       │      │
│  └─────────────────────┘                                      │      │
│            USB-C (tethered)                                   │      │
│                  │                                            │      │
│                  ▼                                            │      │
│  Ubuntu node (RTX 3050, 8 GB VRAM)                           │      │
│  ┌────────────────────────────────────────────────────────┐   │      │
│  │  janus_bridge.py (asyncio serial bridge)               │◄──┘      │
│  │   │                                                    │          │
│  │   ├─► janus_brain.py                                   │          │
│  │   │    ├── ChromaDB (sentence-transformers, CPU)       │          │
│  │   │    │   all-MiniLM-L6-v2 · .janus_vectors/         │          │
│  │   │    ├── networkx DiGraph  (.janus_graph.pkl)        │          │
│  │   │    │   nodes: Claim/Decision/Belief                │          │
│  │   │    │   edges: supports|contradicts|supersedes      │          │
│  │   │    │          |relates-to                          │          │
│  │   │    └── Cognee (bonus graph layer, async/timeout)  │          │
│  │   │                                                    │          │
│  │   └─► Exo cluster endpoint  http://localhost:52415/v1  │          │
│  │        (OpenAI-compat, shards model across both nodes) │          │
│  │        Fallback: Ollama      http://localhost:11434/v1  │          │
│  │                                                        │          │
│  │  faster-whisper (large-v3 int8_float16 on RTX)        │          │
│  └────────────────────────────────────────────────────────┘          │
│            LAN (UDP broadcast peer discovery, gRPC activations)      │
│                  │                                                    │
│                  ▼                                                    │
│  M4 Pro Mac (24 GB unified RAM)                                      │
│  ┌────────────────────────────────────────────────────────┐          │
│  │  Exo node (MLX backend)                               │          │
│  │  Runs the other shard of the LLM model                │          │
│  │  janus_viz.py (D3 graph HTML — open in browser)       │          │
│  └────────────────────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────────────────────┘
```

**Data flow per claim:**
1. User types on Cardputer → `{"cmd":"dump","text":"..."}` over USB-CDC
2. `janus_bridge.py` calls `janus_brain.store_claim()` → ChromaDB embed + graph write
3. Contradiction detection runs (see agent loop below)
4. Result sent back: `{"link":"CONTRADICTS: you said X 3wk ago"}` or `{"link":"ECHOES: you said X"}`
5. Status: `CHECKING` → `CONFLICT!` or `CONSISTENT`

---

## Files

| File | Description |
|------|-------------|
| `janus.ino` | M5Cardputer firmware (ESP32-S3, NDJSON serial protocol) |
| `janus_bridge.py` | Asyncio bridge: serial ↔ brain ↔ LLM cluster |
| `janus_brain.py` | Memory + contradiction detection (ChromaDB + networkx + Cognee) |
| `janus_viz.py` | D3 force-directed graph renderer (offline, self-contained HTML) |

---

## Graph Schema

**Nodes — Claim / Decision / Belief:**
```
{
  id:        UUID string
  text:      full text of the claim
  reason:    why the user held this position (extracted or stated)
  stance:    POSITIVE | NEGATIVE | UNCERTAIN | NEUTRAL
  topic:     top-level topic label (LLM-extracted)
  timestamp: unix int
  source:    "cardputer" | "mic"
}
```

**Edges:**
```
supports      — new claim reinforces prior claim on same topic
contradicts   — new claim opposes prior claim on same topic (stance conflict)
supersedes    — new claim explicitly replaces prior (user says "I changed my mind")
relates-to    — same topic, no clear stance conflict
```

---

## The Contradiction Agent Loop (Cosine offline-agent bounty)

This is the core agentic loop that runs **entirely offline** on the Exo cluster:

```
┌─────────────────────────────────────────────────────────────────────┐
│                  JANUS CONTRADICTION AGENT LOOP                     │
│                                                                      │
│  PERCEIVE                                                            │
│    New claim arrives via serial: {"cmd":"dump","text":"..."}        │
│    → janus_bridge._handle_dump()                                    │
│    → send {"status":"CHECKING"}                                      │
│                                                                      │
│  RETRIEVE                                                            │
│    janus_brain.search(claim_text, k=5)                              │
│    → sentence-transformers all-MiniLM-L6-v2 (CPU, always-on)       │
│    → ChromaDB cosine similarity over all stored claims              │
│    → returns top-5 prior claims on potentially related topics       │
│                                                                      │
│  REASON                                                              │
│    LLM judge call (Exo cluster → Ollama fallback):                  │
│    "Given PRIOR CLAIM and NEW CLAIM on similar topics,              │
│     do they share the same topic AND have opposing stances?         │
│     Return: {conflict: bool, type: contradicts|drifts|supersedes,   │
│              summary: '<30 chars>'}"                                  │
│    → janus_brain.detect_contradiction(new_claim, prior_claims)      │
│                                                                      │
│  ACT                                                                 │
│    If conflict=true:                                                 │
│      → write "contradicts" edge to networkx graph                   │
│      → send {"link":"CONTRADICTS: <summary>"}                       │
│      → send {"status":"CONFLICT!"}                                   │
│    Else if related but consistent:                                   │
│      → write "relates-to" or "supports" edge                        │
│      → send {"link":"ECHOES: <summary>"}                            │
│      → send {"status":"CONSISTENT"}                                  │
│                                                                      │
│  STORE                                                               │
│    janus_brain.store_claim(text, stance, topic, ...)                │
│    → ChromaDB (always synchronous)                                   │
│    → networkx graph (synchronous after REASON step)                 │
│    → Cognee cognify() (fire-and-forget, 30s timeout)                │
│                                                                      │
│  Loop repeats on every new claim. Runs forever offline.              │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this qualifies as the Cosine offline-agent bounty:**
- Closed perceive → retrieve → reason → act → store loop with no human in the loop
- Runs fully offline: embedding model cached, LLM sharded across Exo cluster, no network calls
- The agent takes *action* (writes graph edges, surfaces contradictions) not just passive retrieval
- Persistent state across sessions (ChromaDB + .pkl graph survive restarts)
- Agentic decisions: the LLM *judges* whether a conflict exists; it is not a keyword match

---

## Exo Cluster Role

Exo shards one LLM (Llama-3.1-8B-Instruct or Qwen2.5-7B-Instruct) across:
- **Ubuntu node (RTX 3050):** tinygrad backend, ~40% of model layers
- **M4 Pro Mac (24 GB):** MLX backend, ~60% of model layers

Activation tensors flow Ubuntu → Mac → Ubuntu over LAN gRPC per token. The Ubuntu node exposes `http://localhost:52415/v1` (OpenAI-compat), which `janus_bridge.py` and `janus_brain.py` point at. The Mac's port is unreachable from the Cardputer USB tether.

**Fallback trigger:** if Exo produces < 3 tok/s or peer discovery fails within 60s, `janus_bridge.py` automatically flips to Ollama at `http://localhost:11434/v1` (same model, same API shape, single-node CUDA).

---

## Cognee Graph Layer Role

Cognee provides a bonus knowledge-graph layer on top of the always-on ChromaDB retrieval:

- After each claim is stored in ChromaDB, `cognee.add()` + `cognee.cognify()` run in a **fire-and-forget background thread** with a **30-second timeout**.
- If Cognee succeeds, `SearchType.CHUNKS` is overlaid on ChromaDB results during `ask` queries.
- If Cognee times out or fails (common with 7B models), the demo continues working — ChromaDB cosine similarity is the always-on source of truth.
- The networkx DiGraph (`.janus_graph.pkl`) is Janus's primary contradiction graph and is populated by the custom JSON extractor, not by Cognee. Cognee is the bonus layer.

---

## Wire Protocol (NDJSON over USB-CDC)

**Cardputer → Host (Ubuntu):**
```jsonc
{"cmd":"dump","text":"I believe remote work is more productive"}  // log a claim
{"cmd":"ask","text":"What have I contradicted myself on?"}        // query the brain
{"cmd":"remind","text":"check notes","in_s":1800}                 // set timer
```

**Host → Cardputer:**
```jsonc
{"status":"CHECKING"}          // contradiction check running
{"status":"CONFLICT!"}         // stance conflict detected
{"status":"CONSISTENT"}        // no contradiction found
{"t":"token text"}             // streamed LLM token
{"e":1}                        // end of stream
{"link":"CONTRADICTS: remote work belief 3wk ago"}   // contradiction surfaced
{"link":"ECHOES: you said remote work helps focus"}  // related prior claim
{"notify":"check notes"}       // fired reminder
{"err":"message"}              // error
```

**Key:** the `{"link"}` frame always carries the strongest signal first:
- Prefix `CONTRADICTS:` → stance conflict detected with a prior claim
- Prefix `ECHOES:` → consistent with a prior claim on the same topic
- No prefix → unrelated connection (fallback cosine similarity label)

---

## Run Order

### Before going offline (one-time, internet required)
Run `setup_ubuntu.sh` on the Ubuntu node and `setup_mac.sh` on the Mac. Both scripts include the one-time model cache-seed step.

### On demo day (fully offline)

**Step 1: Ubuntu node** (plug in Cardputer first)
```bash
cd ~/janus
source .venv/bin/activate

# Start Exo (waits for Mac peer, serves :52415)
EXO_OFFLINE=1 exo --inference-engine tinygrad \
  --chatgpt-api-port 52415 --node-host 0.0.0.0 --disable-tui &

# Wait ~10s for Exo to initialize, then start bridge
python janus_bridge.py
```

**Step 2: M4 Pro Mac** (run after Ubuntu Exo log shows "waiting for peers")
```bash
cd ~/janus-mac
source .venv/bin/activate

EXO_OFFLINE=1 exo --inference-engine mlx \
  --node-host 0.0.0.0 --disable-tui
# If UDP broadcast fails: add --bootstrap-peers <ubuntu-LAN-IP>:52416
```

**Step 3: Verify cluster**
```bash
# On Ubuntu:
curl http://localhost:52415/v1/models
# Should list the sharded model. If empty, check Exo logs for peer discovery.
```

**Step 4: Cardputer**
- Power on (USB-C to Ubuntu)
- Screen shows `JANUS ready.` and mode indicator `[D]` (DUMP) or `[A]` (ASK)
- Backtick `` ` `` toggles between DUMP and ASK modes

---

## Demo Script

```
1. Type a claim in DUMP mode:
   "I think async work makes me more productive than sync office hours"
   → Status: CHECKING
   → Status: CONSISTENT  (first claim, no prior to contradict)

2. Type another claim:
   "I decided to go back to the office full time — I need the structure"
   → Status: CHECKING
   → Status: CONFLICT!
   → CONTRADICTS: productivity belief 2min ago

3. Ask in ASK mode:
   "What have I contradicted myself on?"
   → LLM streams answer using RAG over stored claims
   → "You claimed async work boosts productivity, then decided office
      structure is necessary — these are in direct tension."

4. Type a superseding claim:
   "Update: I changed my mind — hybrid 3 days office is the compromise"
   → Status: CHECKING
   → CONTRADICTS: office full time decision 5min ago
   → Graph writes "supersedes" edge

5. Show janus_viz graph in browser:
   python janus_viz.py  # opens janus_graph.html
   → Nodes: claims / beliefs / entities
   → Red edges: contradicts
   → Blue edges: supports / relates-to
```

---

## Setup Scripts

### setup_ubuntu.sh

```bash
#!/usr/bin/env bash
# setup_ubuntu.sh — Janus Ubuntu node setup (RTX 3050, Cardputer tethered)
# Run ONCE with internet access. Safe to re-run.
set -euo pipefail

JANUS_DIR="${HOME}/janus"
VENV="${JANUS_DIR}/.venv"
WHISPER_VENV="${HOME}/venvs/fwhisper"
ST_CACHE="${JANUS_DIR}/.st_cache"
EXO_DIR="${HOME}/exo"

echo "=== Janus Ubuntu Node Setup ==="
echo "Working dir: ${JANUS_DIR}"

# ── 0. Verify GPU ─────────────────────────────────────────────────────────────
echo ""
echo "--- GPU check ---"
if ! command -v nvidia-smi &>/dev/null; then
  echo "ERROR: nvidia-smi not found. Install NVIDIA drivers first." >&2
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
echo "GPU OK."

# ── 1. System packages ────────────────────────────────────────────────────────
echo ""
echo "--- System packages ---"
sudo apt-get update -qq
sudo apt-get install -y \
  nvidia-cuda-toolkit \
  python3-pip python3-venv python3.11 python3.11-venv \
  git curl build-essential \
  libusb-1.0-0-dev udev

# ── 2. Serial port access ─────────────────────────────────────────────────────
echo ""
echo "--- Serial port setup ---"
# Add user to dialout group (for /dev/ttyACM0)
if ! groups "${USER}" | grep -qw dialout; then
  sudo usermod -aG dialout "${USER}"
  echo "WARN: Added ${USER} to dialout group. You must log out and back in for this"
  echo "      to take effect BEFORE running janus_bridge.py."
fi

# Disable ModemManager so it doesn't grab /dev/ttyACM0 for 30 seconds on plug-in
if systemctl is-active --quiet ModemManager 2>/dev/null; then
  sudo systemctl stop ModemManager
  sudo systemctl disable ModemManager
  echo "ModemManager disabled (was grabbing /dev/ttyACM0)."
fi

# udev rule: make /dev/ttyACM* world-readable without dialout membership
UDEV_RULE='/etc/udev/rules.d/99-cardputer.rules'
if [ ! -f "${UDEV_RULE}" ]; then
  echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", MODE="0666"' \
    | sudo tee "${UDEV_RULE}" > /dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  echo "udev rule installed for Cardputer (VID 303a)."
fi

# ── 3. Ollama ─────────────────────────────────────────────────────────────────
echo ""
echo "--- Ollama (CUDA backend) ---"
if ! command -v ollama &>/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
echo "Ollama version: $(ollama --version)"

# Start Ollama service if not running
if ! pgrep -x ollama > /dev/null; then
  OLLAMA_KEEP_ALIVE=0 ollama serve &
  sleep 3
  echo "Ollama started."
fi

# Pull fallback model
echo "Pulling fallback LLM (llama3.1:8b) — this is ~5 GB, be patient..."
ollama pull llama3.1:8b

# Pull embedding model
echo "Pulling embedding model (nomic-embed-text)..."
ollama pull nomic-embed-text

# Create custom Modelfile with correct context window
MODELFILE="${JANUS_DIR}/Modelfile.janus"
mkdir -p "${JANUS_DIR}"
cat > "${MODELFILE}" <<'MODELEOF'
FROM llama3.1:8b
PARAMETER num_ctx 8192
PARAMETER temperature 0.3
SYSTEM "You are Janus, a contradiction-detection agent. You analyze claims and beliefs to find logical conflicts, stance reversals, and topic drift. Be precise and brief."
MODELEOF
ollama create janus-llm -f "${MODELFILE}"
echo "Custom model 'janus-llm' created with 8192 context window."

# ── 4. Python venv (main) ────────────────────────────────────────────────────
echo ""
echo "--- Python venv (main) at ${VENV} ---"
python3.11 -m venv "${VENV}"
source "${VENV}/bin/activate"

pip install --upgrade pip wheel

pip install \
  pyserial-asyncio \
  httpx \
  networkx \
  chromadb>=0.5 \
  sentence-transformers>=3.0 \
  "cognee[ollama,baml]" \
  piper-tts \
  huggingface_hub

deactivate
echo "Main venv ready."

# ── 5. faster-whisper venv (isolated — avoids cuDNN conflict with Ollama) ───
echo ""
echo "--- faster-whisper venv at ${WHISPER_VENV} ---"
mkdir -p "${HOME}/venvs"
python3.11 -m venv "${WHISPER_VENV}"
source "${WHISPER_VENV}/bin/activate"
pip install --upgrade pip
pip install faster-whisper
# Pin cuDNN for ctranslate2 >= 4.x
pip install nvidia-cudnn-cu12==9.1.0 nvidia-cublas-cu12
deactivate
echo "Whisper venv ready."

# Verify Whisper GPU access
echo "Verifying faster-whisper GPU..."
"${WHISPER_VENV}/bin/python" -c \
  "from faster_whisper import WhisperModel; m=WhisperModel('tiny',device='cuda',compute_type='int8_float16'); print('Whisper GPU OK')" \
  || echo "WARN: Whisper GPU check failed — check cuDNN version mismatch."

# ── 6. Exo cluster (Ubuntu node) ─────────────────────────────────────────────
echo ""
echo "--- Exo cluster install ---"
if [ ! -d "${EXO_DIR}" ]; then
  git clone https://github.com/exo-explore/exo "${EXO_DIR}"
fi
cd "${EXO_DIR}"
git pull --ff-only 2>/dev/null || true

python3.11 -m venv "${EXO_DIR}/.venv"
source "${EXO_DIR}/.venv/bin/activate"
pip install --upgrade pip
pip install -e '.[tinygrad]'
# Upgrade tinygrad to fix CUDA regression in pinned version
pip install --upgrade 'git+https://github.com/tinygrad/tinygrad.git'
deactivate
echo "Exo installed at ${EXO_DIR}."

# ── 7. Firewall rules for Exo ────────────────────────────────────────────────
echo ""
echo "--- Firewall rules ---"
if command -v ufw &>/dev/null; then
  sudo ufw allow 52415/tcp comment "Exo LLM API" 2>/dev/null || true
  sudo ufw allow 52416/udp comment "Exo peer discovery" 2>/dev/null || true
  echo "UFW rules added."
fi

# ── 8. Write .env file ───────────────────────────────────────────────────────
echo ""
echo "--- Writing .env ---"
UBUNTU_LAN_IP=$(ip route get 1.0.0.0 2>/dev/null | awk '{print $7; exit}' || echo "192.168.1.XX")
echo "Detected Ubuntu LAN IP: ${UBUNTU_LAN_IP} (edit .env if wrong)"

cat > "${JANUS_DIR}/.env" <<ENVEOF
# Janus .env — Ubuntu node
# Generated by setup_ubuntu.sh — edit UBUNTU_LAN_IP if auto-detection was wrong.

UBUNTU_LAN_IP=${UBUNTU_LAN_IP}

# === Serial ===
JANUS_SERIAL_PORT=           # blank = auto-detect /dev/ttyACM*
JANUS_BAUD=921600

# === Exo cluster (preferred inference) ===
JANUS_EXO_BASE=http://localhost:52415

# === Ollama fallback ===
JANUS_OLLAMA_BASE=http://localhost:11434

# === Chat model names ===
JANUS_CHAT_MODEL=llama-3.1-8b-instruct
JANUS_OLLAMA_CHAT_MODEL=janus-llm

# === Whisper ===
JANUS_WHISPER_MODEL=large-v3
JANUS_WHISPER_DEVICE=cuda
JANUS_WHISPER_COMPUTE=int8_float16

# === Memory ===
JANUS_VECTORS_PATH=.janus_vectors
JANUS_GRAPH_PATH=.janus_graph.pkl
JANUS_MEM_TIMEOUT=10
JANUS_EXTRACT_TIMEOUT=15
JANUS_CONTEXT_K=5

# === Cognee LLM (bonus layer) ===
LLM_PROVIDER=ollama
LLM_MODEL=janus-llm
LLM_ENDPOINT=http://${UBUNTU_LAN_IP}:11434/v1
LLM_API_KEY=ollama
LLM_MAX_COMPLETION_TOKENS=4096

# === Cognee Embeddings ===
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_ENDPOINT=http://${UBUNTU_LAN_IP}:11434/api/embed
EMBEDDING_DIMENSIONS=768
HUGGINGFACE_TOKENIZER=nomic-ai/nomic-embed-text-v1.5

# === Cognee structured output ===
STRUCTURED_OUTPUT_FRAMEWORK=BAML
BAML_LLM_PROVIDER=ollama
BAML_LLM_MODEL=janus-llm
BAML_LLM_ENDPOINT=http://${UBUNTU_LAN_IP}:11434/v1
BAML_LLM_API_KEY=ollama

# === Cognee storage ===
DB_PROVIDER=sqlite
VECTOR_DB_PROVIDER=lancedb
GRAPH_DATABASE_PROVIDER=networkx
SYSTEM_ROOT_DIRECTORY=.cognee_system
DATA_ROOT_DIRECTORY=.data_storage
TELEMETRY_DISABLED=true
REQUIRE_AUTHENTICATION=False
ENABLE_BACKEND_ACCESS_CONTROL=False

# === sentence-transformers offline cache ===
SENTENCE_TRANSFORMERS_HOME=${JANUS_DIR}/.st_cache

# === Feature flags ===
USE_COGNEE=false
OLLAMA_KEEP_ALIVE=0
OLLAMA_HOST=http://${UBUNTU_LAN_IP}:11434
ENVEOF

echo ".env written to ${JANUS_DIR}/.env"

# ── 9. Pre-cache models (one-time, internet required) ────────────────────────
echo ""
echo "--- Pre-caching models (internet required — do this before going offline) ---"
source "${VENV}/bin/activate"

# sentence-transformers
mkdir -p "${ST_CACHE}"
echo "Caching all-MiniLM-L6-v2..."
SENTENCE_TRANSFORMERS_HOME="${ST_CACHE}" python -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('ST cache OK')"

# HuggingFace tokenizer for Cognee
echo "Caching nomic-embed-text tokenizer..."
python -c \
  "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('nomic-ai/nomic-embed-text-v1.5'); print('Tokenizer cache OK')" \
  2>/dev/null || echo "WARN: tokenizer cache skipped (transformers not installed in main venv)"

deactivate

# Exo model pre-download (Llama-3.1-8B for the tinygrad shard)
echo "Pre-downloading Llama-3.1-8B for Exo (Ubuntu/tinygrad shard)..."
source "${VENV}/bin/activate"
huggingface-cli download \
  meta-llama/Llama-3.1-8B-Instruct \
  --local-dir "${HOME}/.cache/exo/downloads/llama-3.1-8b-instruct" \
  2>/dev/null || echo "WARN: Exo model download failed — you may need: huggingface-cli login"
deactivate

# faster-whisper model pre-download
echo "Pre-caching Whisper large-v3..."
source "${WHISPER_VENV}/bin/activate"
python -c "
from faster_whisper import WhisperModel
print('Downloading Whisper large-v3 (this takes a few minutes)...')
WhisperModel('large-v3', device='cpu', compute_type='int8')
print('Whisper cache OK')
" || echo "WARN: Whisper pre-cache failed — will download on first use"
deactivate

# Piper TTS (stretch goal — skip if not available)
PIPER_VOICE_DIR="${JANUS_DIR}/.piper"
mkdir -p "${PIPER_VOICE_DIR}"
echo "Attempting Piper voice download (stretch goal — skip if this fails)..."
curl -fsSL -o "${PIPER_VOICE_DIR}/en_US-lessac-medium.onnx" \
  "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx" \
  2>/dev/null || echo "SKIP: Piper voice not downloaded (stretch goal)."
curl -fsSL -o "${PIPER_VOICE_DIR}/en_US-lessac-medium.onnx.json" \
  "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json" \
  2>/dev/null || true

# ── 10. Verify offline readiness ─────────────────────────────────────────────
echo ""
echo "=== Offline readiness check ==="

# Check Ollama models
echo -n "Ollama janus-llm: "
ollama show janus-llm > /dev/null 2>&1 && echo "OK" || echo "MISSING — run: ollama create janus-llm -f ${MODELFILE}"

echo -n "Ollama nomic-embed-text: "
ollama show nomic-embed-text > /dev/null 2>&1 && echo "OK" || echo "MISSING — run: ollama pull nomic-embed-text"

# Check sentence-transformers cache
echo -n "sentence-transformers all-MiniLM-L6-v2: "
ls "${ST_CACHE}" 2>/dev/null | grep -q "all-MiniLM" && echo "OK" || echo "MISSING — re-run with internet"

# Check ChromaDB import
echo -n "ChromaDB import: "
"${VENV}/bin/python" -c "import chromadb; print('OK')" 2>/dev/null || echo "MISSING"

# Check Whisper model cache
echo -n "Whisper large-v3 cache: "
ls "${HOME}/.cache/huggingface/hub/" 2>/dev/null | grep -qi whisper && echo "OK" || echo "MISSING"

echo ""
echo "=== safe to go offline: run the checks above and ensure all show OK ==="
echo ""
echo "To start on demo day:"
echo "  1. source ${VENV}/bin/activate"
echo "  2. cd ${JANUS_DIR}"
echo "  3. EXO_OFFLINE=1 ${EXO_DIR}/.venv/bin/exo --inference-engine tinygrad \\"
echo "       --chatgpt-api-port 52415 --node-host 0.0.0.0 --disable-tui &"
echo "  4. python janus_bridge.py"
```

### setup_mac.sh

```bash
#!/usr/bin/env bash
# setup_mac.sh — Janus M4 Pro Mac node setup (Exo MLX shard only)
# Run ONCE with internet access. Safe to re-run.
set -euo pipefail

JANUS_MAC_DIR="${HOME}/janus-mac"
EXO_DIR="${HOME}/exo-mac"
VENV="${EXO_DIR}/.venv"

echo "=== Janus M4 Pro Mac Node Setup ==="

# ── 1. Homebrew and Python ────────────────────────────────────────────────────
echo ""
echo "--- Homebrew + Python ---"
if ! command -v brew &>/dev/null; then
  echo "ERROR: Homebrew not found. Install it first: https://brew.sh" >&2
  exit 1
fi

brew install python@3.11 2>/dev/null || true
PYTHON311=$(brew --prefix python@3.11)/bin/python3.11
echo "Using Python: $("${PYTHON311}" --version)"

# ── 2. Exo (MLX backend) ─────────────────────────────────────────────────────
echo ""
echo "--- Exo MLX install ---"
mkdir -p "${JANUS_MAC_DIR}"

if [ ! -d "${EXO_DIR}" ]; then
  git clone https://github.com/exo-explore/exo "${EXO_DIR}"
fi
cd "${EXO_DIR}"
git pull --ff-only 2>/dev/null || true

"${PYTHON311}" -m venv "${VENV}"
source "${VENV}/bin/activate"
pip install --upgrade pip
pip install -e '.[mlx]'   # MLX is auto-pulled on Apple Silicon
pip install huggingface_hub
deactivate
echo "Exo (MLX) installed at ${EXO_DIR}."

# ── 3. Pre-download model shard for MLX ──────────────────────────────────────
echo ""
echo "--- Pre-downloading Llama-3.1-8B MLX shard (internet required) ---"
source "${VENV}/bin/activate"
huggingface-cli download \
  mlx-community/Meta-Llama-3.1-8B-Instruct-4bit \
  --local-dir "${HOME}/.cache/exo/downloads/llama-3.1-8b-instruct-mlx" \
  2>/dev/null || {
    echo "WARN: MLX model download failed."
    echo "      Run manually: huggingface-cli login && huggingface-cli download mlx-community/Meta-Llama-3.1-8B-Instruct-4bit --local-dir ~/.cache/exo/downloads/llama-3.1-8b-instruct-mlx"
  }
deactivate

# ── 4. Test UDP broadcast reachability (requires Ubuntu node on LAN) ─────────
echo ""
echo "--- UDP broadcast test (best effort — needs Ubuntu on LAN) ---"
"${PYTHON311}" -c "
import socket, sys
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(b'janus-ping', ('<broadcast>', 52416))
    s.close()
    print('UDP broadcast OK (sent to port 52416)')
except Exception as e:
    print(f'UDP test warn: {e} — may need --bootstrap-peers on startup')
"

# ── 5. Write launch helper ────────────────────────────────────────────────────
cat > "${JANUS_MAC_DIR}/start_exo.sh" <<'LAUNCHEOF'
#!/usr/bin/env bash
# start_exo.sh — launch Exo MLX shard on M4 Pro Mac
# Run AFTER Ubuntu node is started (Ubuntu must be up first).

EXO_DIR="${HOME}/exo-mac"
VENV="${EXO_DIR}/.venv"

source "${VENV}/bin/activate"

# Try auto-discovery first
echo "Starting Exo MLX node (auto-discover peers via UDP broadcast)..."
EXO_OFFLINE=1 exo \
  --inference-engine mlx \
  --node-host 0.0.0.0 \
  --disable-tui

# If the above fails to find the Ubuntu node within ~30 seconds, kill it and run:
#   EXO_OFFLINE=1 exo --inference-engine mlx --node-host 0.0.0.0 --disable-tui \
#     --bootstrap-peers <ubuntu-LAN-IP>:52416
LAUNCHEOF
chmod +x "${JANUS_MAC_DIR}/start_exo.sh"

# ── 6. Offline readiness check ───────────────────────────────────────────────
echo ""
echo "=== Offline readiness check ==="

echo -n "Exo binary: "
"${VENV}/bin/exo" --version > /dev/null 2>&1 && echo "OK" || echo "MISSING"

echo -n "MLX model shard: "
ls "${HOME}/.cache/exo/downloads/llama-3.1-8b-instruct-mlx/" 2>/dev/null | grep -q "." \
  && echo "OK" || echo "MISSING — re-run with internet"

echo -n "MLX framework: "
"${VENV}/bin/python" -c "import mlx; print('OK')" 2>/dev/null || echo "MISSING"

echo ""
echo "=== safe to go offline: ensure all checks above show OK ==="
echo ""
echo "To start on demo day (run AFTER Ubuntu Exo node is up):"
echo "  ${JANUS_MAC_DIR}/start_exo.sh"
echo ""
echo "Verify cluster from Ubuntu: curl http://localhost:52415/v1/models"
```

---

## Gotchas and Known Issues

**Serial / Cardputer**
- `ARDUINO_USB_CDC_ON_BOOT=1` must be set in `platformio.ini` build flags or the CDC serial does not enumerate at boot. Physical baud is ignored by USB CDC but set `Serial.begin(921600)` both ends to avoid pyserial open errors.
- ModemManager grabs `/dev/ttyACM0` for ~30 seconds on plug-in. `setup_ubuntu.sh` disables it. If `janus_bridge.py` fails to open serial, check: `sudo systemctl status ModemManager`.
- Mic and speaker cannot be used simultaneously on Cardputer. `Speaker.end()` must be called before `Mic.begin()`. This is a hardware mutual exclusion, not a software bug.

**Exo cluster**
- Start Ubuntu node first (owns port 52415 and the Cardputer USB). Mac node second. Wait for log line `Discovered peer: <IP>:<port>` on both terminals before considering the cluster up.
- Measured 2-node MLX+tinygrad throughput: ~5 tok/s end-to-end (90% penalty vs single-GPU) due to synchronous pipeline-parallel round-trips. This is fine for a demo but not fast.
- Fallback trigger: if `curl http://localhost:52415/v1/models` returns empty or times out after 60s, switch to `ollama serve` and update `.env` with `JANUS_EXO_BASE=http://localhost:11434`.

**Ollama / Cognee**
- Ollama default `keep_alive` is 5 minutes. This means it holds VRAM while idle. Set `OLLAMA_KEEP_ALIVE=0` in environment or Whisper will OOM on the 6 GB RTX. `setup_ubuntu.sh` sets this.
- Cognee's default `LLM_MAX_COMPLETION_TOKENS` is 16384 but Ollama's default `num_ctx` is 4096. This causes HTTP 500 from Ollama during `cognify()`. The custom `Modelfile.janus` sets `num_ctx 8192` and `.env` sets `LLM_MAX_COMPLETION_TOKENS=4096`.
- If Cognee `cognify()` appears to hang forever (no crash, no timeout log), the `asyncio.wait_for(timeout=30)` wrapper in `janus_brain.py` will kill it. This is expected with 7B models that can't produce valid structured JSON after 5 retries.
- `EMBEDDING_PROVIDER` must be explicitly set to `ollama` in `.env`. If omitted, Cognee falls back to OpenAI embeddings and dies offline.

**faster-whisper**
- Install in a separate venv (`~/venvs/fwhisper`) to isolate from Ollama's bundled CUDA libs. If cuDNN version mismatch occurs, ctranslate2 silently falls back to CPU with no error. Verify with: `python -c "from faster_whisper import WhisperModel; m=WhisperModel('tiny',device='cuda'); print('GPU OK')"`.
- Whisper large-v3 `int8_float16` peaks at ~3-4 GB VRAM. Must evict Ollama (POST `keep_alive:0`) before transcribing or the 6 GB RTX will OOM. `janus_bridge.py` does this automatically before each transcription.

**ChromaDB**
- Pass `settings=Settings(anonymized_telemetry=False)` to `PersistentClient`. Without this, ChromaDB tries to phone home on first run, which breaks under network pull.
- ChromaDB data lives in `.janus_vectors/`. Do not delete between runs or all embeddings are lost.

---

## Pip Dependencies

**Main venv (`~/janus/.venv`):**
```
pyserial-asyncio
httpx
networkx
chromadb>=0.5
sentence-transformers>=3.0
cognee[ollama,baml]
piper-tts
huggingface_hub
```

**Whisper venv (`~/venvs/fwhisper`):**
```
faster-whisper
nvidia-cudnn-cu12==9.1.0
nvidia-cublas-cu12
```

**Mac Exo venv (`~/exo-mac/.venv`):**
```
exo[mlx]
huggingface_hub
```

---

## Contradiction Detection — LLM Judge Prompt

The `detect_contradiction()` function in `janus_brain.py` calls the LLM with:

```
System: You are Janus, a contradiction-detection agent. Given a NEW CLAIM and a list of
PRIOR CLAIMS, determine if the new claim contradicts any prior claim.

A contradiction means: same topic AND opposing stance (e.g., "X is good" vs "X is bad",
or "I will do X" vs "I decided not to do X").

Return ONLY valid JSON, no explanation:
{
  "conflict": true|false,
  "type": "contradicts"|"supersedes"|"drifts"|"consistent",
  "prior_index": <int or null>,
  "summary": "<max 30 chars, e.g. 'remote work belief 3wk ago'>"
}

User: NEW CLAIM: <text>
PRIOR CLAIMS:
[0] (2026-06-17) <text>
[1] (2026-06-15) <text>
...
```

If `conflict=true`, `janus_brain.py` writes a `contradicts` edge in the networkx graph and returns the summary with prefix `CONTRADICTS:` in the `{"link"}` frame.

---

## License

MIT. Built for Overmind Localhost Agentic Hackathon, 20 June 2026.

