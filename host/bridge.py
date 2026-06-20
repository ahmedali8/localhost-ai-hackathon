#!/usr/bin/env python3
"""bridge.py — host-side serial link to the Cardputer capture terminal.

The program you run on the Mac. Owns the USB-serial connection: reads
newline-delimited JSON from the device, routes it, sends status frames back.

Handles:
  device -> {"type":"note","src":"keyboard","text":"..."}          (M1)
  device -> {"type":"rec_start"}                                   (M2)
  device -> {"type":"audio","seq":N,"b64":"<pcm16le base64>","last":bool}
  device -> {"type":"rec_end"}
  host   -> {"status":"..."} {"echo":"..."} {"count":N}

On rec_end it writes the recording to data/last_recording.wav (always, for
debugging the transport) and, if mlx-whisper is installed, transcribes it and
stores it as a (voice) capture. Without mlx-whisper it just reports the WAV.

Run:  uv run python host/bridge.py
Env:  CAP_PORT serial path (default auto /dev/cu.usbmodem*), CAP_BAUD (115200)
"""
from __future__ import annotations

import base64
import glob
import json
import os
import pathlib
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # pyserial

from store import append_note, count_notes

BAUD = int(os.environ.get("CAP_BAUD", "115200"))
SR = 16000  # audio sample rate (must match the firmware)
WAV_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "last_recording.wav"

_audio = bytearray()  # accumulates PCM16 bytes for the current recording


_index_lock = threading.Lock()  # serialize ingests: one Exo cognify at a time


def index_async(text: str, source: str) -> None:
    """Ingest a capture into the cognee-mcp knowledge graph in the background.

    remember() runs an Exo cognify (seconds), so it runs in a daemon thread — the
    serial loop must never stall — and behind a lock so two captures don't kick off
    concurrent cognifies that contend for the model. Best-effort: the Markdown store
    is the source of truth and `cognee_ingest.py --reset` can rebuild the graph later.
    """

    def run() -> None:
        with _index_lock:
            try:
                import cognee_ingest

                cognee_ingest.remember(text)
                print(f"[cognee] indexed ({source})")
            except Exception as exc:  # noqa: BLE001 — best-effort; never crash the bridge
                print(f"[cognee] index skipped: {exc}")

    threading.Thread(target=run, daemon=True).start()


def find_port() -> str:
    explicit = os.environ.get("CAP_PORT")
    if explicit:
        return explicit
    for pattern in ("/dev/cu.usbmodem*", "/dev/tty.usbmodem*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    sys.exit("No serial device found. Plug in the Cardputer, or set CAP_PORT=/dev/cu.usbmodemXXXX")


def send(ser: "serial.Serial", obj: dict) -> None:
    ser.write((json.dumps(obj) + "\n").encode())


def write_wav(pcm: bytes) -> float:
    WAV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(WAV_PATH), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return len(pcm) / 2 / SR  # seconds


def handle_rec_end(ser: "serial.Serial") -> None:
    pcm = bytes(_audio)
    _audio.clear()
    dur = write_wav(pcm)
    print(f"[audio] {len(pcm)} bytes, {dur:.1f}s -> {WAV_PATH.name}")
    if dur < 0.2:
        send(ser, {"status": "TOO SHORT"})
        send(ser, {"echo": "(no audio captured)"})
        return
    send(ser, {"status": "TRANSCRIBING"})
    try:
        import transcribe
        text = transcribe.transcribe_wav(str(WAV_PATH))
        if text:
            total = append_note(text, "voice")
            print(f"[saved/voice] {text!r}  (total {total})")
            index_async(text, "voice")
            send(ser, {"status": "SAVED"})
            send(ser, {"echo": text})
            send(ser, {"count": total})
        else:
            print("[voice] empty transcript")
            send(ser, {"status": "EMPTY"})
            send(ser, {"echo": "(no speech detected)"})
    except Exception as exc:  # mlx-whisper missing, model download, etc.
        print(f"[transcribe unavailable] {exc}")
        send(ser, {"status": f"WAV {dur:.0f}s OK"})
        send(ser, {"echo": "(audio saved; add mlx-whisper for STT)"})


def handle(ser: "serial.Serial", msg: dict) -> None:
    kind = msg.get("type")
    if kind == "note":
        text, src = msg.get("text", ""), msg.get("src", "keyboard")
        total = append_note(text, src)
        print(f"[saved/{src}] {text!r}  (total {total})")
        index_async(text, src)
        send(ser, {"status": "SAVED"})
        send(ser, {"echo": text})
        send(ser, {"count": total})
    elif kind == "rec_start":
        _audio.clear()
        print("[audio] recording...")
    elif kind == "audio":
        b64 = msg.get("b64", "")
        if b64:
            try:
                _audio.extend(base64.b64decode(b64))
            except Exception as exc:
                print(f"[audio decode err] {exc}")
    elif kind == "rec_end":
        handle_rec_end(ser)
    elif kind == "rec_cancel":
        _audio.clear()
        print("[audio] canceled — buffer dropped")
        send(ser, {"status": "canceled"})
    else:
        print(f"[ignored] {msg}")


def main() -> None:
    port = find_port()
    print(f"[bridge] connecting {port} @ {BAUD}")
    ser = serial.Serial(port, BAUD, timeout=0.2)
    time.sleep(0.3)
    send(ser, {"status": "idle"})
    send(ser, {"count": count_notes()})
    print(f"[bridge] ready — {count_notes()} captures. Type, or hold the side button to record.")

    buf = b""
    while True:
        buf += ser.read(4096)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"[bad json] {line[:80]!r}")
                continue
            handle(ser, msg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
