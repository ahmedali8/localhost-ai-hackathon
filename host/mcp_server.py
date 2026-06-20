"""mcp_server.py — expose the captures knowledge graph over MCP.

Thin wrapper so an MCP client (Claude Code, or any agent) can read and write the
same persistent Cognee graph the device builds. All the real work — Exo LLM,
fastembed, ladybug graph store — lives in brain.py; this just publishes two
tools over stdio:

    ask_notes(question)  -> a graph-grounded answer from your captures
    ingest_note(text)    -> append a note to the store AND index it

Run standalone:  uv run python host/mcp_server.py   (speaks MCP over stdio)
Wired into Claude Code via .mcp.json at the project root.

brain's API is blocking (each call spins its own asyncio loop), so every tool
offloads to a worker thread with anyio — calling asyncio.run inside FastMCP's
running event loop would otherwise raise "cannot be called from a running loop".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anyio
from mcp.server.fastmcp import FastMCP

import brain
import store

mcp = FastMCP("captures")


@mcp.tool()
async def ask_notes(question: str) -> str:
    """Answer a question grounded in the user's captured notes (voice + keyboard).

    Backed by the local Cognee knowledge graph; fully offline. Returns plain text.
    """
    answer = await anyio.to_thread.run_sync(brain.ask, question)
    return answer or "(no answer — nothing relevant in the captures yet)"


@mcp.tool()
async def ingest_note(text: str) -> str:
    """Append a note to the capture store and index it into the knowledge graph."""
    text = (text or "").strip()
    if not text:
        return "(empty note ignored)"
    await anyio.to_thread.run_sync(store.append_note, text, "agent")
    await anyio.to_thread.run_sync(brain.ingest, text, "agent")
    return "stored + indexed"


if __name__ == "__main__":
    mcp.run()
