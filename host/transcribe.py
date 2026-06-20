"""transcribe.py — WAV → text via mlx-whisper (Apple-Silicon, offline after first run).

First call downloads the model (do it once online). Set WHISPER_MODEL to change it.
"""
from __future__ import annotations

import os

MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small")


def transcribe_wav(path: str) -> str:
    import mlx_whisper  # imported lazily so the bridge runs even before it's installed

    result = mlx_whisper.transcribe(path, path_or_hf_repo=MODEL)
    return (result.get("text") or "").strip()
