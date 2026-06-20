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


def read_notes() -> list[tuple[str, str, str]]:
    """Return every capture as ``(timestamp, source, text)``, oldest first.

    Parses the same line format ``append_note`` writes:
    ``- **<ts>** _(<src>)_ — <text>``. Malformed lines are skipped. This keeps
    the store's format owned in one place (reindex.py consumes this, not the raw
    Markdown).
    """
    if not CAPTURES.exists():
        return []
    notes: list[tuple[str, str, str]] = []
    for line in CAPTURES.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- **") or " — " not in line:
            continue
        prefix, text = line.split(" — ", 1)
        try:
            ts = prefix.split("**", 2)[1]
            src = prefix.split("_(", 1)[1].split(")_", 1)[0]
        except IndexError:
            continue
        notes.append((ts, src, text.strip()))
    return notes
