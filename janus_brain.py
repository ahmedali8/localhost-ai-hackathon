"""
janus_brain.py — Janus contradiction-catcher brain.

Janus (Roman god of two faces) argues with your past self.
Users log DECISIONS / BELIEFS / CLAIMS + their reasons.
Janus proactively detects when a NEW entry CONTRADICTS, DRIFTS FROM, or
SUPERSEDES a PAST one, then surfaces it on the Cardputer.

Architecture
============
PRIMARY PATH (always-on, reliable):
  sentence-transformers/all-MiniLM-L6-v2 (CPU)
  + ChromaDB (persistent across restarts)
  + networkx DiGraph of Claim nodes / supports|contradicts|supersedes|relates-to edges

BONUS PATH (feature-flagged):
  Cognee graph layer — fire-and-forget; falls back silently.

Graph schema
============
Nodes: Claim {text, reason, stance, topic, timestamp, note_id}
Edges: supports | contradicts | supersedes | relates-to
       Each edge carries {ts, summary} attributes.

Contradiction loop (the Cosine offline-agent bounty pattern)
=============================================================
  perceive(new claim text)
  -> extract {claim, reason, stance, topic}      # small-model JSON prompt
  -> retrieve related prior claims (embeddings)
  -> reason: same topic + opposed stance?        # infer_fn judge call
  -> act: write 'contradicts' edge, return result
  -> store: ChromaDB vector + networkx node

Public API (called by janus_bridge.py)
=======================================
  async store_claim(text, infer_fn) -> dict | None
      dict is {"kind": "contradicts"|"echoes", "other": str, "summary": str}
      or None if no significant relation found.
  search(query, k) -> list of {"text", "score", "meta"} dicts
  async answer(query, infer_fn) -> AsyncGenerator[str, None]
      Handles:
        "what have I contradicted myself on?"  (walk 'contradicts' edges)
        "how has my view on X changed?"        (timeline of claims on topic X)
        general RAG over all claims

All state persists across restarts in:
  JANUS_VECTORS_PATH  (.janus_vectors/)   — ChromaDB
  JANUS_GRAPH_PATH    (.janus_graph.pkl)  — networkx DiGraph
  Cognee dirs         (.cognee_system/ etc) — managed by Cognee internally

Env vars
========
  JANUS_VECTORS_PATH     default .janus_vectors
  JANUS_GRAPH_PATH       default .janus_graph.pkl
  OLLAMA_HOST            default http://localhost:11434
  LLM_MODEL              default janus-llm
  SENTENCE_TRANSFORMERS_HOME default <module dir>/.st_cache (absolute)
  USE_COGNEE             default false
  JANUS_EXTRACT_TIMEOUT  default 15  (seconds)
  JANUS_JUDGE_TIMEOUT    default 20  (seconds; LLM contradiction judgement)
  JANUS_COGNEE_TIMEOUT   default 30  (seconds)
  JANUS_SIM_THRESHOLD    default 0.35 (min cosine sim to even consider a prior claim)
  JANUS_JUDGE_TOP_K      default 5   (how many prior claims to present to the judge)
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
from typing import AsyncGenerator, Callable, Optional

import httpx
import networkx as nx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("janus_brain")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VECTORS_PATH = os.environ.get("JANUS_VECTORS_PATH", ".janus_vectors")
GRAPH_PATH = pathlib.Path(os.environ.get("JANUS_GRAPH_PATH", ".janus_graph.pkl"))
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "janus-llm")

# Absolute default so the cache resolves the same regardless of launch CWD.
_ST_HOME_DEFAULT = str(pathlib.Path(__file__).resolve().parent / ".st_cache")
ST_HOME = os.environ.get("SENTENCE_TRANSFORMERS_HOME", _ST_HOME_DEFAULT)
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", ST_HOME)

USE_COGNEE = os.environ.get("USE_COGNEE", "false").lower() in ("1", "true", "yes")

EXTRACT_TIMEOUT = float(os.environ.get("JANUS_EXTRACT_TIMEOUT", "15"))
JUDGE_TIMEOUT = float(os.environ.get("JANUS_JUDGE_TIMEOUT", "20"))
COGNEE_TIMEOUT = float(os.environ.get("JANUS_COGNEE_TIMEOUT", "30"))

# Minimum cosine similarity (0-1) for a prior claim to be worth judging.
# Below this it's unlikely to be the same topic at all.
SIM_THRESHOLD = float(os.environ.get("JANUS_SIM_THRESHOLD", "0.35"))

# How many top similar prior claims to feed to the contradiction judge.
JUDGE_TOP_K = int(os.environ.get("JANUS_JUDGE_TOP_K", "5"))

# ---------------------------------------------------------------------------
# ChromaDB + sentence-transformers (PRIMARY PATH)
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
    _embedding_fn = chroma_ef.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2",
        device="cpu",  # keep VRAM free for Whisper + LLM; MiniLM is fast on CPU
    )
    _collection = _chroma_client.get_or_create_collection(
        name="janus_claims",
        embedding_function=_embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )
    log.info(
        "ChromaDB ready at %s (janus_claims: %d docs)",
        VECTORS_PATH,
        _collection.count(),
    )
else:
    _collection = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Cognee setup (BONUS PATH)
# ---------------------------------------------------------------------------

_COGNEE_READY = False

if USE_COGNEE:
    try:
        import cognee

        # Bypass slow LLM health-check on startup (cognee issue #2119)
        try:
            cognee.config._first_run_done = True  # type: ignore[attr-defined]
        except Exception:
            pass

        _llm_endpoint = os.environ.get("LLM_ENDPOINT", f"{OLLAMA_URL}/v1")
        cognee.config.set_llm_config(  # type: ignore[attr-defined]
            {
                "llm_provider": os.environ.get("LLM_PROVIDER", "ollama"),
                "llm_model": LLM_MODEL,
                "llm_endpoint": _llm_endpoint,
                "llm_api_key": os.environ.get("LLM_API_KEY", "ollama"),
                "llm_max_completion_tokens": int(
                    os.environ.get("LLM_MAX_COMPLETION_TOKENS", "4096")
                ),
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
        log.warning("Cognee init failed (%s) — running without it", exc)
        USE_COGNEE = False

# ---------------------------------------------------------------------------
# NetworkX graph helpers
# ---------------------------------------------------------------------------

# Graph lock protects concurrent pickle reads/writes from background threads.
_GRAPH_LOCK = threading.Lock()

# Valid edge types in the Janus schema.
EDGE_CONTRADICTS = "contradicts"
EDGE_SUPERSEDES = "supersedes"
EDGE_SUPPORTS = "supports"
EDGE_RELATES = "relates-to"


def _load_graph() -> nx.DiGraph:
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


def _add_claim_node(
    g: nx.DiGraph,
    note_id: str,
    claim: str,
    reason: str,
    stance: str,
    topic: str,
    ts: int,
) -> None:
    """Add or update a Claim node in the graph."""
    g.add_node(
        note_id,
        label="CLAIM",
        text=claim[:300],
        reason=reason[:200],
        stance=stance[:100],
        topic=topic[:100],
        timestamp=ts,
    )


def _add_edge(
    g: nx.DiGraph,
    src: str,
    dst: str,
    kind: str,
    summary: str,
    ts: int,
) -> None:
    """Add a directed edge between two claim nodes."""
    g.add_edge(src, dst, relation=kind, summary=summary[:120], ts=ts)


# ---------------------------------------------------------------------------
# LLM extraction prompt — parse claim metadata from raw text
# ---------------------------------------------------------------------------

_EXTRACT_CLAIM_PROMPT = """\
You are an analyst that extracts structured information from personal notes.
Given the user's text, return ONLY valid JSON. No markdown fences, no explanation.

Schema:
{
  "claim": "the core assertion or decision being made (1 sentence)",
  "reason": "the stated reason or justification (1 sentence, or '' if none)",
  "stance": "positive|negative|neutral — the user's position on the topic",
  "topic": "a 2-5 word phrase naming the subject (e.g. 'remote work', 'AI risk')"
}

Rules:
- "claim" must capture the DECISION or BELIEF, not the context or rambling.
- "stance" = 'positive' if the user is FOR/agrees/endorses the topic,
             'negative' if AGAINST/disagrees/rejects it,
             'neutral' if observing without taking a position.
- Keep "topic" short and reusable across notes (will be used for grouping).
- If the text is too vague to extract a claim, return:
  {"claim": "", "reason": "", "stance": "neutral", "topic": "general"}
"""

# ---------------------------------------------------------------------------
# LLM contradiction-judge prompt
# ---------------------------------------------------------------------------

_JUDGE_CONTRADICTION_PROMPT = """\
You are a contradiction detector. Your job: given a NEW claim and a list of
PRIOR claims on a similar topic, decide if the new claim CONTRADICTS any prior one.

Definition: two claims CONTRADICT if:
  - They are on the same or closely related topic AND
  - They express OPPOSING stances or mutually exclusive decisions.

Drifting (gradual change of mind) also counts as a contradiction.

Return ONLY valid JSON, no markdown, no explanation.

Schema:
{
  "contradicts": true|false,
  "prior_index": <int or null>,  // 0-based index into the prior list, null if no contradiction
  "short_summary": "one sentence starting with CONTRADICTS: or ECHOES: "
}

If contradicts=true: short_summary must start with "CONTRADICTS: "
If contradicts=false but there is a related prior: start with "ECHOES: "
If nothing is related: short_summary = ""
"""


# ---------------------------------------------------------------------------
# Small-model JSON extraction helpers
# ---------------------------------------------------------------------------

def _call_llm_json(
    system_prompt: str,
    user_text: str,
    timeout: float,
) -> dict:
    """
    Synchronous call to Ollama to get a JSON-structured response.
    Never raises — returns {} on any failure.
    Strips markdown fences and regex-extracts JSON as a fallback.
    """
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "stream": False,
                "options": {"num_ctx": 2048, "temperature": 0},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text[:1500]},
                ],
            },
            timeout=timeout,
        )
        raw = resp.json()["message"]["content"]
    except Exception as exc:
        log.debug("LLM call failed: %s", exc)
        return {}

    # Strip markdown code fences (```json ... ```)
    raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`").strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Regex fallback: grab the first {...} block from the response
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    log.debug("Could not parse LLM JSON from: %r", raw[:200])
    return {}


def _extract_claim_metadata(text: str) -> dict:
    """
    Extract {claim, reason, stance, topic} from raw user text.
    Returns safe defaults on failure so the store path never breaks.
    """
    data = _call_llm_json(_EXTRACT_CLAIM_PROMPT, text, timeout=EXTRACT_TIMEOUT)
    return {
        "claim": data.get("claim", text[:200]) or text[:200],
        "reason": data.get("reason", "") or "",
        "stance": data.get("stance", "neutral") or "neutral",
        "topic": data.get("topic", "general") or "general",
    }


def _judge_contradicts(
    new_meta: dict,
    prior_claims: list[dict],
) -> dict:
    """
    Ask the LLM if new_meta contradicts any claim in prior_claims.

    prior_claims: list of {"text", "stance", "topic", "note_id", "timestamp"}

    Returns {contradicts: bool, prior_index: int|None, short_summary: str}.
    Safe defaults on LLM failure: contradicts=False.
    """
    if not prior_claims:
        return {"contradicts": False, "prior_index": None, "short_summary": ""}

    # Build a compact prompt payload
    prior_lines = "\n".join(
        f"[{i}] topic={p.get('topic','?')} stance={p.get('stance','?')}: {p.get('text','')[:150]}"
        for i, p in enumerate(prior_claims)
    )
    user_text = (
        f"NEW CLAIM (topic={new_meta['topic']}, stance={new_meta['stance']}):\n"
        f"{new_meta['claim']}\n\n"
        f"PRIOR CLAIMS:\n{prior_lines}"
    )

    data = _call_llm_json(_JUDGE_CONTRADICTION_PROMPT, user_text, timeout=JUDGE_TIMEOUT)

    contradicts = bool(data.get("contradicts", False))
    prior_index = data.get("prior_index", None)
    summary = data.get("short_summary", "") or ""

    # Validate prior_index is in range
    if prior_index is not None:
        try:
            prior_index = int(prior_index)
            if not (0 <= prior_index < len(prior_claims)):
                prior_index = None
        except (TypeError, ValueError):
            prior_index = None

    # If the model said contradicts but gave no index, infer index=0
    if contradicts and prior_index is None:
        prior_index = 0

    return {
        "contradicts": contradicts,
        "prior_index": prior_index,
        "short_summary": summary,
    }


# ---------------------------------------------------------------------------
# Cognee background helpers
# ---------------------------------------------------------------------------

async def _cognee_add_and_cognify(note_id: str, text: str) -> None:
    """Fire-and-forget Cognee enrichment. All errors are swallowed."""
    if not _COGNEE_READY:
        return
    try:
        import cognee
        await cognee.add(text, node_id=note_id)
        await asyncio.wait_for(cognee.cognify(), timeout=COGNEE_TIMEOUT)
        log.debug("Cognee cognify() OK for %s", note_id)
    except asyncio.TimeoutError:
        log.debug("Cognee cognify() timed out for %s — continuing", note_id)
    except Exception as exc:
        log.debug("Cognee bonus error for %s: %s", note_id, exc)


def _fire_cognee(note_id: str, text: str) -> None:
    """Run Cognee in a daemon thread with its own event loop."""
    if not USE_COGNEE:
        return

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_cognee_add_and_cognify(note_id, text))
        except Exception as exc:
            log.debug("Cognee thread error: %s", exc)
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True, name=f"cognee-{note_id[:8]}").start()


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------


async def store_claim(
    text: str,
    infer_fn: Optional[Callable] = None,
    source: str = "cardputer",
) -> Optional[dict]:
    """
    Core Janus agent loop. Perceive -> Retrieve -> Reason -> Act -> Store.

    Args:
        text:      Raw user input (their decision/belief/claim).
        infer_fn:  Optional async-generator callable used for LLM streaming.
                   Signature: async infer_fn(prompt, context_chunks) -> AsyncIterator[str]
                   store_claim itself calls Ollama directly for extraction +
                   judging, so infer_fn is accepted for API symmetry with answer().
        source:    Tag for the storage metadata (e.g. "cardputer", "mic").

    Returns:
        dict with keys:
          kind    : "contradicts" | "echoes"
          other   : the text of the prior claim being referenced
          summary : display string (e.g. "CONTRADICTS: you said X 3wk ago")
        or None if no significant relation was found.

    Stores the claim regardless of whether a contradiction is found.
    The contradiction detection does NOT block the store path.
    """
    if not _CHROMA_AVAILABLE or _collection is None:
        raise RuntimeError("ChromaDB not available — pip install chromadb sentence-transformers")

    note_id = str(uuid.uuid4())
    ts = int(time.time())

    # ------------------------------------------------------------------
    # STEP 1 — Perceive: extract structured claim metadata
    # ------------------------------------------------------------------
    # Run in executor so this async function doesn't block the event loop
    # during the Ollama HTTP round-trip.
    loop = asyncio.get_running_loop()
    meta = await loop.run_in_executor(None, _extract_claim_metadata, text)

    claim_text = meta["claim"]
    reason = meta["reason"]
    stance = meta["stance"]
    topic = meta["topic"]

    log.info(
        "Claim extracted: topic=%r stance=%r id=%s",
        topic,
        stance,
        note_id[:8],
    )

    # ------------------------------------------------------------------
    # STEP 2 — Store: embed + persist in ChromaDB immediately
    #   (retrieval path is always-on even if the judgment step fails)
    # ------------------------------------------------------------------
    _collection.add(
        documents=[text],
        ids=[note_id],
        metadatas=[{
            "source": source,
            "ts": ts,
            "note_id": note_id,
            "claim": claim_text[:200],
            "reason": reason[:200],
            "stance": stance,
            "topic": topic,
        }],
    )

    # ------------------------------------------------------------------
    # STEP 3 — Store: add node to networkx graph
    # ------------------------------------------------------------------
    g = _load_graph()
    _add_claim_node(g, note_id, claim_text, reason, stance, topic, ts)
    # Persist graph before the judgment step so the node exists even if
    # the judgment fails or times out.
    _save_graph(g)

    # ------------------------------------------------------------------
    # STEP 4 — Retrieve: find semantically related PRIOR claims
    #   (not the one we just added — use n_results+1, skip self if present)
    # ------------------------------------------------------------------
    n_stored = _collection.count()
    # We need at least 1 prior claim (the one we just stored is #1 if count was 0)
    if n_stored <= 1:
        # No prior claims exist — fire Cognee bonus, return None
        _fire_cognee(note_id, text)
        return None

    k = min(JUDGE_TOP_K + 1, n_stored)
    raw_results = _collection.query(query_texts=[text], n_results=k)
    docs = raw_results["documents"][0]
    metas_list = raw_results["metadatas"][0]
    distances = raw_results["distances"][0]

    prior_claims: list[dict] = []
    for doc, m, dist in zip(docs, metas_list, distances):
        sim = max(0.0, 1.0 - dist)
        # Skip: the note we just added (exact match by note_id), and
        #       anything below the similarity threshold (unrelated topic).
        if m.get("note_id") == note_id:
            continue
        if sim < SIM_THRESHOLD:
            continue
        prior_claims.append({
            "note_id": m.get("note_id", ""),
            "text": m.get("claim", doc)[:200],
            "reason": m.get("reason", ""),
            "stance": m.get("stance", "neutral"),
            "topic": m.get("topic", "general"),
            "timestamp": m.get("ts", 0),
            "similarity": sim,
        })

    if not prior_claims:
        # No related prior claims above threshold
        _fire_cognee(note_id, text)
        return None

    # ------------------------------------------------------------------
    # STEP 5 — Reason: ask the LLM to judge stance conflict
    # ------------------------------------------------------------------
    judgment = await loop.run_in_executor(
        None, _judge_contradicts, meta, prior_claims
    )

    contradicts = judgment["contradicts"]
    prior_index = judgment["prior_index"]
    summary = judgment["short_summary"]

    # ------------------------------------------------------------------
    # STEP 6 — Act: write edge to graph + return result
    # ------------------------------------------------------------------
    result: Optional[dict] = None

    if contradicts and prior_index is not None:
        prior = prior_claims[prior_index]
        prior_id = prior["note_id"]
        prior_text = prior["text"]

        # Reload graph (in case another thread modified it between steps 3 and 6)
        g = _load_graph()
        _add_edge(
            g,
            src=note_id,          # new claim
            dst=prior_id,         # contradicts the prior
            kind=EDGE_CONTRADICTS,
            summary=summary or f"CONTRADICTS: {claim_text[:60]}",
            ts=ts,
        )
        _save_graph(g)

        # Format the age of the prior claim for display ("3wk ago", "2d ago", etc.)
        age_str = _age_str(ts - prior["timestamp"])

        display_summary = summary or f"CONTRADICTS: you said {prior_text[:60]}"
        if age_str:
            display_summary += f" ({age_str})"

        log.info(
            "CONTRADICTION detected: new=%s prior=%s topic=%r",
            note_id[:8],
            prior_id[:8],
            topic,
        )

        result = {
            "kind": "contradicts",
            "other": prior_text,
            "summary": display_summary[:200],
        }

    else:
        # No contradiction — find the strongest echoing relation
        if prior_claims:
            strongest = prior_claims[0]  # already sorted by similarity (desc) from chroma
            prior_text = strongest["text"]
            age_str = _age_str(ts - strongest["timestamp"])

            # Write a relates-to edge
            g = _load_graph()
            _add_edge(
                g,
                src=note_id,
                dst=strongest["note_id"],
                kind=EDGE_RELATES,
                summary=summary or f"ECHOES: {prior_text[:60]}",
                ts=ts,
            )
            _save_graph(g)

            echo_summary = summary or f"ECHOES: {prior_text[:80]}"
            if age_str:
                echo_summary += f" ({age_str})"

            result = {
                "kind": "echoes",
                "other": prior_text,
                "summary": echo_summary[:200],
            }

    # ------------------------------------------------------------------
    # STEP 7 — Cognee bonus path (fire-and-forget, never blocks)
    # ------------------------------------------------------------------
    _fire_cognee(note_id, text)

    return result


def search(query: str, k: int = 5) -> list[dict]:
    """
    Return the top-k most semantically similar stored claims.

    Each result: {"text": str, "score": float (0-1 cosine similarity), "meta": dict}

    Optionally overlays Cognee CHUNKS results if USE_COGNEE is on.
    Always returns ChromaDB results as the primary source of truth.
    """
    if not _CHROMA_AVAILABLE or _collection is None:
        return []

    n = min(k, _collection.count())
    if n == 0:
        return []

    results = _collection.query(query_texts=[query], n_results=n)
    docs = results["documents"][0]
    metas_list = results["metadatas"][0]
    distances = results["distances"][0]

    primary = [
        {"text": d, "score": max(0.0, 1.0 - dist), "meta": m}
        for d, m, dist in zip(docs, metas_list, distances)
    ]

    # Cognee bonus: SearchType.CHUNKS doesn't require a healthy graph
    if USE_COGNEE and _COGNEE_READY:
        cognee_hits = _cognee_search_sync(query, k=k)
        if cognee_hits:
            seen_ids = {r["meta"].get("note_id") for r in primary}
            for hit in cognee_hits:
                nid = hit.get("note_id", "")
                if nid not in seen_ids:
                    primary.append({
                        "text": hit["text"],
                        "score": hit.get("score", 0.0),
                        "meta": {"note_id": nid, "source": "cognee"},
                    })
                    seen_ids.add(nid)

    return primary[:k]


async def answer(
    query: str,
    infer_fn: Callable,
) -> AsyncGenerator[str, None]:
    """
    RAG answer generator. Streams tokens via infer_fn.

    infer_fn signature (canonical):
        async def infer_fn(prompt: str, context_chunks: list[dict]) -> AsyncIterator[str]:
            # async-yields string tokens one at a time

    Handles special Janus queries:
        "what have I contradicted myself on?"
            -> walks 'contradicts' edges in the graph, lists them, then RAGs.
        "how has my view on X changed?"
            -> retrieves claims on topic X sorted by timestamp.
        All others: standard semantic search + RAG.
    """
    q_lower = query.lower()

    # ------------------------------------------------------------------
    # Special query: contradiction history
    # ------------------------------------------------------------------
    if _is_contradiction_query(q_lower):
        context_chunks = _contradiction_history_chunks()
        prompt = (
            "The user asks about their contradictions. "
            "Below are all recorded contradictions from their claim history.\n\n"
            + "\n".join(c["text"] for c in context_chunks)
            + f"\n\nQuestion: {query}"
        )
        async for token in _run_infer(infer_fn, prompt, context_chunks):
            yield token
        return

    # ------------------------------------------------------------------
    # Special query: view evolution on a topic
    # ------------------------------------------------------------------
    topic_match = _parse_topic_query(q_lower)
    if topic_match:
        context_chunks = _topic_timeline_chunks(topic_match)
        prompt = (
            f"The user asks how their views on '{topic_match}' have changed over time. "
            "Below are their claims on this topic, oldest first.\n\n"
            + "\n".join(c["text"] for c in context_chunks)
            + f"\n\nQuestion: {query}"
        )
        async for token in _run_infer(infer_fn, prompt, context_chunks):
            yield token
        return

    # ------------------------------------------------------------------
    # General RAG
    # ------------------------------------------------------------------
    context_chunks = search(query, k=5)
    prompt = query
    async for token in _run_infer(infer_fn, prompt, context_chunks):
        yield token


# ---------------------------------------------------------------------------
# Special-query helpers
# ---------------------------------------------------------------------------

_CONTRADICTION_PHRASES = (
    "contradict",
    "changed my mind",
    "flip-flop",
    "inconsistent",
    "conflict",
    "said before",
    "past self",
    "argued with",
)


def _is_contradiction_query(q: str) -> bool:
    return any(p in q for p in _CONTRADICTION_PHRASES)


_TOPIC_PATTERNS = [
    re.compile(r"view on (.+?) changed", re.I),
    re.compile(r"opinion on (.+?) over", re.I),
    re.compile(r"think about (.+?) over", re.I),
    re.compile(r"stance on (.+?) (changed|evolved|shifted)", re.I),
    re.compile(r"how.*(?:my|have I).*(?:view|opinion|stance).*on (.+?)[\?$]", re.I),
]


def _parse_topic_query(q: str) -> Optional[str]:
    """Return a topic keyword if the query is asking about evolution of views."""
    for pat in _TOPIC_PATTERNS:
        m = pat.search(q)
        if m:
            return m.group(1).strip()
    # Simpler heuristic: "how has my view on X changed"
    if "view" in q and "changed" in q:
        # Grab words between "on" and "changed"
        m2 = re.search(r"on (.+?) changed", q, re.I)
        if m2:
            return m2.group(1).strip()
    return None


def _contradiction_history_chunks() -> list[dict]:
    """
    Walk all 'contradicts' edges in the graph and format them as text chunks
    for RAG context injection.
    """
    g = _load_graph()
    chunks = []
    for src, dst, data in g.edges(data=True):
        if data.get("relation") != EDGE_CONTRADICTS:
            continue
        src_node = g.nodes.get(src, {})
        dst_node = g.nodes.get(dst, {})
        summary = data.get("summary", "")
        src_ts = src_node.get("timestamp", 0)
        dst_ts = dst_node.get("timestamp", 0)
        chunks.append({
            "text": (
                f"[CONTRADICTION] {summary}\n"
                f"  NEW ({_ts_label(src_ts)}): {src_node.get('text','?')[:120]}\n"
                f"  OLD ({_ts_label(dst_ts)}): {dst_node.get('text','?')[:120]}"
            ),
            "score": 1.0,
            "meta": {"relation": EDGE_CONTRADICTS, "src": src, "dst": dst},
        })

    if not chunks:
        chunks = [{"text": "(No contradictions recorded yet.)", "score": 1.0, "meta": {}}]

    return chunks


def _topic_timeline_chunks(topic: str) -> list[dict]:
    """
    Retrieve all claims matching `topic` (by semantic similarity + metadata filter),
    sorted oldest-first to show view evolution.
    """
    if not _CHROMA_AVAILABLE or _collection is None or _collection.count() == 0:
        return [{"text": "(No claims stored yet.)", "score": 1.0, "meta": {}}]

    # Use semantic search with a generous k to surface topic-related claims
    k = min(20, _collection.count())
    raw = _collection.query(query_texts=[topic], n_results=k)
    docs = raw["documents"][0]
    metas_list = raw["metadatas"][0]
    distances = raw["distances"][0]

    items = []
    for doc, m, dist in zip(docs, metas_list, distances):
        sim = max(0.0, 1.0 - dist)
        # Only include claims with meaningful relevance to the topic
        if sim < 0.25:
            continue
        items.append((m.get("ts", 0), doc, m, sim))

    # Sort oldest-first
    items.sort(key=lambda x: x[0])

    chunks = [
        {
            "text": (
                f"[{_ts_label(ts)}] (sim={sim:.2f}) "
                f"stance={m.get('stance','?')} — {doc[:150]}"
            ),
            "score": sim,
            "meta": m,
        }
        for ts, doc, m, sim in items
    ]

    if not chunks:
        chunks = [{"text": f"(No claims found on topic '{topic}'.)", "score": 0.0, "meta": {}}]

    return chunks


# ---------------------------------------------------------------------------
# infer_fn runner (sync -> async bridge)
# ---------------------------------------------------------------------------

async def _run_infer(
    infer_fn: Callable,
    prompt: str,
    context_chunks: list[dict],
) -> AsyncGenerator[str, None]:
    """
    Stream tokens from the canonical async infer_fn.

    infer_fn signature:
        async def infer(prompt: str, context_chunks: list[dict]) -> AsyncIterator[str]
    """
    try:
        async for token in infer_fn(prompt, context_chunks):
            if token:
                yield token
    except Exception as exc:
        log.error("infer_fn error: %s", exc)


# ---------------------------------------------------------------------------
# Cognee search (sync wrapper, used inside search())
# ---------------------------------------------------------------------------

def _cognee_search_sync(query: str, k: int = 5) -> list[dict]:
    """
    Blocking Cognee CHUNKS search in a fresh event loop.
    Returns [] on any error or timeout (short 5s limit — primary path doesn't wait).
    SearchType.CHUNKS is the only Cognee type that doesn't require a healthy graph.
    """
    try:
        import cognee
        from cognee.api.v1.search import SearchType  # type: ignore[import]

        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(
                asyncio.wait_for(
                    cognee.search(SearchType.CHUNKS, query_text=query),
                    timeout=5.0,
                )
            )
        finally:
            loop.close()

        if not raw:
            return []

        hits = []
        for item in raw[:k]:
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
# Graph query helpers (available for debug or UI overlays)
# ---------------------------------------------------------------------------

def graph_stats() -> dict:
    """Return node/edge counts and contradiction count for diagnostics."""
    g = _load_graph()
    contradiction_edges = sum(
        1 for _, _, d in g.edges(data=True)
        if d.get("relation") == EDGE_CONTRADICTS
    )
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "contradictions": contradiction_edges,
    }


def all_contradictions() -> list[dict]:
    """
    Return all contradiction pairs as a list of dicts.
    Useful for the Cardputer 'status' display and debug endpoints.
    Each dict: {src_text, dst_text, summary, ts}
    """
    g = _load_graph()
    out = []
    for src, dst, data in g.edges(data=True):
        if data.get("relation") != EDGE_CONTRADICTS:
            continue
        out.append({
            "src_text": g.nodes[src].get("text", "")[:120],
            "dst_text": g.nodes[dst].get("text", "")[:120],
            "summary": data.get("summary", ""),
            "ts": data.get("ts", 0),
        })
    # Newest first
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Formatting utilities
# ---------------------------------------------------------------------------

def _age_str(delta_seconds: int) -> str:
    """
    Format a time delta into a human-readable age string.
    e.g. 0 -> '', 60 -> '1min ago', 3600 -> '1h ago', 86400 -> '1d ago'
    """
    if delta_seconds < 60:
        return ""
    if delta_seconds < 3600:
        return f"{delta_seconds // 60}min ago"
    if delta_seconds < 86400:
        return f"{delta_seconds // 3600}h ago"
    if delta_seconds < 86400 * 7:
        return f"{delta_seconds // 86400}d ago"
    if delta_seconds < 86400 * 30:
        return f"{delta_seconds // (86400 * 7)}wk ago"
    return f"{delta_seconds // (86400 * 30)}mo ago"


def _ts_label(ts: int) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM'."""
    if ts <= 0:
        return "unknown"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Self-test (python janus_brain.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=== Janus Brain Self-Test ===\n")

    if not _CHROMA_AVAILABLE:
        print("ERROR: chromadb not installed. Run: pip install chromadb sentence-transformers")
        sys.exit(1)

    # Simple mock infer_fn (no LLM needed for self-test) — canonical async gen
    async def mock_infer(prompt: str, chunks: list[dict]) -> AsyncGenerator[str, None]:
        yield "[mock LLM] Relevant claims:\n"
        for c in chunks[:3]:
            yield f"  - {c['text'][:70]}\n"
        yield f"[mock answer to]: {prompt[:60]}\n"

    async def run_tests() -> None:
        print("--- Test 1: Store first claim (no prior — expect None) ---")
        r1 = await store_claim(
            "I believe remote work is more productive than office work because I focus better at home.",
            source="test",
        )
        print(f"Result: {r1}")

        print("\n--- Test 2: Store a CONTRADICTING claim ---")
        r2 = await store_claim(
            "I've decided remote work hurts team cohesion — we should go back to the office full time.",
            source="test",
        )
        print(f"Result: {r2}")
        if r2:
            print(f"  kind    : {r2['kind']}")
            print(f"  summary : {r2['summary']}")

        print("\n--- Test 3: Store a supporting claim ---")
        r3 = await store_claim(
            "Studies show home workers are 13% more productive, confirming my preference for remote work.",
            source="test",
        )
        print(f"Result: {r3}")

        print("\n--- Test 4: Semantic search ---")
        hits = search("working from home productivity", k=3)
        print(f"{len(hits)} hit(s):")
        for h in hits:
            print(f"  [{h['score']:.3f}] {h['text'][:70]}")

        print("\n--- Test 5: Contradiction history query ---")
        tokens = []
        async for tok in answer("What have I contradicted myself on?", mock_infer):
            tokens.append(tok)
        print("".join(tokens))

        print("\n--- Test 6: Topic timeline query ---")
        tokens = []
        async for tok in answer("How has my view on remote work changed?", mock_infer):
            tokens.append(tok)
        print("".join(tokens))

        print("\n--- Test 7: Graph stats ---")
        gs = graph_stats()
        print(f"Graph: {gs['nodes']} nodes, {gs['edges']} edges, {gs['contradictions']} contradictions")

        print("\n--- Test 8: All contradictions ---")
        for c in all_contradictions():
            print(f"  [{_ts_label(c['ts'])}] {c['summary']}")

        print("\n=== Self-test complete ===")

    asyncio.run(run_tests())

