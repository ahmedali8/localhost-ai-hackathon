# Architecture

A **capture terminal**: jot thoughts on the M5Stack Cardputer — typed or spoken — and they
land in a local Markdown file on the Mac. Fully offline. Project-name-agnostic (rename later).

```
Cardputer (ESP32-S3)                         Mac (M4 Pro)
  keyboard ─type─┐                             host/bridge.py   serial loop + routing
  side btn ─rec──┤  ──USB-CDC NDJSON──>         ├─ host/transcribe.py  PCM → text (mlx-whisper)  [M2]
  SPM1423 mic ───┘                             ├─ host/store.py        append → data/captures.md
  240x135 screen <──status/echo/count──         └─ data/captures.md     the output
```
The Cardputer is a dumb I/O terminal — **no AI runs on it**. The Mac transcribes + stores.

## Files
| Path | Role |
|---|---|
| `firmware/cardputer/cardputer.ino` | Device firmware: keyboard, side-button mic, screen UI, serial. |
| `host/bridge.py` | **Run this.** Opens serial, reads NDJSON, routes notes/audio, sends status back. |
| `host/store.py` | Appends a timestamped entry to `data/captures.md`; returns the running count. |
| `host/transcribe.py` | *(M2)* PCM16 audio → text via `mlx-whisper`. |
| `data/captures.md` | The captured thoughts, human-readable. |
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
Audio (M2): 16 kHz mono int16, recorded to PSRAM, sent **after** stop as base64 chunks; cap ~30 s.

## Screen UI (240×135, ~40 cols)
```
 notes:3                       ● REC      <- status bar (count + state)
 LAST:                                    <- most recent capture (wrapped)
 remote work kills my focus lately
 > buy milk and eggs_                     <- input line (keyboard)
```
Keyboard types (Enter saves a `(keyboard)` note). Side button (G0) toggles `(voice)` recording.

## Milestones
- **M0** serial handshake · **M1** keyboard → `captures.md` ✅ *(current)* · **M2** mic → transcript · **M3** UX polish.

## Run (see README for installs)
```
uv run python host/bridge.py     # then type on the Cardputer
```
Deferred (in `archive/`): contradiction detection, knowledge graph, viz, Exo, Cognee.
