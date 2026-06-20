# Architecture

A **capture terminal with memory**: jot thoughts on the M5Stack Cardputer — typed or spoken —
they land in a local Markdown file on the Mac, get indexed into a **local knowledge graph**, and
you can **chat with your own notes** from the terminal. Fully offline. Project-name-agnostic.

```
CAPTURE
Cardputer (ESP32-S3)                         Mac (M4 Pro)
  keyboard ─type─┐                             host/bridge.py   serial loop + routing
  side btn ─rec──┤  ──USB-CDC NDJSON──>         ├─ host/transcribe.py  PCM → text (mlx-whisper)
  ES8311 mic ────┘                             ├─ host/store.py        append → data/captures.md
  240x135 screen <──status/echo/count──         └─ index (background) → cognee-mcp graph

QUERY
  host/chat.py ──"what did I do today?"──> cognee-mcp (recall, CHUNKS) ──notes──┐
       ↑___________________ grounded answer ___________ Exo (Qwen3.5, local) <──┘
```
The Cardputer is a dumb I/O terminal — **no AI runs on it**. The Mac transcribes, stores, indexes
into the graph (Cognee), and answers questions with a local LLM (Exo). Nothing leaves the machine.

## Files
| Path | Role |
|---|---|
| `firmware/cardputer/cardputer.ino` | Device firmware: keyboard, side-button mic, screen UI, serial. |
| `host/bridge.py` | **Run this.** Opens serial, reads NDJSON, routes notes/audio, sends status back. |
| `host/store.py` | Appends a timestamped entry to `data/captures.md`; returns the running count. |
| `host/transcribe.py` | PCM16 audio → text via `mlx-whisper`. |
| `host/cognee_ingest.py` | Pushes captures into the cognee-mcp graph (`remember`). CLI `--reset` rebuilds; `bridge.py` calls it live. |
| `host/chat.py` | **Ask your notes.** Retrieves from the graph (`recall` CHUNKS) and answers with Exo. |
| `data/captures.md` | The captured thoughts, human-readable — the source of truth. The graph is just an index. |
| `host/brain.py`, `host/ask.py`, `host/reindex.py` | Fallback memory path: Cognee wired directly (no MCP server), project-local DB. See *Memory* below. |
| `host/mcp_server.py`, `.mcp.json` | Exposes the fallback path as an MCP server (`ask_notes`/`ingest_note`) to Claude Code. |
| `archive/` | Prior work kept for reuse: `janus_brain.py` (contradiction engine), `janus_viz.py` (graph). |

## Wire protocol (NDJSON, `\n`-terminated)
```
device → host:
  {"type":"note","src":"keyboard","text":"..."}
  {"type":"rec_start"}                                     # M2
  {"type":"audio","seq":N,"b64":"<pcm16le base64>","last":false}   # M2
  {"type":"rec_end"}                                       # M2
host → device:
  {"status":"idle|REC|TRANSCRIBING|SAVED|ERR"}             # status bar, ≤18 chars
  {"echo":"<text>"}                                        # shown in LAST pane
  {"count":N}                                              # total captures
```
Audio: 16 kHz mono int16. This unit has **no usable PSRAM**, so audio is **streamed** in 0.1 s
chunks (base64) *during* recording — not buffered then sent. Any length, ~6 KB RAM.

## Screen UI (240×135, ~40 cols)
```
 notes:3                       ● REC      <- status bar (count + state)
 LAST:                                    <- most recent capture (wrapped)
 remote work kills my focus lately
 > buy milk and eggs_                     <- input line (keyboard)
```
Keyboard types (Enter saves a `(keyboard)` note). Side button (G0) toggles `(voice)` recording.

## Memory & query (Cognee + Exo, all local)

Captures are indexed into a **Cognee** knowledge graph and queried with a local **Exo** LLM
(Qwen3.5 on MLX). Two interchangeable paths:

- **Primary — cognee-mcp** *(used by the demo)*: the official Cognee MCP server, run from a
  sibling checkout of the Cognee repo (`<cognee>/cognee-mcp`), configured via its `.env` for Exo (LLM) +
  **fastembed** (local ONNX embeddings — Exo has no `/embeddings` endpoint) + ladybug (graph).
  `bridge.py` and `cognee_ingest.py` push captures in (`remember`); `chat.py` queries it.
- **Fallback — in-process** (`brain.py`): the same Cognee config wired directly in the project
  venv (project-local DB), no server. `ask.py` is its CLI; `mcp_server.py` re-exposes it over MCP.

**Why `chat.py` retrieves then answers itself** instead of Cognee's one-shot `recall` synthesis:
Cognee's OpenAI adapter forces instructor `json_schema_mode`, which routes answers through
tool-calls that Exo's small MLX model can't emit (`Instructor does not support multiple tool
calls`). So `chat.py` uses `recall` only for **retrieval** (`CHUNKS`, pure vector search, no LLM)
and hands the notes to Exo for a free-text answer — robust and fully under our control.

The markdown store is the source of truth; the graph is a rebuildable index
(`cognee_ingest.py --reset`).

## Milestones
- **M0** handshake · **M1** keyboard → `captures.md` ✅ · **M2** mic → transcript ✅ · **M3** memory + chat ✅ · **M4** UX polish.

## Run (see README / docs/setup.md for installs + the full demo runbook)
```
uv run python host/bridge.py                     # capture: type / record on the Cardputer
uv run python host/chat.py "what did I do today?" # ask your notes (needs cognee-mcp + Exo up)
```
