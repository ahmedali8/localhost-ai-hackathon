"""
engram_bridge.py — Engram backend bridge (asyncio + pyserial-asyncio)

Tethers the Cardputer (USB-CDC NDJSON) to:
  - engram_brain  : ChromaDB semantic store + custom graph extractor
  - Exo cluster   : OpenAI-compat endpoint at Ubuntu node (http://localhost:52415/v1)
  - Ollama        : fallback at http://localhost:11434/v1
  - faster-whisper: STT for audio chunks (CUDA, int8_float16)

Wire protocol (NDJSON, \\n-terminated):
  Cardputer -> here:
    {"cmd":"dump","text":"..."}
    {"cmd":"ask","text":"..."}
    {"cmd":"remind","text":"...","in_s":<int>}
    {"cmd":"rec_start"} / {"cmd":"rec_end"}
    {"cmd":"audio","b64":"...","seq":<int>,"done":<bool>}
  Here -> Cardputer:
    {"status":"<<=40 chars>"}
    {"t":"<token>"}            (streamed; capped 200 chars/frame)
    {"e":1}                    (end of stream)
    {"link":"<A -> B, <=40>"}  (strongest graph connection)
    {"notify":"<text>"}        (fired reminder)
    {"err":"<msg>"}
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
from typing import Optional

import httpx
import serial_asyncio

# ---------------------------------------------------------------------------
# Config — override any of these via environment variables
# ---------------------------------------------------------------------------

SERIAL_PORT: str = os.environ.get(
    "ENGRAM_SERIAL_PORT", ""
)  # empty = auto-detect /dev/ttyACM*

SERIAL_BAUD: int = int(os.environ.get("ENGRAM_BAUD", "921600"))

# Exo cluster endpoint (Ubuntu node with Cardputer tether)
EXO_BASE: str = os.environ.get("ENGRAM_EXO_BASE", "http://localhost:52415")

# Ollama fallback endpoint
OLLAMA_BASE: str = os.environ.get("ENGRAM_OLLAMA_BASE", "http://localhost:11434")

# Chat model name used by both Exo and Ollama (same name, different backends)
CHAT_MODEL: str = os.environ.get("ENGRAM_CHAT_MODEL", "llama-3.1-8b-instruct")
OLLAMA_CHAT_MODEL: str = os.environ.get("ENGRAM_OLLAMA_CHAT_MODEL", "engram-llm")

# Whisper model — loaded lazily on first audio request
WHISPER_MODEL_SIZE: str = os.environ.get("ENGRAM_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE: str = os.environ.get("ENGRAM_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE: str = os.environ.get("ENGRAM_WHISPER_COMPUTE", "int8_float16")

# Memory write timeout (seconds) — never block the serial loop beyond this
MEMORY_WRITE_TIMEOUT: float = float(os.environ.get("ENGRAM_MEM_TIMEOUT", "10"))

# LLM inference timeout per streaming request
LLM_TIMEOUT: float = float(os.environ.get("ENGRAM_LLM_TIMEOUT", "120"))

# Max context notes fed to LLM (top-K from semantic search)
CONTEXT_TOP_K: int = int(os.environ.get("ENGRAM_CONTEXT_K", "5"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("engram_bridge")

# ---------------------------------------------------------------------------
# Lazy Whisper model (loaded once, kept resident in VRAM)
# ---------------------------------------------------------------------------

_whisper_model = None


def _load_whisper():
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
        log.error("Whisper load failed: %s — audio will be unavailable", exc)
    return _whisper_model


# ---------------------------------------------------------------------------
# Inference client: Exo first, Ollama fallback
# ---------------------------------------------------------------------------

# Module-level flag so we don't re-probe every request
_using_exo: Optional[bool] = None  # None = not yet probed


async def _probe_exo(client: httpx.AsyncClient) -> bool:
    """Return True if the Exo cluster endpoint is healthy."""
    try:
        r = await client.get(f"{EXO_BASE}/v1/models", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


async def _evict_ollama_vram(client: httpx.AsyncClient) -> None:
    """Release Ollama VRAM before Whisper runs (keep_alive=0)."""
    try:
        await client.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": OLLAMA_CHAT_MODEL, "keep_alive": 0},
            timeout=5.0,
        )
    except Exception:
        pass  # best-effort


async def _stream_chat(
    client: httpx.AsyncClient,
    messages: list[dict],
    send_frame,  # coroutine: send_frame(dict) -> None
) -> None:
    """
    Stream chat completion tokens to the Cardputer.

    Tries Exo first (auto-probed once); falls back to Ollama on error.
    Each delta.content is sent as {"t":"..."} (capped at 200 chars/frame).
    Sends {"e":1} when done.
    """
    global _using_exo

    # Probe once
    if _using_exo is None:
        _using_exo = await _probe_exo(client)
        log.info("Inference backend: %s", "Exo" if _using_exo else "Ollama (fallback)")

    if _using_exo:
        base_url = f"{EXO_BASE}/v1"
        model = CHAT_MODEL
    else:
        base_url = f"{OLLAMA_BASE}/v1"
        model = OLLAMA_CHAT_MODEL

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 512,
        "temperature": 0.7,
    }

    try:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            json=payload,
            timeout=LLM_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
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
                if not content:
                    continue
                # Cap at 200 chars per frame (protocol requirement)
                while content:
                    frame_text = content[:200]
                    content = content[200:]
                    await send_frame({"t": frame_text})

    except Exception as exc:
        log.error("Inference error on %s: %s", "Exo" if _using_exo else "Ollama", exc)
        # If Exo failed, try falling back to Ollama for this request
        if _using_exo:
            log.info("Retrying on Ollama fallback...")
            _using_exo = False  # flip flag for this and future requests
            await _stream_chat(client, messages, send_frame)
            return
        await send_frame({"err": f"LLM error: {str(exc)[:60]}"})

    await send_frame({"e": 1})


# ---------------------------------------------------------------------------
# Brain interaction helpers
# ---------------------------------------------------------------------------

async def _store_and_index(text: str, source: str = "cardputer") -> str:
    """
    Embed and persist a note via engram_brain.store_memory().
    store_memory() already fires graph extraction and Cognee in background threads,
    so this wrapper only needs to bound the blocking ChromaDB write with a timeout.
    Returns the note_id string, or "" on failure.
    """
    import engram_brain  # local module

    try:
        note_id = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, engram_brain.store_memory, text, source
            ),
            timeout=MEMORY_WRITE_TIMEOUT,
        )
        log.info("Stored note %s (len=%d)", note_id, len(text))
        return note_id
    except asyncio.TimeoutError:
        log.warning("Memory write timed out — note may not be stored")
        return ""
    except Exception as exc:
        log.error("Memory write error: %s", exc)
        return ""


async def _build_context_and_answer(
    query: str,
    client: httpx.AsyncClient,
    send_frame,
) -> Optional[str]:
    """
    Semantic search for relevant notes, build RAG prompt, stream LLM answer.
    Returns the strongest connection string (for {"link"} frame), or None.

    Uses engram_brain.search() for retrieval and engram_brain.find_links()
    for the connection label — both are the brain's public API.
    """
    import engram_brain  # local module

    # Semantic search (CPU-side sentence-transformers, fast)
    try:
        hits = await asyncio.get_event_loop().run_in_executor(
            None, engram_brain.search, query, CONTEXT_TOP_K
        )
    except Exception as exc:
        log.error("Semantic search error: %s", exc)
        hits = []

    # Build context block for the LLM prompt
    context_parts = []
    for i, h in enumerate(hits):
        score = h.get("score", 0)
        text = h.get("text", "")
        context_parts.append(f"[{i+1}] (relevance {score:.2f}) {text}")

    context_block = "\n".join(context_parts) if context_parts else "(no notes stored yet)"

    system_msg = (
        "You are Engram, an offline personal second-brain assistant. "
        "Answer concisely using the user's own notes when relevant. "
        "If context notes are provided, reference them. "
        "Keep answers short (3-5 sentences max) for a small screen."
    )

    user_msg = f"My notes:\n{context_block}\n\nQuestion: {query}"

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    await _stream_chat(client, messages, send_frame)

    # {"link"} frame: use brain's find_links() which does cosine similarity across
    # all stored embeddings and returns a ready "A -> B" string (<=40 chars).
    strongest_link: Optional[str] = None
    try:
        link_str = await asyncio.get_event_loop().run_in_executor(
            None, engram_brain.find_links, query
        )
        if link_str:
            strongest_link = link_str[:40]
    except Exception as exc:
        log.debug("find_links error: %s", exc)

    return strongest_link


# ---------------------------------------------------------------------------
# Audio / Whisper transcription
# ---------------------------------------------------------------------------

class AudioAccumulator:
    """Accumulates base64-encoded PCM16 chunks until done=True."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._expected_seq = 0

    def add_chunk(self, b64_data: str, seq: int, done: bool) -> bool:
        """Add a chunk. Returns True when assembly is complete (done=True)."""
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
        self._expected_seq = 0


async def _transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> Optional[str]:
    """
    Run faster-whisper on raw int16 PCM bytes.
    Returns transcript string, or None on failure.
    """
    model = _load_whisper()
    if model is None:
        return None

    # Write to a temporary WAV file (faster-whisper accepts file paths or numpy arrays)
    import wave

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        # Write a minimal WAV header
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16_bytes)

        # Transcribe in thread executor (blocking call)
        def _run():
            segments, _info = model.transcribe(
                tmp_path,
                beam_size=5,
                language="en",
                vad_filter=True,  # filter out silence
            )
            return " ".join(seg.text for seg in segments).strip()

        transcript = await asyncio.get_event_loop().run_in_executor(None, _run)
        log.info("Transcript: %r", transcript[:100])
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
        """Fire a reminder after in_s seconds."""
        task = asyncio.create_task(self._fire(text, in_s))
        self._tasks.append(task)
        # Clean up completed tasks to avoid memory growth on long sessions
        self._tasks = [t for t in self._tasks if not t.done()]
        log.info("Reminder scheduled: %r in %d s", text, in_s)

    async def _fire(self, text: str, in_s: int) -> None:
        await asyncio.sleep(in_s)
        log.info("Reminder firing: %r", text)
        await self._send_frame({"notify": text[:120]})


# ---------------------------------------------------------------------------
# Serial auto-detection
# ---------------------------------------------------------------------------

def _find_serial_port() -> str:
    """Auto-detect /dev/ttyACM* (Linux USB-CDC). Env override takes priority."""
    if SERIAL_PORT:
        return SERIAL_PORT

    candidates = sorted(glob.glob("/dev/ttyACM*"))
    if candidates:
        log.info("Auto-detected serial port: %s", candidates[0])
        return candidates[0]

    # macOS USB serial fallback
    candidates = sorted(glob.glob("/dev/cu.usbmodem*"))
    if candidates:
        log.info("Auto-detected serial port: %s", candidates[0])
        return candidates[0]

    raise RuntimeError(
        "No serial port found. Set ENGRAM_SERIAL_PORT env var to the device path."
    )


# ---------------------------------------------------------------------------
# Main bridge loop
# ---------------------------------------------------------------------------

class EngramBridge:
    def __init__(self, port: str, baud: int):
        self._port = port
        self._baud = baud
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._http = httpx.AsyncClient(timeout=None)  # per-request timeouts applied
        self._audio = AudioAccumulator()
        self._recording = False
        self._reminders: Optional[ReminderScheduler] = None
        self._send_lock = asyncio.Lock()  # serialize serial writes

    async def start(self) -> None:
        log.info("Opening serial %s @ %d baud", self._port, self._baud)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud
        )
        self._reminders = ReminderScheduler(self._send_frame)
        log.info("Engram bridge ready. Waiting for Cardputer commands...")

        # Pre-load Whisper in background so first audio request isn't slow
        asyncio.create_task(asyncio.get_event_loop().run_in_executor(None, _load_whisper))

        await self._read_loop()

    async def _send_frame(self, obj: dict) -> None:
        """Serialize obj as NDJSON and write to serial (thread-safe)."""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        async with self._send_lock:
            self._writer.write(encoded)
            await self._writer.drain()

    async def _read_loop(self) -> None:
        """Main read loop — never blocks; dispatches each line as a Task."""
        while True:
            try:
                raw_line = await self._reader.readline()
            except Exception as exc:
                log.error("Serial read error: %s — exiting", exc)
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

            # Audio chunks are high-frequency — handle inline to avoid task overhead
            if cmd == "audio":
                await self._handle_audio_chunk(msg)
            else:
                # All other commands are dispatched as independent tasks so the
                # read loop is never blocked by slow LLM or memory operations.
                asyncio.create_task(self._dispatch(cmd, msg))

    async def _dispatch(self, cmd: str, msg: dict) -> None:
        """Route a command to its handler. Exceptions are caught and reported."""
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
        """Store a brain-dump. Fast ack, then background tasks."""
        text = msg.get("text", "").strip()
        if not text:
            await self._send_frame({"err": "empty dump"})
            return

        await self._send_frame({"status": "SAVING..."})

        note_id = await _store_and_index(text)

        if note_id:
            await self._send_frame({"status": "SAVED"})
        else:
            await self._send_frame({"err": "save failed"})

    async def _handle_ask(self, msg: dict) -> None:
        """Answer a question using semantic search + LLM streaming."""
        query = msg.get("text", "").strip()
        if not query:
            await self._send_frame({"err": "empty question"})
            return

        await self._send_frame({"status": "THINKING"})

        link = await _build_context_and_answer(query, self._http, self._send_frame)

        if link:
            await self._send_frame({"link": link})

    async def _handle_remind(self, msg: dict) -> None:
        """Schedule a time-based reminder."""
        text = msg.get("text", "").strip()
        in_s = msg.get("in_s", 0)
        if not text:
            await self._send_frame({"err": "empty reminder"})
            return
        if not isinstance(in_s, (int, float)) or in_s <= 0:
            await self._send_frame({"err": "in_s must be positive int"})
            return

        self._reminders.schedule(text, int(in_s))
        await self._send_frame({"status": f"REM {int(in_s)}s"})

    async def _handle_rec_start(self) -> None:
        """Begin a push-to-talk recording session."""
        self._audio.reset()
        self._recording = True
        await self._send_frame({"status": "REC..."})
        log.info("Recording started")

    async def _handle_rec_end(self) -> None:
        """End recording; Cardputer will now stream audio chunks."""
        self._recording = False
        # Audio chunks arrive after rec_end — accumulator already active.
        # Nothing to do here; _handle_audio_chunk drives the rest.
        log.info("Recording ended — awaiting audio chunks")

    async def _handle_audio_chunk(self, msg: dict) -> None:
        """
        Accumulate a base64 PCM16 chunk.
        When done=True: evict Ollama VRAM, transcribe, then treat as dump+ask.
        """
        b64 = msg.get("b64", "")
        seq = msg.get("seq", 0)
        done = bool(msg.get("done", False))

        complete = self._audio.add_chunk(b64, seq, done)

        if not complete:
            return  # Still accumulating

        # All chunks received
        await self._send_frame({"status": "TRANSCRIBING"})
        log.info("Audio complete — starting transcription")

        # Evict Ollama from VRAM so Whisper can use the GPU
        await _evict_ollama_vram(self._http)

        pcm16_bytes = self._audio.get_pcm16_bytes()
        self._audio.reset()

        transcript = await _transcribe(pcm16_bytes)

        if not transcript:
            await self._send_frame({"err": "transcription failed"})
            return

        log.info("Transcript ready (%d chars): %r", len(transcript), transcript[:80])

        # Treat transcript as a brain-dump (store) AND answer any implicit question
        await self._send_frame({"status": "THINKING"})

        # Store the transcript as a note (fire-and-forget internal tasks)
        asyncio.create_task(_store_and_index(transcript, source="mic"))

        # Stream an LLM response that acknowledges + reflects on the transcript
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Engram, an offline personal second-brain assistant. "
                    "The user just spoke a voice note. Acknowledge the key points briefly "
                    "(1-2 sentences) and note if anything connects to their prior notes. "
                    "Keep it short for a small screen."
                ),
            },
            {"role": "user", "content": f"Voice note: {transcript}"},
        ]

        await _stream_chat(self._http, messages, self._send_frame)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    port = _find_serial_port()
    bridge = EngramBridge(port=port, baud=SERIAL_BAUD)
    try:
        await bridge.start()
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
