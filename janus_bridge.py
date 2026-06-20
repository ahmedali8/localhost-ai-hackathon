"""
janus_bridge.py — Janus backend bridge (asyncio + pyserial-asyncio)

Janus is a CONTRADICTION CATCHER: it argues with your past self.
Users log decisions/beliefs/claims; Janus detects when a new entry
contradicts, drifts from, or supersedes a past one.

Tethered to the Cardputer (USB-CDC) and:
  - janus_brain  : claim graph + contradiction detection
  - Exo cluster  : OpenAI-compat endpoint at Ubuntu node (http://localhost:52415/v1)
  - Ollama       : fallback at http://localhost:11434/v1
  - faster-whisper: STT for mic audio (CUDA, int8_float16)

Wire protocol (NDJSON, \\n-terminated):
  Cardputer -> here:
    {"cmd":"dump","text":"..."}                          # log a new claim/decision
    {"cmd":"ask","text":"..."}                           # query the brain
    {"cmd":"remind","text":"...","in_s":<int>}           # time-based reminder
    {"cmd":"rec_start"} / {"cmd":"rec_end"}              # mic spike brackets
    {"cmd":"audio","b64":"...","seq":<int>,"done":<bool>}# mic PCM16 chunks

  Here -> Cardputer:
    {"status":"CHECKING"}     # dump received, scanning for contradictions
    {"status":"CONFLICT!"}    # contradiction found
    {"status":"CONSISTENT"}   # no contradiction found
    {"status":"THINKING"}     # LLM inference running
    {"status":"REC..."}       # recording in progress
    {"status":"TRANSCRIBING"} # Whisper running
    {"status":"<<=40 chars>"}  # any other status
    {"t":"<token>"}           # streamed LLM token (cap 200 chars/frame)
    {"e":1}                   # end of token stream
    {"link":"CONTRADICTS: <A vs B, <=40 chars>"}  # contradiction surfaced
    {"link":"ECHOES: <A -> B, <=40 chars>"}       # related prior claim
    {"notify":"<reminder text>"}                  # fired reminder
    {"err":"<msg>"}

janus_brain expected API (janus_brain.py sibling module — canonical contract):
    async store_claim(text, infer_fn)  -> dict | None
        # dict keys: {"kind": "contradicts"|"echoes", "other": str, "summary": str}
        # None when no significant relation was found.

    async answer(query, infer_fn)  -> AsyncIterator[str]   # async-yields tokens
        # RAG: retrieve relevant prior claims, stream answer via infer_fn

    infer_fn signature passed from bridge to brain (canonical):
        async def infer(prompt: str, context_chunks: list) -> AsyncIterator[str]
            # builds the OpenAI message list internally, streams from Exo
            # (Ollama fallback), and async-yields token strings.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import sys
import tempfile
import time
from typing import AsyncIterator, Iterator, Optional

import httpx
import serial_asyncio

# ---------------------------------------------------------------------------
# Config — override any of these via environment variables
# ---------------------------------------------------------------------------

# Serial port for Cardputer USB-CDC. Empty = auto-detect /dev/ttyACM*.
SERIAL_PORT: str = os.environ.get("JANUS_SERIAL_PORT", "")

SERIAL_BAUD: int = int(os.environ.get("JANUS_BAUD", "921600"))

# Exo cluster endpoint (Ubuntu node that has the Cardputer tether)
EXO_BASE: str = os.environ.get("JANUS_EXO_BASE", "http://localhost:52415")

# Ollama fallback endpoint
OLLAMA_BASE: str = os.environ.get("JANUS_OLLAMA_BASE", "http://localhost:11434")

# Model names (same name works for both Exo and Ollama; Ollama model may differ)
CHAT_MODEL: str = os.environ.get("JANUS_CHAT_MODEL", "llama-3.1-8b-instruct")
OLLAMA_CHAT_MODEL: str = os.environ.get("JANUS_OLLAMA_CHAT_MODEL", "janus-llm")

# faster-whisper settings — loaded lazily on first audio request
WHISPER_MODEL_SIZE: str = os.environ.get("JANUS_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE: str = os.environ.get("JANUS_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE: str = os.environ.get("JANUS_WHISPER_COMPUTE", "int8_float16")

# How long to let janus_brain.store_claim() block before giving up (seconds).
# store_claim does a synchronous embedding + LLM contradiction check.
STORE_CLAIM_TIMEOUT: float = float(os.environ.get("JANUS_STORE_TIMEOUT", "45"))

# LLM streaming timeout per request
LLM_TIMEOUT: float = float(os.environ.get("JANUS_LLM_TIMEOUT", "120"))

# Exo cluster health-probe timeout (seconds)
EXO_PROBE_TIMEOUT: float = float(os.environ.get("JANUS_EXO_PROBE_TIMEOUT", "5"))

# Max link label length for the {"link"} serial frame
LINK_MAX_CHARS: int = 40

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("janus_bridge")

# ---------------------------------------------------------------------------
# Lazy Whisper model (loaded once at startup, stays resident in VRAM)
# ---------------------------------------------------------------------------

_whisper_model = None


def _load_whisper():
    """Load faster-whisper. Call once; result cached in module-level var."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel

        log.info(
            "Loading Whisper %s on %s (%s) — first load may take 5-10 s...",
            WHISPER_MODEL_SIZE,
            WHISPER_DEVICE,
            WHISPER_COMPUTE,
        )
        _whisper_model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
        )
        log.info("Whisper loaded OK.")
    except Exception as exc:
        log.error("Whisper load failed: %s — audio transcription unavailable", exc)
    return _whisper_model


# ---------------------------------------------------------------------------
# Inference client: Exo cluster (primary) → Ollama (automatic fallback)
# ---------------------------------------------------------------------------

# Probed once on startup; None = not yet probed
_using_exo: Optional[bool] = None


async def _probe_exo(client: httpx.AsyncClient) -> bool:
    """Return True if the Exo cluster API is reachable and healthy."""
    try:
        r = await client.get(f"{EXO_BASE}/v1/models", timeout=EXO_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


async def _evict_ollama_vram(client: httpx.AsyncClient) -> None:
    """
    Release the model from Ollama's VRAM by setting keep_alive=0.
    Must run before Whisper to avoid OOM on 6 GB RTX-3050.
    Best-effort — never raises.
    """
    try:
        await client.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": OLLAMA_CHAT_MODEL, "keep_alive": 0},
            timeout=5.0,
        )
        log.debug("Ollama VRAM evicted")
    except Exception as exc:
        log.debug("Ollama eviction skipped: %s", exc)


def _make_infer_fn(
    http_client: httpx.AsyncClient,
    using_exo: bool,
):
    """
    Return the canonical ASYNC-GENERATOR infer_fn the brain consumes via
    `async for`.

    Canonical signature:
        async def infer(prompt: str, context_chunks: list[dict]) -> AsyncIterator[str]

    It builds the OpenAI-style message list internally (system + RAG context +
    user prompt), streams SSE deltas from Exo (or the Ollama fallback) over the
    shared async httpx client, and yields token strings one at a time.
    """

    base_url = f"{EXO_BASE}/v1" if using_exo else f"{OLLAMA_BASE}/v1"
    model = CHAT_MODEL if using_exo else OLLAMA_CHAT_MODEL

    async def infer_fn(prompt: str, context_chunks: list) -> AsyncIterator[str]:
        """Build messages from (prompt, context_chunks) and stream tokens."""
        messages: list[dict] = [
            {
                "role": "system",
                "content": "You are Janus, an assistant that argues with the "
                "user's past self. Answer using the provided prior claims as "
                "context. Be concise.",
            }
        ]
        if context_chunks:
            context_text = "\n".join(
                c.get("text", "") for c in context_chunks if c.get("text")
            )
            if context_text:
                messages.append(
                    {"role": "system", "content": f"Prior claims:\n{context_text}"}
                )
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": 512,
            "temperature": 0.7,
        }
        try:
            async with http_client.stream(
                "POST",
                f"{base_url}/chat/completions",
                json=payload,
                timeout=LLM_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
        except Exception as exc:
            log.error("infer_fn error (base=%s): %s", base_url, exc)
            # Yield nothing on error — brain handles the empty iterator

    return infer_fn


async def _get_infer_fn(client: httpx.AsyncClient):
    """
    Probe Exo (once) and return an async infer_fn pointed at the live backend.
    """
    global _using_exo
    if _using_exo is None:
        _using_exo = await _probe_exo(client)
        log.info(
            "Inference backend: %s",
            f"Exo ({EXO_BASE})" if _using_exo else f"Ollama fallback ({OLLAMA_BASE})",
        )
    return _make_infer_fn(client, _using_exo)


# ---------------------------------------------------------------------------
# Serial token streaming helper
# ---------------------------------------------------------------------------

async def _stream_tokens_from_iter(
    token_iter: Iterator[str],
    send_frame,
) -> None:
    """
    Drain a synchronous token iterator and write each token as {"t":"..."}.
    Caps each frame at 200 chars (protocol requirement).
    Sends {"e":1} after the iterator is exhausted.

    Runs the iterator in an executor to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    def _drain_to_queue():
        """Runs in thread executor; puts tokens onto the asyncio queue."""
        try:
            for token in token_iter:
                if token:
                    asyncio.run_coroutine_threadsafe(queue.put(token), loop)
        except Exception as exc:
            log.error("Token iteration error: %s", exc)
        # Signal end
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    # Start draining in a thread (so serial writes can interleave)
    loop.run_in_executor(None, _drain_to_queue)

    while True:
        token = await queue.get()
        if token is None:
            break
        # Cap at 200 chars/frame per protocol
        while token:
            frame_text = token[:200]
            token = token[200:]
            await send_frame({"t": frame_text})

    await send_frame({"e": 1})


# ---------------------------------------------------------------------------
# Audio accumulator
# ---------------------------------------------------------------------------

class AudioAccumulator:
    """Accumulates base64-encoded PCM16 chunks until done=True."""

    def __init__(self):
        self._chunks: list[bytes] = []

    def add_chunk(self, b64_data: str, seq: int, done: bool) -> bool:
        """Decode and store a chunk. Returns True when done=True received."""
        try:
            raw = base64.b64decode(b64_data)
            self._chunks.append(raw)
        except Exception as exc:
            log.warning("Audio chunk decode error seq=%d: %s", seq, exc)
        return done

    def get_pcm16_bytes(self) -> bytes:
        return b"".join(self._chunks)

    def reset(self):
        self._chunks = []


async def _transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> Optional[str]:
    """
    Run faster-whisper on raw int16 PCM bytes.
    Returns transcript string, or None on failure.
    Runs in thread executor (blocking).
    """
    model = _load_whisper()
    if model is None:
        return None

    import wave

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)       # int16 = 2 bytes per sample
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16_bytes)

        def _run():
            segments, _info = model.transcribe(
                tmp_path,
                beam_size=5,
                language="en",
                vad_filter=True,    # strip silence regions automatically
            )
            return " ".join(seg.text for seg in segments).strip()

        transcript = await asyncio.get_event_loop().run_in_executor(None, _run)
        log.info("Whisper transcript (%d chars): %r", len(transcript), transcript[:100])
        return transcript

    except Exception as exc:
        log.error("Whisper transcription error: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Reminder scheduler
# ---------------------------------------------------------------------------

class ReminderScheduler:
    """Simple asyncio time-based reminder scheduler."""

    def __init__(self, send_frame):
        self._send_frame = send_frame
        self._tasks: list[asyncio.Task] = []

    def schedule(self, text: str, in_s: int) -> None:
        """Schedule `text` to fire after `in_s` seconds."""
        task = asyncio.create_task(self._fire(text, in_s))
        self._tasks.append(task)
        # Prune completed tasks so the list doesn't grow indefinitely
        self._tasks = [t for t in self._tasks if not t.done()]
        log.info("Reminder scheduled: %r in %ds", text, in_s)

    async def _fire(self, text: str, in_s: int) -> None:
        await asyncio.sleep(in_s)
        log.info("Reminder firing: %r", text)
        await self._send_frame({"notify": text[:120]})


# ---------------------------------------------------------------------------
# Serial port auto-detection
# ---------------------------------------------------------------------------

def _find_serial_port() -> str:
    """
    Auto-detect Cardputer USB-CDC port.
    Priority: JANUS_SERIAL_PORT env var > /dev/ttyACM* (Linux) > /dev/cu.usbmodem* (macOS).
    """
    if SERIAL_PORT:
        return SERIAL_PORT

    candidates = sorted(glob.glob("/dev/ttyACM*"))
    if candidates:
        log.info("Auto-detected serial port: %s", candidates[0])
        return candidates[0]

    candidates = sorted(glob.glob("/dev/cu.usbmodem*"))
    if candidates:
        log.info("Auto-detected serial port: %s", candidates[0])
        return candidates[0]

    raise RuntimeError(
        "No Cardputer serial port found. "
        "Set JANUS_SERIAL_PORT env var (e.g. /dev/ttyACM0) or check USB connection."
    )


# ---------------------------------------------------------------------------
# Link label helpers
# ---------------------------------------------------------------------------

def _fmt_link(prefix: str, text: str) -> str:
    """
    Build a link frame value: "<PREFIX>: <truncated text>" capped at LINK_MAX_CHARS.
    prefix is e.g. "CONTRADICTS" or "ECHOES".
    """
    available = LINK_MAX_CHARS - len(prefix) - 2   # ": "
    if available <= 0:
        return prefix[:LINK_MAX_CHARS]
    snippet = text.strip()
    # First sentence only for brevity
    period = snippet.find(". ")
    if 0 < period < available:
        snippet = snippet[:period + 1]
    else:
        snippet = snippet[:available]
    snippet = snippet.rstrip(".")
    return f"{prefix}: {snippet}"[:LINK_MAX_CHARS]


# ---------------------------------------------------------------------------
# Main bridge
# ---------------------------------------------------------------------------

class JanusBridge:
    def __init__(self, port: str, baud: int):
        self._port = port
        self._baud = baud
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._http = httpx.AsyncClient(timeout=None)     # per-request timeouts
        self._audio = AudioAccumulator()
        self._recording = False
        self._reminders: Optional[ReminderScheduler] = None
        self._send_lock = asyncio.Lock()                 # serialize serial writes

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("Opening serial %s @ %d baud", self._port, self._baud)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud
        )
        self._reminders = ReminderScheduler(self._send_frame)
        log.info("Janus bridge ready. Contradiction detection online.")

        # Pre-load Whisper in background so the first audio request isn't slow.
        asyncio.create_task(
            asyncio.get_event_loop().run_in_executor(None, _load_whisper)
        )

        # Probe Exo cluster now so first dump doesn't pay the probe cost.
        asyncio.create_task(self._preflight_probe())

        await self._read_loop()

    async def _preflight_probe(self) -> None:
        """Probe Exo cluster on startup (updates _using_exo global)."""
        await _get_infer_fn(self._http)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Serial I/O
    # ------------------------------------------------------------------

    async def _send_frame(self, obj: dict) -> None:
        """Serialize `obj` as NDJSON and write to serial (write-serialized)."""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        async with self._send_lock:
            self._writer.write(encoded)
            await self._writer.drain()

    async def _read_loop(self) -> None:
        """
        Main read loop — reads one NDJSON line at a time.
        Audio chunks are handled inline; all other commands are dispatched
        as independent asyncio Tasks so the loop is never blocked.
        """
        while True:
            try:
                raw_line = await self._reader.readline()
            except Exception as exc:
                log.error("Serial read error: %s — exiting read loop", exc)
                break

            if not raw_line:
                continue

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            log.debug("RX: %s", line[:120])

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Invalid JSON from Cardputer: %r", line[:80])
                await self._send_frame({"err": "invalid json"})
                continue

            cmd = msg.get("cmd", "")

            if cmd == "audio":
                # High-frequency chunks — handle inline to avoid task overhead.
                await self._handle_audio_chunk(msg)
            else:
                # All other commands run as independent tasks.
                asyncio.create_task(self._dispatch(cmd, msg))

    async def _dispatch(self, cmd: str, msg: dict) -> None:
        """Route a command to its handler; catch all exceptions."""
        try:
            if cmd == "dump":
                await self._handle_dump(msg)
            elif cmd == "ask":
                await self._handle_ask(msg)
            elif cmd == "remind":
                await self._handle_remind(msg)
            elif cmd == "rec_start":
                await self._handle_rec_start()
            elif cmd == "rec_end":
                await self._handle_rec_end()
            else:
                log.warning("Unknown cmd: %r", cmd)
                await self._send_frame({"err": f"unknown cmd: {cmd}"})
        except Exception as exc:
            log.exception("Unhandled error in cmd=%r: %s", cmd, exc)
            await self._send_frame({"err": f"internal: {str(exc)[:60]}"})

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_dump(self, msg: dict) -> None:
        """
        Log a new claim/decision/belief.

        Protocol:
          1. Send {"status":"CHECKING"}   — immediately, before any LLM work
          2. Call janus_brain.store_claim(text, infer_fn) in executor
             (store_claim: embed + retrieve semantically-related priors +
              LLM contradiction judge + write graph edges)
          3a. If contradiction found:
                {"status":"CONFLICT!"}
                {"link":"CONTRADICTS: <prior claim snippet>"}
          3b. If no contradiction:
                {"status":"CONSISTENT"}
                {"link":"ECHOES: <related prior claim snippet>"}  (if any echo)
          Note: there is NO LLM streaming token output for dump — status + link only.
        """
        text = msg.get("text", "").strip()
        if not text:
            await self._send_frame({"err": "empty dump"})
            return

        # Immediate acknowledgment — Cardputer shows "CHECKING" in HUD
        await self._send_frame({"status": "CHECKING"})

        infer_fn = await _get_infer_fn(self._http)

        # store_claim() is an async coroutine (it pushes its own blocking
        # embedding/LLM work to executors internally). Await it directly on the
        # bridge's event loop, bounded by STORE_CLAIM_TIMEOUT.
        try:
            result = await asyncio.wait_for(
                _call_store_claim(text, infer_fn),
                timeout=STORE_CLAIM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("store_claim timed out after %ds", STORE_CLAIM_TIMEOUT)
            await self._send_frame({"status": "SAVED"})  # claim still stored; graph timed out
            return
        except Exception as exc:
            log.error("store_claim error: %s", exc)
            await self._send_frame({"err": f"store failed: {str(exc)[:50]}"})
            return

        # store_claim returns: {"kind": "contradicts"|"echoes",
        #                       "other": str, "summary": str}  or None.
        if result and result.get("kind") == "contradicts":
            await self._send_frame({"status": "CONFLICT!"})
            summary = result.get("summary", "")
            if summary:
                await self._send_frame({"link": summary[:LINK_MAX_CHARS]})
            log.info(
                "Contradiction detected. New claim: %r | Prior: %r",
                text[:60],
                result.get("other", "")[:60],
            )
        else:
            await self._send_frame({"status": "CONSISTENT"})
            if result and result.get("summary"):
                await self._send_frame(
                    {"link": result["summary"][:LINK_MAX_CHARS]}
                )

    async def _handle_ask(self, msg: dict) -> None:
        """
        Query the brain.

        Examples: "what have I contradicted myself on?"
                  "how has my view on AI changed?"
                  "show me all my beliefs about X"

        Protocol:
          1. {"status":"THINKING"}
          2. janus_brain.answer(query, infer_fn) -> token stream
          3. stream {"t":"..."} frames, then {"e":1}
          4. {"link":"CONTRADICTS/ECHOES: ..."} if brain surfaces a connection
        """
        query = msg.get("text", "").strip()
        if not query:
            await self._send_frame({"err": "empty question"})
            return

        await self._send_frame({"status": "THINKING"})

        infer_fn = await _get_infer_fn(self._http)

        # answer() is an async generator — iterate it directly on the event loop,
        # writing each token as a {"t":...} frame (capped 200 chars), then {"e":1}.
        import janus_brain
        try:
            async for token in janus_brain.answer(query, infer_fn):
                if not token:
                    continue
                while token:
                    frame_text = token[:200]
                    token = token[200:]
                    await self._send_frame({"t": frame_text})
        except Exception as exc:
            log.error("janus_brain.answer error: %s", exc)

        await self._send_frame({"e": 1})

    async def _handle_remind(self, msg: dict) -> None:
        """Schedule a simple time-based reminder."""
        text = msg.get("text", "").strip()
        in_s = msg.get("in_s", 0)

        if not text:
            await self._send_frame({"err": "empty reminder"})
            return
        if not isinstance(in_s, (int, float)) or in_s <= 0:
            await self._send_frame({"err": "in_s must be a positive number"})
            return

        self._reminders.schedule(text, int(in_s))
        label = f"REM {int(in_s)}s"[:40]
        await self._send_frame({"status": label})

    async def _handle_rec_start(self) -> None:
        """Begin a push-to-talk recording session."""
        self._audio.reset()
        self._recording = True
        await self._send_frame({"status": "REC..."})
        log.info("Audio recording started")

    async def _handle_rec_end(self) -> None:
        """
        Signal that recording has ended.
        The Cardputer will now stream audio chunks via {"cmd":"audio",...}.
        We just log here; _handle_audio_chunk drives transcription.
        """
        self._recording = False
        log.info("Audio recording ended — awaiting chunks")

    async def _handle_audio_chunk(self, msg: dict) -> None:
        """
        Accumulate a base64-encoded PCM16 chunk.

        When done=True:
          1. Evict Ollama from VRAM (so Whisper can use the GPU)
          2. Run faster-whisper in executor
          3. Treat transcript as a {"cmd":"dump"} (call store_claim, detect contradictions)
        """
        b64 = msg.get("b64", "")
        seq = msg.get("seq", 0)
        done = bool(msg.get("done", False))

        complete = self._audio.add_chunk(b64, seq, done)
        if not complete:
            return  # Still accumulating chunks

        # All chunks received — start transcription
        await self._send_frame({"status": "TRANSCRIBING"})
        log.info("Audio assembly complete — transcribing")

        # Free Ollama VRAM before Whisper runs (RTX-3050 6 GB constraint)
        await _evict_ollama_vram(self._http)

        pcm16_bytes = self._audio.get_pcm16_bytes()
        self._audio.reset()

        transcript = await _transcribe(pcm16_bytes)

        if not transcript:
            await self._send_frame({"err": "transcription failed"})
            return

        log.info("Transcript ready: %r", transcript[:80])

        # Route the transcript through the dump handler exactly as if the user
        # had typed it — this triggers contradiction detection, graph storage, etc.
        await self._handle_dump({"cmd": "dump", "text": transcript})


# ---------------------------------------------------------------------------
# janus_brain call wrappers (thin; isolate import + call in one place)
# ---------------------------------------------------------------------------

async def _call_store_claim(text: str, infer_fn) -> Optional[dict]:
    """
    Await janus_brain.store_claim() and return its result dict (or None).

    Result keys (canonical): {"kind", "other", "summary"} or None when no
    significant relation was found.

    Returns None on import failure (graceful degradation: claim path skipped).
    """
    try:
        import janus_brain  # sibling module
    except ImportError as exc:
        log.error(
            "janus_brain not found: %s. "
            "Make sure janus_brain.py is in the same directory.",
            exc,
        )
        # Graceful degradation: act as if stored, no relation found
        return None
    return await janus_brain.store_claim(text, infer_fn)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    port = _find_serial_port()
    bridge = JanusBridge(port=port, baud=SERIAL_BAUD)
    try:
        await bridge.start()
    except KeyboardInterrupt:
        log.info("Shutdown requested via keyboard interrupt.")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())

