"""store.py — append captures to data/captures.md as timestamped Markdown.

Pure storage helper (no serial, no AI). One growing human-readable file:

    # captures

    - **2026-06-20 15:10** _(voice)_ — remote work makes me focus better
    - **2026-06-20 15:12** _(keyboard)_ — call Dr. Patel about results
"""
from __future__ import annotations

import datetime
import pathlib

# data/captures.md lives at the project root, next to host/
CAPTURES = pathlib.Path(__file__).resolve().parent.parent / "data" / "captures.md"


def append_note(text: str, src: str = "keyboard") -> int:
    """Append one capture; return the new total count. Blank text is ignored."""
    text = " ".join(text.split())  # collapse whitespace/newlines
    if not text:
        return count_notes()
    CAPTURES.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CAPTURES.exists()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with CAPTURES.open("a", encoding="utf-8") as f:
        if new_file:
            f.write("# captures\n\n")
        f.write(f"- **{ts}** _({src})_ — {text}\n")
    return count_notes()


def count_notes() -> int:
    """Number of captures stored so far."""
    if not CAPTURES.exists():
        return 0
    return sum(
        1
        for line in CAPTURES.read_text(encoding="utf-8").splitlines()
        if line.startswith("- **")
    )
