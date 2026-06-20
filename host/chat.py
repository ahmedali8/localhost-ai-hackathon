"""chat.py — terminal chat over your captures. Cognee graph retrieval + local Exo answer.

    uv run python host/chat.py "what did I do today?"
    uv run python host/chat.py            # interactive, Ctrl-D to exit

Cognee retrieves the relevant notes from the knowledge graph (CHUNKS search — pure
vector retrieval, no LLM); Exo writes the answer from them. Fully offline.

Why not Cognee's own recall synthesis? Its OpenAI adapter forces instructor
"json_schema_mode", which routes through tool-calls that Exo's small MLX model
can't emit ("Instructor does not support multiple tool calls"). So we retrieve
here and hand the notes to Exo directly, which answers cleanly.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("COGNEE_MCP_URL", "http://127.0.0.1:8000/mcp")
EXO = os.environ.get("EXO_ENDPOINT", "http://127.0.0.1:52415/v1")
MODEL = os.environ.get("EXO_MODEL", "mlx-community/Qwen3.5-2B-MLX-8bit")
DATASET = "captures"
T = timedelta(seconds=300)  # Exo is slow; be patient

SYSTEM = (
    "You answer questions about the user's own captured notes. "
    "Use only the notes provided as context; if they don't cover it, say so. "
    "Be concise and direct. /no_think"  # /no_think disables Qwen's chain-of-thought
)


async def retrieve(session: ClientSession, question: str) -> str:
    """Pull the notes most relevant to the question from the Cognee graph."""
    res = await session.call_tool(
        "recall", {"query": question, "search_type": "CHUNKS", "datasets": DATASET}
    )
    text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "text", ""))
    return text.replace("[graph]", "").strip()


def synthesize(question: str, context: str) -> str:
    """Ask Exo to answer the question grounded in the retrieved notes."""
    r = httpx.post(
        f"{EXO}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Notes:\n{context}\n\nQuestion: {question}"},
            ],
            "temperature": 0,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


async def answer(session: ClientSession, question: str) -> str:
    context = await retrieve(session, question)
    if not context:
        return "(nothing captured yet — record a note first)"
    return synthesize(question, context)


async def main() -> None:
    async with streamablehttp_client(MCP_URL, timeout=T, sse_read_timeout=T) as (r, w, _):
        async with ClientSession(r, w, read_timeout_seconds=T) as s:
            await s.initialize()

            if len(sys.argv) > 1:  # one-shot
                print(await answer(s, " ".join(sys.argv[1:])))
                return

            print("chat over your captures — Ctrl-D to exit")
            while True:
                try:
                    q = input("\nyou> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if q:
                    print("ai>", await answer(s, q))


if __name__ == "__main__":
    asyncio.run(main())
