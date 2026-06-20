# Janus — 2-Hour Build Plan (Mac-solo)

**Janus** = a Contradiction Catcher. You log decisions/beliefs by keyboard; a local LLM
detects when a new claim contradicts your past self and flashes `CONFLICT!`. Fully offline.

> Theme fit: **Cosine offline-agent track** (perceive → retrieve → reason → act → store).
> Exo + Cognee = architected, code present = talking points (not live in the 2h cut).

---

## Scope (2h cuts — be ruthless)
- ✅ Keep: keyboard input → Mac **Ollama** LLM → contradiction detection → screen
- ❌ Cut: Exo cluster (talking point), Cognee layer (`USE_COGNEE=false`), mic/Whisper, TTS, reminders
- 🌥 Stretch only if green: graph viz (`janus_graph.html`), the Cardputer custom firmware

## Architecture (live path)
```
Cardputer (keyboard+screen)  --USB-CDC serial-->  Mac (M4 Pro)
                                                   ├─ janus_bridge.py  (serial + inference)
                                                   ├─ janus_brain.py   (claims + contradiction)
                                                   ├─ Ollama (Metal)   llama3.1:8b   :11434
                                                   ├─ sentence-transformers (CPU)  embeddings
                                                   └─ ChromaDB + networkx (local files, persist)
```

---

## 1. INSTALL  (commands)

### 1a. System tools  — DONE (running/installed)
```bash
brew install ollama python@3.12 arduino-cli   # python 3.14 lacks ML wheels — use 3.12
```

### 1b. Ollama + models  — running in background
```bash
brew services start ollama          # starts the daemon on :11434
ollama pull llama3.1:8b             # ~4.9GB  (primary — strong contradiction judge)
ollama pull llama3.2:3b             # ~2GB    (fast fallback if 8b too slow)
ollama list                         # verify both show up
```

### 1c. Python env (3.12) + deps  — running in background
```bash
cd ~/scorpio/localhost-agentic-hackathon
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install pyserial-asyncio httpx sentence-transformers chromadb networkx
# NOT needed for the keyboard demo (skipped): faster-whisper, cognee, piper-tts
```

### 1d. (OPTIONAL / STRETCH) Exo cluster — skip for the 2h demo
```bash
# Only if you want the live multi-node Exo bounty (slow, ~5-10 tok/s heterogeneous).
git clone https://github.com/exo-explore/exo && cd exo
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[mlx]'   # Mac node
EXO_OFFLINE=1 exo --inference-engine mlx --disable-tui
# then point the bridge at it:  export JANUS_EXO_BASE=http://localhost:52415
# For 2h: leave Exo OFF -> set JANUS_EXO_PROBE_TIMEOUT=0.1 so bridge uses Ollama instantly.
```

### 1e. Cardputer firmware — pick ONE (see §4). Reversible; you cannot brick it.
```bash
# Option C (custom, nicest UX): flash janus.ino via arduino-cli
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install "M5Cardputer" "ArduinoJson"
# plug Cardputer in, find the port, then compile+upload:
PORT=$(ls /dev/cu.usbmodem* | head -1)
arduino-cli compile -b esp32:esp32:m5stack_cardputer \
  --build-property build.extra_flags=-DARDUINO_USB_CDC_ON_BOOT=1 janus.ino
arduino-cli upload  -b esp32:esp32:m5stack_cardputer -p "$PORT" janus.ino
```

---

## 2. RUN  (env + launch)
```bash
cd ~/scorpio/localhost-agentic-hackathon && source .venv/bin/activate
export OLLAMA_HOST=http://localhost:11434
export LLM_MODEL=llama3.1:8b            # brain: extraction + contradiction judge
export JANUS_OLLAMA_CHAT_MODEL=llama3.1:8b
export JANUS_EXO_PROBE_TIMEOUT=0.1      # skip Exo -> Ollama immediately
export JANUS_WHISPER_DEVICE=cpu         # harmless; mic is off
export USE_COGNEE=false
export JANUS_SIM_THRESHOLD=0.25         # cast a wider net so contradictions fire

# A) prove the brain WITHOUT the Cardputer (terminal demo, safety net):
python3 janus_brain.py                  # runs built-in self-test

# B) full loop with Cardputer attached:
export JANUS_SERIAL_PORT=$(ls /dev/cu.usbmodem* | head -1)
python3 janus_bridge.py
```

---

## 3. STEP ORDER (critical path, ~2h)
1. **Installs finish** (Ollama models, pip deps) — running now. ✓ when `ollama list` shows models + pip done.
2. **Prove the brain in terminal** — a tiny script logs 2 claims then a contradicting 3rd via real Ollama; confirm it returns `kind:"contradicts"`. *(This is the guaranteed demo even with no Cardputer.)*
3. **Tune** — if it misses, lower `JANUS_SIM_THRESHOLD` (0.2) / adjust judge prompt; if false-positives, raise to 0.4.
4. **Cardputer** (pick §4 option) — flash/connect, serial smoke test.
5. **Full loop** — run `janus_bridge.py`, type the scripted claims, watch `CONFLICT!`.
6. **Rehearse** — seed claims, pull wifi, run the 3-min demo (§5).

---

## 4. Cardputer options (decide after the brain works)
| Opt | What | Risk | UX |
|----|------|------|----|
| A | **Mac terminal only** | none | type/read on Mac. Guaranteed. |
| B | generic serial-terminal app (M5Burner) | reversible flash | raw text lines on device |
| C | **flash `janus.ino`** | reversible flash | JANUS HUD, red `CONFLICT!`, LOG/ASK modes |

Reflash stock anytime via **M5Burner** (GUI, one click). ESP32-S3 has a ROM bootloader — unbrickable.

---

## 5. Demo script (3 min)
1. Seed 2 opinions (e.g. "Remote work makes me more productive"; "AI will be net-positive for society"). Screen → `CONSISTENT`.
2. **Pull the wifi.** "Everything now is local — embeddings, LLM, graph. No cloud."
3. Log the contradiction: "Remote work is killing my productivity, too many distractions at home." → `CHECKING` → **`CONFLICT!`** + `CONTRADICTS: remote work makes me…`
4. Switch to ASK: "what have I contradicted myself on?" → it streams the answer from the local graph.
5. Close: "perceive → retrieve → reason → act → store. A local agent that argues with your past self. No internet."

## 6. Fallback tiers
- Exo slow/broken → already on Ollama (`EXO_PROBE_TIMEOUT=0.1`).
- Cardputer flaky → **demo from Mac terminal** (Option A) — brain is identical.
- Contradiction misses live → use pre-seeded claims + lower threshold; rehearse the exact 3 lines.
- LLM down → `janus_brain.py` self-test (mock LLM) still shows the full loop.

## 7. Bounty framing (for judges)
- **Cosine (offline agent):** the live perceive→reason→act→store loop, network pulled.
- **Cognee:** graph-memory layer integrated in code (`USE_COGNEE=true` path) — enable on an isolated run.
- **Exo:** clustered-inference architecture in `janus_bridge.py` + setup; Ollama used live for latency.
