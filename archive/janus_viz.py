"""
janus_viz.py — Offline force-directed graph renderer for Janus.

Janus is a contradiction-catcher: it argues with your past self.
This module visualises the claim graph, making CONTRADICTION edges
visually unmissable — they are the product's entire value proposition.

Public API
----------
    refresh(graph, output_path="janus_graph.html")

`graph` is a networkx DiGraph produced by the Janus backend.

Node attributes (Claim / Decision / Belief nodes)
--------------------------------------------------
    text      — the full claim text
    reason    — why the user held this position
    stance    — "for" | "against" | "neutral" (used for colour tint)
    topic     — topic slug, e.g. "diet", "career", "tech-stack"
    timestamp — ISO-8601 string, e.g. "2026-06-20T14:32:00"
    kind      — "claim" | "decision" | "belief" (controls node shape / colour)

Edge attributes
---------------
    relation  — "contradicts" | "supersedes" | "supports" | "relates-to"
    weight    — float 1–5 (line thickness)

Edge visual rules (this is the core design constraint):
    contradicts  → RED   (#e53935), thick stroke (4 px+), animated dash
    supersedes   → ORANGE (#fb8c00), medium stroke
    supports     → GREY  (#8b949e), thin
    relates-to   → GREY  (#8b949e), thin, dashed

Output
------
    A single self-contained HTML file with D3 v7 inlined.
    No CDN references.  Works with zero network access.
    The backend calls refresh(graph) after every store() to keep
    janus_graph.html live.

Dependencies
------------
    networkx — graph representation
    stdlib only for everything else (json, pathlib, base64 …)
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Any

import networkx as nx

# ---------------------------------------------------------------------------
# D3 v7 — load from local cache (_d3v7.min.js, same directory as this file)
# Falls back to a minimal stub if the cache is missing.
# ---------------------------------------------------------------------------

_D3_CACHE = Path(__file__).with_name("_d3v7.min.js")
_D3_CDN   = "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"


def _get_d3_source() -> str:
    """Return D3 v7 JS as a plain string.  Tries local cache, then one-time
    network fetch (for dev), then falls back to a stub."""
    if _D3_CACHE.exists():
        return _D3_CACHE.read_text(encoding="utf-8")
    try:
        import urllib.request
        with urllib.request.urlopen(_D3_CDN, timeout=10) as r:
            src = r.read().decode("utf-8")
        _D3_CACHE.write_text(src, encoding="utf-8")
        return src
    except Exception:
        pass
    return _D3_STUB


_D3_STUB = textwrap.dedent("""\
    // D3 stub — real D3 not found.
    // Place _d3v7.min.js next to janus_viz.py to fix this.
    var d3={select:function(){return d3;},append:function(){return d3;},
    attr:function(){return d3;},style:function(){return d3;},
    on:function(){return d3;},call:function(){return d3;},
    text:function(){return d3;},html:function(){return d3;},
    selectAll:function(){return d3;},data:function(){return d3;},
    join:function(){return d3;},node:function(){return null;},
    forceSimulation:function(){return d3;},force:function(){return d3;},
    alphaDecay:function(){return d3;},restart:function(){},
    forceManyBody:function(){return{strength:function(){return{};}}},
    forceLink:function(){return{id:function(){return{};},distance:function(){return{};}}},
    forceCenter:function(){return{};},
    forceCollide:function(){return{radius:function(){return{};}}},
    zoom:function(){return{scaleExtent:function(){return d3;},on:function(){return d3;}}},
    drag:function(){return{on:function(){return d3;}}},
    scaleLinear:function(){return{domain:function(){return d3;},range:function(){return function(){return 1;};}}}};
    document.body.innerHTML='<p style="color:#e53935;font-family:monospace;padding:2em">D3 not loaded — place _d3v7.min.js next to janus_viz.py</p>';
""")


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

# Edge colours by relation type
_EDGE_STYLE: dict[str, dict[str, Any]] = {
    "contradicts": {
        "color":      "#e53935",   # vivid red
        "width":      5,
        "opacity":    0.95,
        "dash":       "8,4",       # animated dash defined in CSS
        "label_fill": "#ff6b6b",
        "z":          10,          # draw on top
    },
    "supersedes": {
        "color":      "#fb8c00",   # orange
        "width":      3,
        "opacity":    0.85,
        "dash":       "",
        "label_fill": "#ffb74d",
        "z":          5,
    },
    "supports": {
        "color":      "#8b949e",   # neutral grey
        "width":      1.5,
        "opacity":    0.55,
        "dash":       "",
        "label_fill": "#8b949e",
        "z":          1,
    },
    "relates-to": {
        "color":      "#8b949e",
        "width":      1.2,
        "opacity":    0.40,
        "dash":       "4,4",
        "label_fill": "#8b949e",
        "z":          1,
    },
}
_EDGE_STYLE_DEFAULT = _EDGE_STYLE["relates-to"]

# Node colours by kind
_KIND_COLOURS: dict[str, str] = {
    "claim":    "#4e79a7",   # blue
    "decision": "#f28e2b",   # amber
    "belief":   "#59a14f",   # green
    "default":  "#bab0ac",   # warm grey
}


# ---------------------------------------------------------------------------
# Graph → JSON
# ---------------------------------------------------------------------------

def _edge_style(relation: str) -> dict[str, Any]:
    return _EDGE_STYLE.get(relation, _EDGE_STYLE_DEFAULT)


def _normalize_kind(data: dict, nid: Any) -> str:
    """
    Resolve a node 'kind' for the viz (lowercase 'claim'|'decision'|'belief').

    The Janus brain writes node attr label='CLAIM' (uppercase, no 'kind'), so
    fall back to a lowercased label when 'kind' is absent. 'CLAIM' -> 'claim'.
    """
    raw = data.get("kind") or data.get("label") or "claim"
    return str(raw).lower()


def _normalize_timestamp(data: dict) -> str:
    """
    Return an ISO-8601 timestamp string for a node.

    The brain stores timestamp as an int epoch; the viz expects ISO. Convert
    epoch ints/numeric strings to ISO; pass existing ISO strings through.
    """
    raw = data.get("timestamp")
    if raw is None or raw == "":
        raw = data.get("ts")
    if raw is None or raw == "":
        return ""
    # Epoch int (or numeric string) -> ISO
    try:
        epoch = int(raw)
        if epoch > 0:
            from datetime import datetime, timezone
            return (
                datetime.fromtimestamp(epoch, tz=timezone.utc)
                .replace(tzinfo=None)
                .isoformat()
            )
    except (TypeError, ValueError):
        pass
    return str(raw)


def _graph_to_json(graph: nx.Graph) -> dict[str, Any]:
    nodes: list[dict] = []
    for nid, data in graph.nodes(data=True):
        kind      = _normalize_kind(data, nid)
        text      = data.get("text")      or data.get("label") or str(nid)
        reason    = data.get("reason")    or ""
        stance    = data.get("stance")    or ""
        topic     = data.get("topic")     or ""
        timestamp = _normalize_timestamp(data)
        colour    = _KIND_COLOURS.get(kind, _KIND_COLOURS["default"])

        # Short label: first 28 chars of text + timestamp hint
        short_ts  = timestamp[5:16] if len(timestamp) >= 16 else timestamp  # "MM-DD HH:MM"
        label     = (text[:26] + "…") if len(text) > 28 else text

        nodes.append({
            "id":        str(nid),
            "label":     label,
            "text":      text,
            "reason":    reason,
            "stance":    stance,
            "topic":     topic,
            "timestamp": timestamp,
            "short_ts":  short_ts,
            "kind":      kind,
            "color":     colour,
        })

    # Sort edges so 'contradicts' are rendered last (on top)
    edge_data = []
    for u, v, data in graph.edges(data=True):
        rel    = data.get("relation") or "relates-to"
        weight = float(data.get("weight", 1))
        style  = _edge_style(rel)
        edge_data.append({
            "source":    str(u),
            "target":    str(v),
            "relation":  rel,
            "weight":    max(0.5, min(weight, 5)),
            "color":     style["color"],
            "width":     style["width"],
            "opacity":   style["opacity"],
            "dash":      style["dash"],
            "labelFill": style["label_fill"],
            "z":         style["z"],
        })
    edge_data.sort(key=lambda e: e["z"])  # contradicts drawn last → on top

    return {"nodes": nodes, "links": edge_data}


# ---------------------------------------------------------------------------
# HTML template
# The template uses {d3_source} and {graph_json} as injection points.
# All other braces are doubled to survive .format().
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Janus — Contradiction Catcher</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}}
  #canvas{{width:100vw;height:100vh}}
  svg{{width:100%;height:100%}}

  /* ---- contradiction pulse animation ---- */
  @keyframes dash-march {{
    to {{ stroke-dashoffset: -24; }}
  }}
  .edge-contradicts {{
    animation: dash-march 0.7s linear infinite;
  }}

  /* ---- tooltip ---- */
  #tooltip {{
    position:fixed;
    background:#161b22;
    border:1px solid #30363d;
    border-radius:6px;
    padding:12px 16px;
    font-size:12px;
    max-width:340px;
    line-height:1.55;
    pointer-events:none;
    display:none;
    z-index:999;
    box-shadow:0 4px 20px rgba(0,0,0,.6);
  }}
  #tooltip .tt-kind    {{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin-bottom:4px}}
  #tooltip .tt-text    {{font-weight:600;font-size:13px;color:#e6edf3;margin-bottom:6px;white-space:pre-wrap;word-break:break-word}}
  #tooltip .tt-reason  {{color:#c9d1d9;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow-y:auto}}
  #tooltip .tt-meta    {{margin-top:8px;font-size:10px;color:#6e7681;display:flex;gap:12px;flex-wrap:wrap}}
  #tooltip .tt-stance  {{font-size:10px;padding:1px 6px;border-radius:3px;background:#21262d;color:#8b949e}}
  #tooltip .tt-ts      {{font-size:10px;color:#6e7681}}

  /* ---- contradiction badge on tooltip ---- */
  #tooltip .tt-conflict {{
    margin-top:8px;
    padding:4px 8px;
    border-radius:4px;
    background:rgba(229,57,53,.15);
    border-left:3px solid #e53935;
    font-size:11px;
    color:#ff6b6b;
    font-weight:600;
  }}

  /* ---- legend ---- */
  #legend {{
    position:fixed;
    bottom:16px;
    left:16px;
    background:rgba(22,27,34,.92);
    border:1px solid #30363d;
    border-radius:6px;
    padding:10px 14px;
    font-size:11px;
    line-height:2;
  }}
  #legend .dot {{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}}
  #legend .eline {{display:inline-block;width:24px;height:3px;margin-right:6px;vertical-align:middle;border-radius:2px}}
  #legend .section {{color:#6e7681;font-size:10px;margin-top:4px;text-transform:uppercase;letter-spacing:.07em}}

  /* ---- stats ---- */
  #stats {{position:fixed;top:14px;right:16px;font-size:11px;color:#8b949e;text-align:right;line-height:1.7}}
  #stats .conflict-count {{color:#e53935;font-weight:700}}

  /* ---- search ---- */
  #search-wrap {{position:fixed;top:14px;left:16px}}
  #search-wrap input {{
    background:#161b22;
    border:1px solid #30363d;
    border-radius:6px;
    padding:5px 10px;
    color:#e6edf3;
    font-size:12px;
    outline:none;
    width:210px;
  }}
  #search-wrap input:focus{{border-color:#58a6ff}}

  /* ---- node text ---- */
  .node-label{{fill:#e6edf3;font-size:10px;pointer-events:none}}
  .node-ts   {{fill:#6e7681;font-size:9px;pointer-events:none}}

  /* ---- edge label ---- */
  .edge-label{{font-size:10px;pointer-events:none;text-anchor:middle}}
</style>
</head>
<body>
<div id="canvas"><svg id="svg"></svg></div>
<div id="tooltip"></div>
<div id="search-wrap"><input id="search" type="text" placeholder="Search claims…" autocomplete="off"></div>
<div id="stats"></div>
<div id="legend"></div>

<script>
/* ---- D3 v7 inlined ---- */
{d3_source}
</script>

<script>
(function(){{
  const GRAPH = {graph_json};

  /* ---- helpers ---- */
  function esc(s){{
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  /* ---- canvas ---- */
  const W=window.innerWidth, H=window.innerHeight;
  const svg=d3.select('#svg').attr('viewBox',[0,0,W,H]);
  const root=svg.append('g');

  svg.call(
    d3.zoom().scaleExtent([0.05,8])
      .on('zoom', e=>root.attr('transform',e.transform))
  );

  /* ---- counts ---- */
  const conflictEdges=GRAPH.links.filter(l=>l.relation==='contradicts');
  const statsEl=document.getElementById('stats');
  statsEl.innerHTML=
    `${{GRAPH.nodes.length}} claims &nbsp;·&nbsp; ${{GRAPH.links.length}} edges<br>`+
    (conflictEdges.length
      ? `<span class="conflict-count">⚡ ${{conflictEdges.length}} contradiction${{conflictEdges.length>1?'s':''}}</span>`
      : '<span style="color:#59a14f">✓ no contradictions yet</span>');

  /* ---- legend ---- */
  const legendEl=document.getElementById('legend');
  legendEl.innerHTML=
    '<div class="section">Node kind</div>'+
    '<div><span class="dot" style="background:#4e79a7"></span>Claim</div>'+
    '<div><span class="dot" style="background:#f28e2b"></span>Decision</div>'+
    '<div><span class="dot" style="background:#59a14f"></span>Belief</div>'+
    '<div class="section" style="margin-top:6px">Edge type</div>'+
    '<div><span class="eline" style="background:#e53935"></span>contradicts ⚡</div>'+
    '<div><span class="eline" style="background:#fb8c00"></span>supersedes</div>'+
    '<div><span class="eline" style="background:#8b949e"></span>supports / relates-to</div>';

  /* ---- weight → stroke-width ---- */
  const wScale=d3.scaleLinear().domain([0.5,5]).range([1,6]);

  /* ---- simulation ---- */
  const sim=d3.forceSimulation(GRAPH.nodes)
    .force('link', d3.forceLink(GRAPH.links)
      .id(d=>d.id)
      .distance(d=>d.relation==='contradicts'?160:d.relation==='supersedes'?120:90))
    .force('charge', d3.forceManyBody().strength(-350))
    .force('center', d3.forceCenter(W/2,H/2))
    .force('collide', d3.forceCollide().radius(d=>nodeR(d)+10))
    .alphaDecay(0.022);

  function nodeR(d){{
    const deg=GRAPH.links.filter(l=>
      (l.source.id||l.source)===d.id||(l.target.id||l.target)===d.id).length;
    return Math.max(10,Math.min(26,10+deg*2));
  }}

  /* ---- EDGES ----
     Render in z-order: supports/relates-to first, supersedes next,
     contradicts last so they sit on top of everything.
  ---- */
  const linkG=root.append('g');
  const link=linkG.selectAll('line')
    .data(GRAPH.links).join('line')
    .attr('stroke',          d=>d.color)
    .attr('stroke-width',    d=>wScale(d.weight)*d.width/3)
    .attr('stroke-opacity',  d=>d.opacity)
    .attr('stroke-dasharray',d=>d.dash||null)
    .attr('class', d=>d.relation==='contradicts'?'edge-contradicts':null);

  /* edge labels — only for contradicts/supersedes to reduce clutter */
  const edgeLabelG=root.append('g');
  const edgeLabel=edgeLabelG.selectAll('text')
    .data(GRAPH.links.filter(l=>l.relation==='contradicts'||l.relation==='supersedes'))
    .join('text')
    .attr('class','edge-label')
    .attr('fill',d=>d.labelFill)
    .attr('font-weight',d=>d.relation==='contradicts'?'700':'400')
    .text(d=>d.relation==='contradicts'?'⚡ CONTRADICTS':d.relation);

  /* ---- NODES ---- */
  const nodeG=root.append('g');
  const node=nodeG.selectAll('g')
    .data(GRAPH.nodes).join('g')
    .attr('class','node')
    .call(d3.drag()
      .on('start',dragStart)
      .on('drag', dragged)
      .on('end',  dragEnd));

  /* circle */
  node.append('circle')
    .attr('r',nodeR)
    .attr('fill',d=>d.color)
    .attr('stroke',d=>d.kind==='claim'?'#21262d':'#21262d')
    .attr('stroke-width',1.5)
    .style('cursor','pointer');

  /* glow ring for nodes that have a contradiction edge */
  const conflictNodeIds=new Set();
  conflictEdges.forEach(e=>{{
    conflictNodeIds.add(e.source.id||e.source);
    conflictNodeIds.add(e.target.id||e.target);
  }});
  node.filter(d=>conflictNodeIds.has(d.id))
    .append('circle')
    .attr('r',d=>nodeR(d)+5)
    .attr('fill','none')
    .attr('stroke','#e53935')
    .attr('stroke-width',1.5)
    .attr('stroke-opacity',0.5)
    .attr('stroke-dasharray','4,3')
    .attr('class','edge-contradicts');  /* reuse dash-march animation */

  /* label: short claim text */
  node.append('text')
    .attr('class','node-label')
    .attr('dy', d=>nodeR(d)+12)
    .attr('text-anchor','middle')
    .text(d=>d.label);

  /* timestamp below label */
  node.append('text')
    .attr('class','node-ts')
    .attr('dy', d=>nodeR(d)+22)
    .attr('text-anchor','middle')
    .text(d=>d.short_ts);

  /* ---- tooltip ---- */
  const tip=document.getElementById('tooltip');

  node.on('mouseover',(e,d)=>{{
    /* find contradiction partners */
    const partners=GRAPH.links
      .filter(l=>l.relation==='contradicts'&&
        ((l.source.id||l.source)===d.id||(l.target.id||l.target)===d.id))
      .map(l=>{{
        const otherId=(l.source.id||l.source)===d.id
          ?(l.target.id||l.target):(l.source.id||l.source);
        const other=GRAPH.nodes.find(n=>n.id===otherId);
        return other?other.label:'?';
      }});

    tip.innerHTML=
      `<div class="tt-kind">${{esc(d.kind)}}</div>`+
      `<div class="tt-text">${{esc(d.text)}}</div>`+
      (d.reason?`<div class="tt-reason"><strong>Reason:</strong> ${{esc(d.reason)}}</div>`:'')+
      `<div class="tt-meta">`+
      (d.topic?`<span>📌 ${{esc(d.topic)}}</span>`:'')+
      (d.stance?`<span class="tt-stance">${{esc(d.stance)}}</span>`:'')+
      (d.timestamp?`<span class="tt-ts">${{esc(d.timestamp)}}</span>`:'')+
      `</div>`+
      (partners.length
        ? `<div class="tt-conflict">⚡ CONTRADICTS: ${{partners.map(esc).join(' · ')}}</div>`
        : '');
    tip.style.display='block';
    moveTip(e);
  }})
  .on('mousemove',e=>moveTip(e))
  .on('mouseout',()=>{{tip.style.display='none';}});

  function moveTip(e){{
    let x=e.clientX+14, y=e.clientY-10;
    if(x+360>W) x=e.clientX-360;
    if(y+260>H) y=e.clientY-260;
    tip.style.left=x+'px';
    tip.style.top=y+'px';
  }}

  /* ---- search / dim ---- */
  const searchEl=document.getElementById('search');
  searchEl.addEventListener('input',()=>{{
    const q=searchEl.value.trim().toLowerCase();
    node.select('circle:first-of-type').attr('opacity',d=>
      !q||d.text.toLowerCase().includes(q)||
      (d.reason||'').toLowerCase().includes(q)||
      (d.topic||'').toLowerCase().includes(q)?1:0.12);
    node.selectAll('text').attr('opacity',d=>
      !q||d.text.toLowerCase().includes(q)||
      (d.reason||'').toLowerCase().includes(q)?1:0.08);
    link.attr('stroke-opacity',l=>{{
      if(!q) return l.opacity;
      const sl=(l.source.text||''), tl=(l.target.text||'');
      return sl.toLowerCase().includes(q)||tl.toLowerCase().includes(q)
        ?l.opacity:0.04;
    }});
    edgeLabel.attr('opacity',l=>{{
      if(!q) return 1;
      const sl=(l.source.text||''), tl=(l.target.text||'');
      return sl.toLowerCase().includes(q)||tl.toLowerCase().includes(q)?1:0.04;
    }});
  }});

  /* ---- tick ---- */
  sim.on('tick',()=>{{
    link
      .attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
      .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);

    edgeLabel
      .attr('x',d=>(d.source.x+d.target.x)/2)
      .attr('y',d=>(d.source.y+d.target.y)/2-6);

    node.attr('transform',d=>`translate(${{d.x}},${{d.y}})`);
  }});

  /* ---- drag ---- */
  function dragStart(e,d){{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;}}
  function dragged(e,d){{d.fx=e.x;d.fy=e.y;}}
  function dragEnd(e,d){{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}}

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
    output_path: str | os.PathLike = "janus_graph.html",
) -> Path:
    """Render the Janus claim graph as a self-contained HTML file.

    Call this after every store() so the browser view stays live.

    Parameters
    ----------
    graph:
        networkx Graph or DiGraph with Janus node/edge attributes.
    output_path:
        Where to write the HTML.  Defaults to ``janus_graph.html``.

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

    n_conflict = sum(
        1 for _, _, d in graph.edges(data=True)
        if (d.get("relation") or "") == "contradicts"
    )
    print(
        f"[janus_viz] → {out}  "
        f"({graph.number_of_nodes()} claims, {graph.number_of_edges()} edges"
        + (f", {n_conflict} CONTRADICTIONS" if n_conflict else "")
        + ")"
    )
    return out


# ---------------------------------------------------------------------------
# CLI demo — python janus_viz.py
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Build a sample claim graph with deliberate contradictions and render it."""

    G = nx.DiGraph()

    claims = [
        ("c1", {
            "kind": "belief", "topic": "diet",
            "text": "Intermittent fasting is the best way to lose weight.",
            "reason": "Worked for 3 months — lost 8 kg.",
            "stance": "for", "timestamp": "2025-11-03T08:00:00",
        }),
        ("c2", {
            "kind": "belief", "topic": "diet",
            "text": "Caloric restriction alone is sufficient for weight loss; meal timing does not matter.",
            "reason": "Read meta-analysis on CICO; IF effect disappears when calories are matched.",
            "stance": "for", "timestamp": "2026-02-14T10:30:00",
        }),
        ("c3", {
            "kind": "decision", "topic": "tech-stack",
            "text": "We should use PostgreSQL for the project database.",
            "reason": "Mature, ACID, great ecosystem.",
            "stance": "for", "timestamp": "2025-09-01T09:00:00",
        }),
        ("c4", {
            "kind": "decision", "topic": "tech-stack",
            "text": "SQLite is good enough — we don't need Postgres overhead.",
            "reason": "Team is small, no concurrency issues, simpler ops.",
            "stance": "for", "timestamp": "2026-01-20T14:00:00",
        }),
        ("c5", {
            "kind": "claim", "topic": "career",
            "text": "Remote work kills team cohesion.",
            "reason": "Noticed less spontaneous collaboration at current job.",
            "stance": "against", "timestamp": "2024-06-15T11:00:00",
        }),
        ("c6", {
            "kind": "claim", "topic": "career",
            "text": "Remote-first teams outperform office teams when async processes are strong.",
            "reason": "GitLab all-remote handbook; own experience after joining async team.",
            "stance": "for", "timestamp": "2026-03-10T16:45:00",
        }),
        ("c7", {
            "kind": "belief", "topic": "diet",
            "text": "Protein intake above 1.6 g/kg provides no extra muscle-building benefit.",
            "reason": "Morton et al. 2018 meta-analysis.",
            "stance": "for", "timestamp": "2026-05-01T08:00:00",
        }),
        ("c8", {
            "kind": "claim", "topic": "tech-stack",
            "text": "TypeScript adds unnecessary complexity for small teams.",
            "reason": "Type ceremony slows iteration; Python or plain JS is faster to ship.",
            "stance": "against", "timestamp": "2025-03-20T09:00:00",
        }),
        ("c9", {
            "kind": "decision", "topic": "tech-stack",
            "text": "All new services must be written in TypeScript.",
            "reason": "Caught too many runtime type bugs; team agreed on TS as standard.",
            "stance": "for", "timestamp": "2026-06-10T15:00:00",
        }),
    ]
    G.add_nodes_from([(nid, attrs) for nid, attrs in claims])

    edges = [
        # contradictions (the money edges)
        ("c1", "c2", {"relation": "contradicts", "weight": 5}),
        ("c3", "c4", {"relation": "contradicts", "weight": 5}),
        ("c5", "c6", {"relation": "contradicts", "weight": 5}),
        ("c8", "c9", {"relation": "contradicts", "weight": 5}),
        # supersedes
        ("c6", "c5", {"relation": "supersedes",  "weight": 3}),
        ("c4", "c3", {"relation": "supersedes",  "weight": 3}),
        # supports / relates-to
        ("c2", "c7", {"relation": "supports",    "weight": 2}),
        ("c1", "c7", {"relation": "relates-to",  "weight": 1}),
        ("c3", "c9", {"relation": "relates-to",  "weight": 1}),
    ]
    G.add_edges_from([(u, v, d) for u, v, d in edges])

    out = refresh(G, "janus_graph.html")
    print(f"[demo] open {out} in a browser to preview.")


if __name__ == "__main__":
    _demo()

