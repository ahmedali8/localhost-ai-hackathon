"""
engram_brain.py — Offline second-brain memory/retrieval module for Engram.

Architecture (per probe):
  PRIMARY PATH (always on):
    sentence-transformers/all-MiniLM-L6-v2 (CPU) → ChromaDB (persistent)
    + lightweight networkx graph (pickle) populated by a small-model JSON extractor

  BONUS PATH (feature-flagged, fire-and-forget):
    Cognee graph layer (cognee.add / cognee.cognify / cognee.search)
    Falls back silently to the primary path on any error or timeout.

Public API:
  store_memory(text, source) -> note_id (str)
  search(query, k)          -> list of {"text", "score", "meta"} dicts
  find_links(text)          -> "A -> B" string (strongest prior connection) or ""
  answer(query, infer_fn)   -> generator of str tokens (RAG: retrieve + call infer_fn)

All state persists across restarts in:
  ENGRAM_VECTORS_PATH   (.engram_vectors/)    — ChromaDB collection
  ENGRAM_GRAPH_PATH     (.engram_graph.pkl)   — networkx DiGraph
  COGNEE system dirs    (.cognee_system/ etc) — managed by Cognee internally

Set USE_COGNEE=true in env to enable the Cognee bonus path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import pickle
import re
import threading
import time
import uuid
from typing import Callable, Generator, Iterator, Optional

import httpx
import networkx as nx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("engram_brain")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# ---------------------------------------------------------------------------
# Configuration (from env, with sane defaults)
# ---------------------------------------------------------------------------

# Where ChromaDB stores its files
VECTORS_PATH = os.environ.get("ENGRAM_VECTORS_PATH", ".engram_vectors")

# Where the custom networkx graph pickle lives
GRAPH_PATH = pathlib.Path(os.environ.get("ENGRAM_GRAPH_PATH", ".engram_graph.pkl"))

# Ollama / Exo endpoint for entity extraction (and later RAG)
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Model name used for entity extraction and RAG answering
LLM_MODEL = os.environ.get("LLM_MODEL", "engram-llm")

# sentence-transformers cache dir (pre-downloaded before going offline)
ST_HOME = os.environ.get("SENTENCE_TRANSFORMERS_HOME", ".st_cache")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", ST_HOME)

# Feature flag: enable Cognee bonus layer
USE_COGNEE = os.environ.get("USE_COGNEE", "false").lower() in ("1", "true", "yes")

# Timeouts
EXTRACTION_TIMEOUT = float(os.environ.get("ENGRAM_EXTRACT_TIMEOUT", "15"))
COGNEE_TIMEOUT = float(os.environ.get("ENGRAM_COGNEE_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# ChromaDB + sentence-transformers setup (PRIMARY PATH)
# ---------------------------------------------------------------------------

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    from chromadb.utils import embedding_functions as chroma_ef
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    log.error("chromadb not installed — pip install chromadb sentence-transformers")

if _CHROMA_AVAILABLE:
    _chroma_client = chromadb.PersistentClient(
        path=VECTORS_PATH,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    # SentenceTransformerEmbeddingFunction handles the model load internally.
    # model_name will be fetched from cache (SENTENCE_TRANSFORMERS_HOME) offline.
    _embedding_fn = chroma_ef.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2",
        device="cpu",  # always CPU per probe — keeps VRAM free for Whisper + LLM
    )
    _collection = _chroma_client.get_or_create_collection(
        name="engram_notes",
        embedding_function=_embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )
    log.info("ChromaDB ready at %s (collection: engram_notes, %d docs)", VECTORS_PATH, _collection.count())
else:
    _collection = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Cognee setup (BONUS PATH — only if USE_COGNEE=true)
# ---------------------------------------------------------------------------

_COGNEE_READY = False

if USE_COGNEE:
    try:
        import cognee

        # Patch: skip the slow LLM pre-flight health-check on first run
        # (cognee issue #2119 — blocks on slow 7B models at startup)
        try:
            cognee.config._first_run_done = True  # type: ignore[attr-defined]
        except Exception:
            pass

        # Apply .env values to Cognee config programmatically as a safety net
        # (Cognee also reads from the .env file if it's in CWD)
        _llm_endpoint = os.environ.get("LLM_ENDPOINT", f"{OLLAMA_URL}/v1")
        _embed_endpoint = os.environ.get("EMBEDDING_ENDPOINT", f"{OLLAMA_URL}/api/embed")

        cognee.config.set_llm_config(  # type: ignore[attr-defined]
            {
                "llm_provider": os.environ.get("LLM_PROVIDER", "ollama"),
                "llm_model": os.environ.get("LLM_MODEL", "engram-llm"),
                "llm_endpoint": _llm_endpoint,
                "llm_api_key": os.environ.get("LLM_API_KEY", "ollama"),
                "llm_max_completion_tokens": int(os.environ.get("LLM_MAX_COMPLETION_TOKENS", "4096")),
            }
        )
        cognee.config.set_vector_db_config(  # type: ignore[attr-defined]
            {
                "vector_db_provider": os.environ.get("VECTOR_DB_PROVIDER", "lancedb"),
            }
        )

        _COGNEE_READY = True
        log.info("Cognee bonus layer ENABLED")
    except Exception as exc:
        log.warning("Cognee import/config failed (%s) — running without graph bonus", exc)
        USE_COGNEE = False

# ---------------------------------------------------------------------------
# NetworkX graph helpers (lightweight entity graph — custom extractor)
# ---------------------------------------------------------------------------

_GRAPH_LOCK = threading.Lock()

_ENTITY_EXTRACTION_PROMPT = """\
You are a knowledge graph extractor. Given a text note, return ONLY valid JSON.
No explanation. No markdown. Only the JSON object.

Schema:
{
  "entities": [{"name": "string", "type": "PERSON|PLACE|CONCEPT|EVENT|OBJECT"}],
  "relations": [{"subject": "string", "predicate": "string", "object": "string"}]
}

Rules:
- Extract 1-5 entities max.
- Extract 0-5 relations max.
- Use short, lowercase predicate verbs (e.g. "relates_to", "causes", "is_part_of").
- If nothing is extractable, return {"entities": [], "relations": []}.
"""


def _load_graph() -> nx.DiGraph:
    """Load the graph from disk (or return a fresh one)."""
    with _GRAPH_LOCK:
        if GRAPH_PATH.exists():
            try:
                return pickle.loads(GRAPH_PATH.read_bytes())
            except Exception as exc:
                log.warning("Graph pickle corrupt (%s) — starting fresh", exc)
        return nx.DiGraph()


def _save_graph(g: nx.DiGraph) -> None:
    with _GRAPH_LOCK:
        GRAPH_PATH.write_bytes(pickle.dumps(g))


def _extract_and_store(note_id: str, text: str) -> bool:
    """
    Call the local LLM to extract entities/relations and store them in the graph.
    Always returns True/False — NEVER raises. Caller does not block on this.
    """
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "stream": False,
                "options": {"num_ctx": 2048, "temperature": 0},
                "messages": [
                    {"role": "system", "content": _ENTITY_EXTRACTION_PROMPT},
                    {"role": "user", "content": text[:1500]},  # hard cap to stay in ctx
                ],
            },
            timeout=EXTRACTION_TIMEOUT,
        )
        raw = resp.json()["message"]["content"]
        # Strip accidental markdown code fences
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        data = json.loads(raw)
    except Exception as exc:
        log.debug("Entity extraction failed for %s: %s", note_id, exc)
        return False

    try:
        g = _load_graph()

        # Add the note itself as a node
        g.add_node(note_id, label="NOTE", text=text[:200])

        # Add entity nodes and link them to the note
        entity_names: set[str] = set()
        for ent in data.get("entities", [])[:5]:
            ename = ent["name"].lower()
            entity_names.add(ename)
            if not g.has_node(ename):
                g.add_node(ename, label=ent.get("type", "CONCEPT"))
            g.add_edge(note_id, ename, relation="mentions")

        # Add relation edges between entity pairs
        for rel in data.get("relations", [])[:5]:
            s = rel.get("subject", "").lower()
            o = rel.get("object", "").lower()
            pred = rel.get("predicate", "relates_to").lower()
            # Only add edge if both nodes exist (avoids phantom nodes)
            if s in g and o in g:
                g.add_edge(s, o, relation=pred)

        _save_graph(g)
        log.debug("Graph updated for note %s: %d entities", note_id, len(entity_names))
        return True
    except Exception as exc:
        log.debug("Graph write failed for %s: %s", note_id, exc)
        return False


def _fire_and_forget_extract(note_id: str, text: str) -> None:
    """Spawn a daemon thread for entity extraction — never blocks the caller."""
    t = threading.Thread(
        target=_extract_and_store,
        args=(note_id, text),
        daemon=True,
        name=f"extract-{note_id[:8]}",
    )
    t.start()


# ---------------------------------------------------------------------------
# Cognee fire-and-forget helper
# ---------------------------------------------------------------------------

async def _cognee_add_and_cognify(note_id: str, text: str) -> None:
    """
    Add text to Cognee and run cognify() with a hard timeout.
    Errors and timeouts are swallowed — embedding path is the source of truth.
    """
    if not _COGNEE_READY:
        return
    try:
        import cognee
        await cognee.add(text, node_id=note_id)
        await asyncio.wait_for(cognee.cognify(), timeout=COGNEE_TIMEOUT)
        log.debug("Cognee cognify() succeeded for note %s", note_id)
    except asyncio.TimeoutError:
        log.debug("Cognee cognify() timed out for note %s — continuing", note_id)
    except Exception as exc:
        log.debug("Cognee bonus path error for %s: %s", note_id, exc)


def _fire_and_forget_cognee(note_id: str, text: str) -> None:
    """Run Cognee in a background thread (its own event loop) — non-blocking."""
    if not USE_COGNEE:
        return

    def _run() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_cognee_add_and_cognify(note_id, text))
        except Exception as exc:
            log.debug("Cognee background thread error: %s", exc)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True, name=f"cognee-{note_id[:8]}")
    t.start()


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------


def store_memory(text: str, source: str = "cardputer") -> str:
    """
    Chunk, embed, and persist a brain-dump note.

    Steps:
      1. Add to ChromaDB (always-on, synchronous, returns immediately).
      2. Fire-and-forget entity extraction → networkx graph (background thread).
      3. Fire-and-forget Cognee cognify() (background thread, only if USE_COGNEE).

    Returns the new note_id (UUID string).
    """
    if not _CHROMA_AVAILABLE or _collection is None:
        raise RuntimeError("ChromaDB not available — check installation")

    note_id = str(uuid.uuid4())
    ts = int(time.time())

    # --- 1. Embed and store (always synchronous) ---
    # For large notes, chunk into overlapping 512-char windows so retrieval
    # granularity is fine-grained. Each chunk gets its own vector.
    chunks = _chunk_text(text, chunk_size=512, overlap=64)

    chunk_ids = [f"{note_id}#{i}" for i in range(len(chunks))]
    metadatas = [
        {"source": source, "ts": ts, "note_id": note_id, "chunk": i}
        for i in range(len(chunks))
    ]
    _collection.add(documents=chunks, ids=chunk_ids, metadatas=metadatas)
    log.info("Stored note %s (%d chunk(s)) from %s", note_id, len(chunks), source)

    # --- 2. Background entity extraction → graph ---
    _fire_and_forget_extract(note_id, text)

    # --- 3. Background Cognee bonus layer ---
    _fire_and_forget_cognee(note_id, text)

    return note_id


def search(query: str, k: int = 5) -> list[dict]:
    """
    Return the top-k most semantically similar stored chunks.

    Each result dict: {"text": str, "score": float (0–1), "meta": dict}
    Score is cosine similarity (1 = identical, 0 = orthogonal).

    Falls through to Cognee CHUNKS search as a bonus if USE_COGNEE is on,
    but always returns the ChromaDB results (Cognee results are appended,
    deduped by note_id).
    """
    if not _CHROMA_AVAILABLE or _collection is None:
        return []

    # Primary: ChromaDB cosine similarity
    n = min(k, _collection.count())
    if n == 0:
        return []

    results = _collection.query(query_texts=[query], n_results=n)
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    distances = results["distances"][0]

    primary = [
        {"text": d, "score": max(0.0, 1.0 - dist), "meta": m}
        for d, m, dist in zip(docs, metas, distances)
    ]

    # Bonus: Cognee CHUNKS search (non-blocking attempt)
    if USE_COGNEE and _COGNEE_READY:
        cognee_hits = _cognee_search_sync(query, k=k)
        if cognee_hits:
            seen_ids = {r["meta"].get("note_id") for r in primary}
            for hit in cognee_hits:
                nid = hit.get("note_id", "")
                if nid not in seen_ids:
                    primary.append({"text": hit["text"], "score": hit.get("score", 0.0), "meta": {"note_id": nid, "source": "cognee"}})
                    seen_ids.add(nid)

    return primary[:k]


def find_links(text: str) -> str:
    """
    Given a piece of text (e.g. a freshly typed note or query), return the
    single strongest related prior memory as a short "A -> B" string.

    Uses cosine similarity across all stored embeddings.
    Returns "" if no memories are stored yet.

    This string is intended for the {"link": "A -> B"} serial frame.
    """
    if not _CHROMA_AVAILABLE or _collection is None or _collection.count() == 0:
        return ""

    hits = _collection.query(query_texts=[text], n_results=2)
    docs = hits["documents"][0]
    distances = hits["distances"][0]

    if not docs:
        return ""

    # Skip an exact/near-exact match (self) — score > 0.99
    candidates = [
        (doc, 1.0 - dist)
        for doc, dist in zip(docs, distances)
        if (1.0 - dist) < 0.99
    ]

    if not candidates:
        # If only one doc and it's the same text, still return it
        candidates = [(docs[0], 1.0 - distances[0])]

    best_doc, best_score = candidates[0]
    if best_score < 0.20:
        # Too dissimilar — not a meaningful connection
        return ""

    # Truncate both sides to short labels for the display frame
    a_label = _short_label(text)
    b_label = _short_label(best_doc)
    return f"{a_label} -> {b_label}"


def answer(query: str, infer_fn: Callable[[str, list[dict]], Iterator[str]]) -> Generator[str, None, None]:
    """
    RAG: retrieve top-k context chunks, then call infer_fn to generate an answer.

    infer_fn signature:
        def infer_fn(query: str, context_chunks: list[dict]) -> Iterator[str]:
            # yields token strings one by one

    This generator yields token strings as they arrive from infer_fn.
    Callers can write each token to serial as a {"t": "<token>"} frame.

    Example infer_fn (Ollama streaming):

        def infer_fn(query, chunks):
            context = "\\n---\\n".join(c["text"] for c in chunks)
            prompt = f"Context:\\n{context}\\n\\nQuestion: {query}"
            with httpx.stream("POST", f"{OLLAMA_URL}/api/generate",
                              json={"model": LLM_MODEL, "prompt": prompt, "stream": True},
                              timeout=60) as r:
                for line in r.iter_lines():
                    if line:
                        data = json.loads(line)
                        if token := data.get("response"):
                            yield token
                        if data.get("done"):
                            break
    """
    context_chunks = search(query, k=5)
    if not context_chunks:
        log.info("No context found for query: %s", query[:80])

    yield from infer_fn(query, context_chunks)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """
    Split text into overlapping chunks by character count.
    For short texts (< chunk_size), returns a single chunk.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # Try to break at a sentence/word boundary
        if end < len(text):
            last_period = chunk.rfind(". ")
            last_space = chunk.rfind(" ")
            break_at = last_period + 1 if last_period > chunk_size // 2 else last_space
            if break_at > 0:
                chunk = text[start : start + break_at]
                end = start + break_at
        chunks.append(chunk.strip())
        start = end - overlap
    return [c for c in chunks if c]


def _short_label(text: str, max_chars: int = 40) -> str:
    """Return a truncated first-sentence label for display in link frames."""
    text = text.strip()
    # Try first sentence
    period = text.find(". ")
    snippet = text[: period + 1] if 0 < period < max_chars else text[:max_chars]
    snippet = snippet.strip().rstrip(".")
    return snippet + ("..." if len(text) > max_chars else "")


def _cognee_search_sync(query: str, k: int = 5) -> list[dict]:
    """
    Blocking wrapper for Cognee async search, run in a fresh event loop.
    Returns [] on any error or timeout.
    Uses SearchType.CHUNKS — the only Cognee search type that does NOT require
    a healthy graph (it falls through to the LanceDB vector store).
    """
    try:
        import cognee
        from cognee.api.v1.search import SearchType  # type: ignore[import]

        loop = asyncio.new_event_loop()
        try:
            coro = asyncio.wait_for(
                cognee.search(SearchType.CHUNKS, query_text=query),
                timeout=5.0,  # short timeout — primary path does not wait for this
            )
            raw = loop.run_until_complete(coro)
        finally:
            loop.close()

        if not raw:
            return []

        hits = []
        for item in raw[:k]:
            # Cognee returns various object shapes; normalize defensively
            if hasattr(item, "payload"):
                payload = item.payload or {}
                hits.append({
                    "text": payload.get("text", str(item)),
                    "score": getattr(item, "score", 0.5),
                    "note_id": payload.get("node_id", ""),
                })
            elif isinstance(item, dict):
                hits.append({
                    "text": item.get("text", item.get("content", str(item))),
                    "score": item.get("score", 0.5),
                    "note_id": item.get("node_id", item.get("id", "")),
                })
        return hits
    except Exception as exc:
        log.debug("Cognee search skipped: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Graph query (public — available for debug / display in serial frames)
# ---------------------------------------------------------------------------


def graph_neighbors(entity: str, hops: int = 2) -> list[str]:
    """
    Return up to 20 node names reachable within `hops` from `entity` in the
    custom networkx graph. Returns [] if entity not found.
    Useful for building the "connections" overlay in the UI.
    """
    g = _load_graph()
    ename = entity.lower()
    if ename not in g:
        return []

    reachable: set[str] = set()
    frontier = {ename}
    for _ in range(hops):
        next_f: set[str] = set()
        for node in frontier:
            next_f.update(g.successors(node))
            next_f.update(g.predecessors(node))
        frontier = next_f - reachable - {ename}
        reachable.update(frontier)

    return list(reachable)[:20]


def graph_stats() -> dict:
    """Return basic stats about the in-memory graph (for debug logging)."""
    g = _load_graph()
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()}


# ---------------------------------------------------------------------------
# Self-test (run with: python engram_brain.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=== Engram Brain Self-Test ===")

    if not _CHROMA_AVAILABLE:
        print("ERROR: chromadb not installed. Run: pip install chromadb sentence-transformers")
        sys.exit(1)

    # Store a couple of test memories
    id1 = store_memory("The Cardputer uses an ESP32-S3 with a PDM microphone.", source="test")
    print(f"Stored note 1: {id1}")

    id2 = store_memory("Whisper large-v3 at int8_float16 uses about 3-4 GB of VRAM on the RTX-3050.", source="test")
    print(f"Stored note 2: {id2}")

    id3 = store_memory("Cognee builds a knowledge graph from text using an LLM extractor.", source="test")
    print(f"Stored note 3: {id3}")

    # Allow background threads a moment to run
    time.sleep(1.0)

    # Semantic search
    results = search("microphone audio recording", k=2)
    print(f"\nSearch 'microphone audio recording' -> {len(results)} result(s):")
    for r in results:
        print(f"  [{r['score']:.3f}] {r['text'][:80]}")

    # find_links
    link = find_links("I need to run speech recognition on the GPU")
    print(f"\nfind_links('I need to run speech recognition...') -> '{link}'")

    # Graph stats
    gs = graph_stats()
    print(f"\nGraph: {gs['nodes']} nodes, {gs['edges']} edges")

    # Inline RAG demo (no LLM call — just prints retrieved context)
    def mock_infer(query: str, chunks: list[dict]) -> Iterator[str]:
        yield "[mock] Context retrieved:\n"
        for c in chunks:
            yield f"  - {c['text'][:60]}\n"
        yield f"[mock] Would answer: {query}\n"

    print("\nRAG answer test:")
    for token in answer("How much VRAM does Whisper use?", mock_infer):
        print(token, end="", flush=True)

    print("\n\n=== Self-test complete ===")
