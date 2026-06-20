"""brain.py — Cognee knowledge-graph memory, wired to a local Exo LLM.

Every capture (keyboard note or voice transcript) is ingested into a Cognee
knowledge graph so the user can later *ask questions* of their own notes and get
answers grounded in what they actually said. Everything runs locally:

    LLM         -> the Exo cluster's OpenAI-compatible endpoint (Qwen on MLX)
    embeddings  -> fastembed (local ONNX, no network after first model fetch)
    graph DB    -> networkx (file-based)      vector DB -> lancedb (embedded)

Nothing leaves the machine.

Who calls what
    bridge.py    -> ingest()  (in a daemon thread, once per capture)
    ask.py       -> ask()     (CLI question -> grounded answer)
    reindex.py   -> reset() + ingest()  (rebuild the graph from the store)

Cognee's API is async; this module exposes blocking wrappers (asyncio.run) so
the synchronous bridge and CLIs can use it without managing an event loop. Each
wrapper runs its own short-lived loop — there is no shared/global loop to clash
with, which sidesteps the classic "coroutine was never awaited" foot-guns.

The markdown store remains the source of truth; Cognee is only an index. If the
graph is ever lost or corrupted, `reindex.py` rebuilds it from the store.
"""
from __future__ import annotations

import asyncio
import os
import pathlib

# --- Local-only configuration -------------------------------------------------
# Overridable via the environment, but every default points at on-device infra.

#: Exo's OpenAI-compatible base URL (the cluster's local inference endpoint).
EXO_ENDPOINT = os.environ.get("EXO_ENDPOINT", "http://127.0.0.1:52415/v1")
#: Exo model id used for graph extraction and answer synthesis.
EXO_MODEL = os.environ.get("EXO_MODEL", "mlx-community/Qwen3.5-2B-MLX-8bit")
#: Local fastembed model (downloaded once, then fully offline) and its width.
EMBED_MODEL = os.environ.get("COGNEE_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIMS = int(os.environ.get("COGNEE_EMBED_DIMS", "384"))
#: Cognee dataset all captures live under.
DATASET = "captures"

#: Keep Cognee's databases inside the project (its default lands in site-packages
#: and is wiped on every reinstall). data/ is already git-ignored.
_DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
_SYSTEM_DIR = _DATA / "cognee_system"
_DATA_DIR = _DATA / "cognee_data"

# Single local user — turn off Cognee's multi-tenant access control so calls
# don't require auth. Must be set before cognee initializes its config.
os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")

# Force instructor's plain-JSON mode for structured extraction. Cognee's OpenAI
# adapter defaults to "json_schema_mode", which needs native JSON-schema/tool
# support that Exo's small MLX models don't provide — extraction then fails and
# retries forever. JSON mode just asks for JSON in the message content, which
# Exo returns cleanly (its reasoning goes to a separate reasoning_content field).
os.environ.setdefault("LLM_INSTRUCTOR_MODE", "json_mode")

_configured = False


def _configure() -> None:
    """Point Cognee at Exo + local backends. Idempotent; safe to call per op."""
    global _configured
    if _configured:
        return
    import cognee

    _SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    cognee.config.system_root_directory(str(_SYSTEM_DIR))
    cognee.config.data_root_directory(str(_DATA_DIR))

    # LLM -> Exo. Cognee uses LiteLLM under the hood; an OpenAI-compatible custom
    # endpoint needs the "openai/" prefix on the model plus an explicit api_base.
    cognee.config.set_llm_provider("openai")
    cognee.config.set_llm_endpoint(EXO_ENDPOINT)
    cognee.config.set_llm_model(f"openai/{EXO_MODEL}")
    cognee.config.set_llm_api_key("x")  # Exo ignores the key; LiteLLM requires one

    # Embeddings -> local fastembed (ONNX). No endpoint/key: it runs in-process.
    cognee.config.set_embedding_provider("fastembed")
    cognee.config.set_embedding_model(EMBED_MODEL)
    cognee.config.set_embedding_dimensions(EMBED_DIMS)

    # Graph -> ladybug (Cognee's embedded local graph store, already installed).
    # kuzu (the older default) isn't pulled in, and networkx isn't a supported
    # provider in 1.1.x. ladybug needs no server and keeps everything on disk.
    cognee.config.set_graph_database_provider("ladybug")

    _configured = True


def _result_to_text(results) -> str:
    """Flatten Cognee's search results into a single human-readable answer."""
    parts: list[str] = []
    for r in results or []:
        # Results may be plain strings or SearchResult-like objects; be liberal.
        for attr in ("text", "answer", "content"):
            val = getattr(r, attr, None)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
                break
        else:
            parts.append(str(r).strip())
    return "\n".join(p for p in parts if p).strip()


# --- async core ---------------------------------------------------------------


async def _ingest(text: str) -> None:
    import cognee

    _configure()
    await cognee.add(text, dataset_name=DATASET)
    await cognee.cognify(datasets=[DATASET])


async def _ingest_many(texts: list[str]) -> None:
    import cognee

    _configure()
    for text in texts:
        await cognee.add(text, dataset_name=DATASET)
    await cognee.cognify(datasets=[DATASET])  # one extraction pass over the batch


async def _ask(question: str, top_k: int) -> str:
    import cognee
    from cognee import SearchType

    _configure()
    results = await cognee.search(
        query_text=question,
        query_type=SearchType.GRAPH_COMPLETION,  # graph-grounded answer via the LLM
        datasets=[DATASET],
        top_k=top_k,
    )
    return _result_to_text(results)


async def _reset() -> None:
    import cognee

    _configure()
    # Drop ingested data + the graph/vector contents, but DON'T pass
    # metadata=True: that wipes the users table too, and the very next add()
    # then fails with "Could not find user". Defaults keep the default user.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system()


# --- blocking wrappers (for the sync bridge / CLIs) ---------------------------


def ingest(text: str, source: str = "note") -> None:
    """Add one capture to the graph. `source` is accepted for call-site clarity.

    Blocks until cognify finishes, so callers that must not stall (the serial
    bridge) should run this in a background thread.
    """
    text = (text or "").strip()
    if not text:
        return
    asyncio.run(_ingest(text))


def ingest_many(texts: list[str]) -> None:
    """Add several captures, then cognify once. Used by reindex for a full rebuild."""
    texts = [t.strip() for t in texts if t and t.strip()]
    if texts:
        asyncio.run(_ingest_many(texts))


def ask(question: str, top_k: int = 10) -> str:
    """Answer a question grounded in the captured notes. Returns plain text."""
    return asyncio.run(_ask(question, top_k))


def reset() -> None:
    """Wipe the graph + vector store. Used by reindex before a full rebuild."""
    asyncio.run(_reset())
