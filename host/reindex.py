"""reindex.py — rebuild the Cognee graph from the Markdown store.

The Markdown file (data/captures.md) is the source of truth; the graph is just
an index. This wipes the current graph + vector store and re-ingests every
capture. Use it after editing the store by hand, recovering from a corrupt
graph, or changing the embedding/LLM configuration.

    uv run python host/reindex.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain
import store


def main() -> None:
    notes = store.read_notes()
    print(f"[reindex] {len(notes)} captures in the store")
    if not notes:
        print("[reindex] nothing to index")
        return

    print("[reindex] wiping existing graph + vectors...")
    brain.reset()

    # Tag each capture with its source + timestamp so the graph can reason about
    # when/how something was said.
    texts = [f"({src}, {ts}) {text}" for ts, src, text in notes]
    print(f"[reindex] ingesting {len(texts)} captures (single cognify pass)...")
    brain.ingest_many(texts)
    print("[reindex] done")


if __name__ == "__main__":
    main()
