"""
engram_viz.py — Offline force-directed brain-graph renderer for Engram.

Public API:
    refresh(graph, output_path="engram_graph.html")

`graph` is a networkx Graph (or DiGraph).
Node attributes used:
    label   — display text (falls back to node id)
    memory  — the raw memory text shown on hover
    kind    — "concept" | "entity" | "memory" | ... (controls colour)
    ts      — ISO timestamp string (shown in tooltip)

Edge attributes used:
    relation — label drawn on the edge
    weight   — line thickness (1–5, default 1)

Output is a single, fully self-contained HTML file.  No CDN references.
The D3 v7 minified source is embedded verbatim via base64 so the file works
with zero network access.

Dependencies: networkx, (optionally) requests (only used if you want to
auto-fetch D3 once — see _fetch_d3_once()).  Everything else is stdlib.
"""

from __future__ import annotations

import base64
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import networkx as nx

# ---------------------------------------------------------------------------
# D3 v7 minified — embedded as base64 so the HTML is fully self-contained.
# We attempt to load it from a local cache file first; if not present we try
# to fetch it once (requires internet at build time, not at view time).
# ---------------------------------------------------------------------------

_D3_CACHE = Path(__file__).with_name("_d3v7.min.js")
_D3_CDN   = "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"


def _get_d3_source() -> str:
    """Return D3 v7 minified JS as a plain string (not base64)."""
    if _D3_CACHE.exists():
        return _D3_CACHE.read_text(encoding="utf-8")
    # Try a one-time fetch (internet needed at dev time, not at runtime)
    try:
        import urllib.request
        with urllib.request.urlopen(_D3_CDN, timeout=10) as r:
            src = r.read().decode("utf-8")
        _D3_CACHE.write_text(src, encoding="utf-8")
        return src
    except Exception:
        pass
    # Last resort: tiny stub that draws nothing but prevents JS errors
    return _D3_STUB


# Minimal stub so the page at least loads if D3 is unavailable everywhere
_D3_STUB = textwrap.dedent("""\
    // D3 stub — real D3 not found.  Run once with internet to cache it.
    var d3 = {
      select: function(){ return { append: function(){ return d3; },
        attr: function(){ return d3; }, style: function(){ return d3; },
        on: function(){ return d3; }, call: function(){ return d3; },
        text: function(){ return d3; }, html: function(){ return d3; },
        selectAll: function(){ return d3; }, data: function(){ return d3; },
        join: function(){ return d3; }, node: function(){ return null; } }; },
      forceSimulation: function(){ return { force: function(){ return d3._sim; },
        on: function(){ return d3._sim; }, alphaDecay: function(){ return d3._sim; },
        restart: function(){} }; },
      _sim: { force: function(){ return d3._sim; }, on: function(){ return d3._sim; },
        alphaDecay: function(){ return d3._sim; }, restart: function(){} },
      forceManyBody: function(){ return { strength: function(){ return {}; } }; },
      forceLink: function(){ return { id: function(){ return {}; }, distance: function(){ return {}; } }; },
      forceCenter: function(){ return {}; },
      forceCollide: function(){ return { radius: function(){ return {}; } }; },
      zoom: function(){ return { scaleExtent: function(){ return d3._zoom; }, on: function(){ return d3._zoom; } }; },
      _zoom: { scaleExtent: function(){ return d3._zoom; }, on: function(){ return d3._zoom; } },
      drag: function(){ return { on: function(){ return d3._drag; } }; },
      _drag: { on: function(){ return d3._drag; } },
      zoomTransform: function(){ return { k:1, x:0, y:0 }; },
      schemeTableau10: ['#4e79a7'],
      scaleOrdinal: function(){ return function(){ return '#888'; }; },
      scaleLinear: function(){ return { domain: function(){ return d3._scale; }, range: function(){ return function(){ return 1; }; } }; },
      _scale: { domain: function(){ return d3._scale; }, range: function(){ return function(){ return 1; }; } },
    };
    document.body.innerHTML = '<p style="color:red;font-family:monospace;padding:2em">D3 not loaded — run engram_viz.py once with internet access to cache d3.min.js</p>';
""")


# ---------------------------------------------------------------------------
# Colour palette by node kind
# ---------------------------------------------------------------------------

_KIND_COLOURS: dict[str, str] = {
    "concept":  "#4e79a7",
    "entity":   "#f28e2b",
    "memory":   "#59a14f",
    "tag":      "#b07aa1",
    "reminder": "#e15759",
    "document": "#76b7b2",
    "default":  "#bab0ac",
}


# ---------------------------------------------------------------------------
# Graph → JSON
# ---------------------------------------------------------------------------

def _graph_to_json(graph: nx.Graph) -> dict[str, Any]:
    nodes = []
    for nid, data in graph.nodes(data=True):
        label   = data.get("label")   or str(nid)
        memory  = data.get("memory")  or ""
        kind    = data.get("kind")    or "default"
        ts      = data.get("ts")      or ""
        colour  = _KIND_COLOURS.get(kind, _KIND_COLOURS["default"])
        nodes.append({
            "id":     str(nid),
            "label":  label,
            "memory": memory,
            "kind":   kind,
            "ts":     ts,
            "color":  colour,
        })

    edges = []
    for u, v, data in graph.edges(data=True):
        relation = data.get("relation") or ""
        weight   = float(data.get("weight", 1))
        edges.append({
            "source":   str(u),
            "target":   str(v),
            "relation": relation,
            "weight":   max(0.5, min(weight, 5)),
        })

    return {"nodes": nodes, "links": edges}


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Engram — Brain Graph</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif; overflow: hidden; }}
  #canvas {{ width: 100vw; height: 100vh; }}
  svg {{ width: 100%; height: 100%; }}

  /* links */
  .link {{ stroke: #30363d; stroke-opacity: 0.7; }}
  .link-label {{ fill: #8b949e; font-size: 10px; pointer-events: none; text-anchor: middle; }}

  /* nodes */
  .node circle {{ stroke: #21262d; stroke-width: 1.5px; cursor: pointer; }}
  .node circle:hover {{ stroke: #58a6ff; stroke-width: 2.5px; }}
  .node text {{ fill: #e6edf3; font-size: 11px; pointer-events: none; }}

  /* tooltip */
  #tooltip {{
    position: fixed;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 12px;
    max-width: 320px;
    line-height: 1.5;
    pointer-events: none;
    display: none;
    z-index: 999;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }}
  #tooltip .tt-label {{ font-weight: 600; font-size: 13px; color: #58a6ff; margin-bottom: 4px; }}
  #tooltip .tt-kind  {{ font-size: 10px; color: #8b949e; margin-bottom: 6px; }}
  #tooltip .tt-mem   {{ color: #c9d1d9; white-space: pre-wrap; word-break: break-word; max-height: 180px; overflow-y: auto; }}
  #tooltip .tt-ts    {{ margin-top: 6px; font-size: 10px; color: #6e7681; }}

  /* legend */
  #legend {{
    position: fixed;
    bottom: 18px;
    left: 18px;
    background: rgba(22,27,34,0.92);
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 11px;
    line-height: 1.8;
  }}
  #legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}

  /* stats */
  #stats {{
    position: fixed;
    top: 14px;
    right: 18px;
    font-size: 11px;
    color: #8b949e;
    text-align: right;
  }}

  /* search */
  #search-box {{
    position: fixed;
    top: 14px;
    left: 18px;
  }}
  #search-box input {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 5px 10px;
    color: #e6edf3;
    font-size: 12px;
    outline: none;
    width: 200px;
  }}
  #search-box input:focus {{ border-color: #58a6ff; }}
</style>
</head>
<body>
<div id="canvas"><svg id="svg"></svg></div>
<div id="tooltip"></div>
<div id="search-box"><input type="text" id="search" placeholder="Search nodes…" autocomplete="off"></div>
<div id="stats"></div>
<div id="legend" id="legend"></div>

<script>
// ---- inline D3 ----
{d3_source}
</script>

<script>
(function() {{
  const GRAPH = {graph_json};

  // ---- dimensions ----
  const W = window.innerWidth, H = window.innerHeight;

  const svg = d3.select('#svg')
    .attr('viewBox', [0, 0, W, H]);

  // ---- zoom ----
  const g = svg.append('g');
  svg.call(d3.zoom()
    .scaleExtent([0.05, 8])
    .on('zoom', e => g.attr('transform', e.transform)));

  // ---- legend ----
  const kinds = [...new Set(GRAPH.nodes.map(n => n.kind))];
  const kindColour = {{}};
  GRAPH.nodes.forEach(n => {{ kindColour[n.kind] = n.color; }});
  const legend = document.getElementById('legend');
  legend.innerHTML = kinds.map(k =>
    `<div><span class="dot" style="background:${{kindColour[k] || '#888'}}"></span>${{k}}</div>`
  ).join('');

  // ---- stats ----
  document.getElementById('stats').textContent =
    `${{GRAPH.nodes.length}} nodes · ${{GRAPH.links.length}} edges`;

  // ---- link weight scale ----
  const weightScale = d3.scaleLinear()
    .domain([0.5, 5])
    .range([0.8, 4]);

  // ---- simulation ----
  const sim = d3.forceSimulation(GRAPH.nodes)
    .force('link', d3.forceLink(GRAPH.links)
      .id(d => d.id)
      .distance(d => 80 + (5 - d.weight) * 12))
    .force('charge', d3.forceManyBody().strength(-280))
    .force('center', d3.forceCenter(W / 2, H / 2))
    .force('collide', d3.forceCollide().radius(d => nodeRadius(d) + 8))
    .alphaDecay(0.025);

  function nodeRadius(d) {{
    // degree-based sizing
    const deg = (GRAPH.links.filter(l =>
      (l.source.id || l.source) === d.id ||
      (l.target.id || l.target) === d.id).length);
    return Math.max(8, Math.min(24, 8 + deg * 2));
  }}

  // ---- edges ----
  const linkG = g.append('g').attr('class', 'links');
  const link = linkG.selectAll('line')
    .data(GRAPH.links).join('line')
    .attr('class', 'link')
    .attr('stroke-width', d => weightScale(d.weight));

  // edge labels (only if relation set)
  const linkLabel = g.append('g').attr('class', 'link-labels')
    .selectAll('text')
    .data(GRAPH.links.filter(l => l.relation))
    .join('text')
    .attr('class', 'link-label')
    .text(d => d.relation);

  // ---- nodes ----
  const nodeG = g.append('g').attr('class', 'nodes');
  const node = nodeG.selectAll('g')
    .data(GRAPH.nodes).join('g')
    .attr('class', 'node')
    .call(d3.drag()
      .on('start', dragStart)
      .on('drag',  dragged)
      .on('end',   dragEnd));

  node.append('circle')
    .attr('r', nodeRadius)
    .attr('fill', d => d.color);

  node.append('text')
    .attr('dy', d => nodeRadius(d) + 12)
    .attr('text-anchor', 'middle')
    .text(d => d.label.length > 18 ? d.label.slice(0,16)+'…' : d.label);

  // ---- tooltip ----
  const tooltip = document.getElementById('tooltip');
  node.on('mouseover', (e, d) => {{
    tooltip.innerHTML =
      `<div class="tt-label">${{d.label}}</div>` +
      `<div class="tt-kind">${{d.kind}}</div>` +
      (d.memory ? `<div class="tt-mem">${{escHtml(d.memory)}}</div>` : '') +
      (d.ts     ? `<div class="tt-ts">${{d.ts}}</div>` : '');
    tooltip.style.display = 'block';
    moveTooltip(e);
  }})
  .on('mousemove', (e) => moveTooltip(e))
  .on('mouseout', () => {{ tooltip.style.display = 'none'; }});

  function moveTooltip(e) {{
    let x = e.clientX + 14, y = e.clientY - 10;
    if (x + 340 > W) x = e.clientX - 340;
    if (y + 220 > H) y = e.clientY - 220;
    tooltip.style.left = x + 'px';
    tooltip.style.top  = y + 'px';
  }}

  function escHtml(s) {{
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  // ---- search / highlight ----
  const searchInput = document.getElementById('search');
  searchInput.addEventListener('input', () => {{
    const q = searchInput.value.trim().toLowerCase();
    node.select('circle').attr('opacity', d =>
      (!q || d.label.toLowerCase().includes(q) ||
             (d.memory||'').toLowerCase().includes(q)) ? 1 : 0.15);
    node.select('text').attr('opacity', d =>
      (!q || d.label.toLowerCase().includes(q) ||
             (d.memory||'').toLowerCase().includes(q)) ? 1 : 0.1);
    link.attr('stroke-opacity', d => {{
      if (!q) return 0.7;
      const sl = d.source.label||'', tl = d.target.label||'';
      return (sl.toLowerCase().includes(q) || tl.toLowerCase().includes(q)) ? 0.9 : 0.05;
    }});
  }});

  // ---- tick ----
  sim.on('tick', () => {{
    link
      .attr('x1', d => d.source.x)
      .attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x)
      .attr('y2', d => d.target.y);

    linkLabel
      .attr('x', d => (d.source.x + d.target.x) / 2)
      .attr('y', d => (d.source.y + d.target.y) / 2);

    node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
  }});

  // ---- drag ----
  function dragStart(e, d) {{
    if (!e.active) sim.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }}
  function dragged(e, d) {{
    d.fx = e.x; d.fy = e.y;
  }}
  function dragEnd(e, d) {{
    if (!e.active) sim.alphaTarget(0);
    d.fx = null; d.fy = null;
  }}

}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh(
    graph: nx.Graph,
    output_path: str | os.PathLike = "engram_graph.html",
) -> Path:
    """
    Render `graph` as a self-contained force-directed HTML file.

    Parameters
    ----------
    graph:
        A networkx Graph or DiGraph.  See module docstring for supported
        node/edge attributes.
    output_path:
        Where to write the HTML.  Defaults to ``engram_graph.html`` in the
        current working directory.

    Returns
    -------
    Path
        Absolute path to the written file.
    """
    d3_src   = _get_d3_source()
    graph_js = json.dumps(_graph_to_json(graph), ensure_ascii=False)

    html = _HTML_TEMPLATE.format(
        d3_source  = d3_src,
        graph_json = graph_js,
    )

    out = Path(output_path).resolve()
    out.write_text(html, encoding="utf-8")
    print(f"[engram_viz] graph written → {out}  "
          f"({graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges)")
    return out


# ---------------------------------------------------------------------------
# CLI smoke-test / demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Build a sample graph and render it — run directly to test."""
    import datetime, random

    G = nx.Graph()

    nodes = [
        ("mem:1",  {"label": "Offline AI stack",  "kind": "memory",
                    "memory": "Exo shards llama3 across Ubuntu+M4 node over LAN. No internet needed.",
                    "ts": "2026-06-20T09:00:00"}),
        ("mem:2",  {"label": "Cardputer I/O",     "kind": "memory",
                    "memory": "ESP32-S3. USB-CDC serial tethered to Ubuntu. 240x135 screen, 56-key kbd.",
                    "ts": "2026-06-20T09:05:00"}),
        ("mem:3",  {"label": "Cognee graph layer","kind": "memory",
                    "memory": "Graph ontology on top of sentence-transformer embeddings. Fallback = cosine search.",
                    "ts": "2026-06-20T09:10:00"}),
        ("mem:4",  {"label": "faster-whisper STT","kind": "memory",
                    "memory": "RTX 3050 int8_float16. Real-time transcription over serial from SPM1423 mic.",
                    "ts": "2026-06-20T09:15:00"}),
        ("mem:5",  {"label": "Piper TTS",         "kind": "memory",
                    "memory": "Runs on M4 Pro CPU. Stretch goal — not in critical path.",
                    "ts": "2026-06-20T09:20:00"}),
        ("ent:1",  {"label": "Exo",               "kind": "entity",
                    "memory": "Distributed inference framework. Shards one model across heterogeneous devices."}),
        ("ent:2",  {"label": "Cognee",            "kind": "entity",
                    "memory": "Knowledge-graph memory layer. Bounty sponsor for Overmind Hackathon."}),
        ("ent:3",  {"label": "Ubuntu / RTX node", "kind": "entity",
                    "memory": "CUDA GPU node. Runs Exo shard + Whisper + Ollama fallback."}),
        ("ent:4",  {"label": "M4 Pro Mac",        "kind": "entity",
                    "memory": "24 GB unified RAM. Runs Exo shard + Piper TTS + orchestrator."}),
        ("con:1",  {"label": "Vector search",     "kind": "concept",
                    "memory": "sentence-transformers all-MiniLM-L6-v2. FAISS or chromadb local store."}),
        ("con:2",  {"label": "Serial protocol",   "kind": "concept",
                    "memory": "USB-CDC at 921600 baud. Commands: TYPE, QUERY, REMIND."}),
        ("rem:1",  {"label": "Reminder: demo at 18:00", "kind": "reminder",
                    "memory": "Fire reminder over serial to Cardputer at 17:45 — judges pull the plug at 18:00.",
                    "ts": "2026-06-20T17:45:00"}),
        ("tag:1",  {"label": "#offline",          "kind": "tag"}),
        ("tag:2",  {"label": "#hackathon",        "kind": "tag"}),
    ]
    G.add_nodes_from(nodes)

    edges = [
        ("mem:1", "ent:1", {"relation": "uses",     "weight": 4}),
        ("mem:1", "ent:3", {"relation": "runs on",  "weight": 3}),
        ("mem:1", "ent:4", {"relation": "runs on",  "weight": 3}),
        ("mem:2", "ent:3", {"relation": "tethered", "weight": 4}),
        ("mem:3", "ent:2", {"relation": "powered by","weight": 5}),
        ("mem:3", "con:1", {"relation": "backed by","weight": 4}),
        ("mem:4", "ent:3", {"relation": "runs on",  "weight": 3}),
        ("mem:5", "ent:4", {"relation": "runs on",  "weight": 2}),
        ("ent:1", "ent:3", {"relation": "shard",    "weight": 3}),
        ("ent:1", "ent:4", {"relation": "shard",    "weight": 3}),
        ("con:2", "mem:2", {"relation": "used by",  "weight": 2}),
        ("rem:1", "con:2", {"relation": "fires via","weight": 2}),
        ("tag:1", "mem:1", {"weight": 1}),
        ("tag:1", "mem:3", {"weight": 1}),
        ("tag:1", "mem:4", {"weight": 1}),
        ("tag:2", "mem:1", {"weight": 1}),
        ("tag:2", "ent:2", {"weight": 1}),
    ]
    G.add_edges_from([(u, v, d) for u, v, d in edges])

    refresh(G, "engram_graph_demo.html")
    print("[engram_viz] open engram_graph_demo.html in a browser to preview.")


if __name__ == "__main__":
    _demo()
