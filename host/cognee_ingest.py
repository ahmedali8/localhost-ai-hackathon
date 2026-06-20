"""cognee_ingest.py — push captures into the running cognee-mcp knowledge graph.

Used two ways:
  * CLI, to (re)build the graph from the markdown store:
        uv run python host/cognee_ingest.py            # add captures.md to the graph
        uv run python host/cognee_ingest.py --reset    # wipe the dataset first, then add
  * module: bridge.py calls remember() to index each capture live.

Drives the official cognee-mcp server over MCP Streamable HTTP. The markdown
store (data/captures.md) stays the source of truth; this is only the index, so
--reset + re-ingest always rebuilds it cleanly.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8000/mcp"
DATASET = "captures"
STORE = pathlib.Path(__file__).resolve().parent.parent / "data" / "captures.md"
T = timedelta(seconds=600)  # Exo cognify (LLM graph extraction) is slow


async def _call(tool: str, args: dict) -> str:
    async with streamablehttp_client(MCP_URL, timeout=T, sse_read_timeout=T) as (r, w, _):
        async with ClientSession(r, w, read_timeout_seconds=T) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return "\n".join(
                getattr(c, "text", "") for c in res.content if getattr(c, "text", "")
            ).strip()


def remember(text: str, dataset: str = DATASET) -> str:
    """Add + cognify one piece of text into the graph. Blocking (own event loop)."""
    text = (text or "").strip()
    if not text:
        return ""
    return asyncio.run(_call("remember", {"data": text, "dataset_name": dataset}))


def reset(dataset: str = DATASET) -> str:
    """Wipe the dataset (relational + graph + vector) so a re-ingest starts clean."""
    return asyncio.run(_call("forget", {"dataset": dataset}))


def main() -> None:
    if "--reset" in sys.argv:
        try:
            print("[ingest] wiping dataset ->", reset() or "(ok)")
        except Exception as exc:  # noqa: BLE001 — empty/absent dataset is fine
            print(f"[ingest] reset skipped: {exc}")

    notes = STORE.read_text(encoding="utf-8")
    print(f"[ingest] remember() — add + cognify {len(notes)} chars (Exo extraction, slow)...")
    print("[ingest] ->", remember(notes) or "(ok)")


if __name__ == "__main__":
    main()
