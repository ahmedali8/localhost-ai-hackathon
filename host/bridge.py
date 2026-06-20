#!/usr/bin/env python3
"""bridge.py — host-side serial link to the Cardputer capture terminal.

The program you run on the Mac. It owns the USB-serial connection:
reads newline-delimited JSON from the device, routes it, and sends short
status frames back for the screen.

M1 (this file): keyboard notes only.
    device -> {"type":"note","src":"keyboard","text":"..."}
    host   -> {"status":"SAVED"} {"echo":"..."} {"count":N}
M2 will add: rec_start / audio / rec_end  ->  transcribe -> store as (voice).

Run:  uv run python host/bridge.py
Env:  CAP_PORT  serial path (default: auto-detect /dev/cu.usbmodem*)
      CAP_BAUD  baud (default 115200; bump to 921600 for the audio phase)
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

# allow `from store import ...` when run as `python host/bridge.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # pyserial

from store import append_note, count_notes

BAUD = int(os.environ.get("CAP_BAUD", "115200"))


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


def handle(ser: "serial.Serial", msg: dict) -> None:
    kind = msg.get("type")
    if kind == "note":
        text = msg.get("text", "")
        src = msg.get("src", "keyboard")
        total = append_note(text, src)
        print(f"[saved/{src}] {text!r}  (total {total})")
        send(ser, {"status": "SAVED"})
        send(ser, {"echo": text})
        send(ser, {"count": total})
    else:
        print(f"[ignored] {msg}")


def main() -> None:
    port = find_port()
    print(f"[bridge] connecting {port} @ {BAUD}")
    ser = serial.Serial(port, BAUD, timeout=0.2)
    time.sleep(0.3)  # let the device settle after the port opens
    send(ser, {"status": "idle"})
    send(ser, {"count": count_notes()})
    print(f"[bridge] ready — {count_notes()} captures so far. Type on the Cardputer.")

    buf = b""
    while True:
        buf += ser.read(256)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"[bad json] {line!r}")
                continue
            handle(ser, msg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
