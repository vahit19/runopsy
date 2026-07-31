"""Self-contained HTML report.

One file, no build step, no network requests, no external assets. A report that needs a
server to read is a report nobody reads, and one that phones out to a CDN cannot be
opened on the isolated machine where the interesting failures usually happen.

Node identities are the same ones the terminal prints, so ``step 9`` in ``runopsy
diagnose`` is the element with ``id="node-…"`` here. That is not a nicety: a visual and
a textual view that disagree about which step is which turn a debugging session into an
argument about tooling.
"""

from __future__ import annotations

from html import escape
from typing import Final

from runopsy_collector import RunSummary
from runopsy_core.impact import infer_affects
from runopsy_core.schema import (
    DiagnosisBundle,
    DiagnosisCandidate,
    TraceGraph,
    TraceNode,
)

REDACTED: Final = "[redacted]"
DIGEST_PREVIEW: Final = 14
NODE_SPACING: Final = 78
NODE_RADIUS: Final = 13
RIBBON_HEIGHT: Final = 150
ARC_TOP: Final = 26

_STYLE: Final = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1d21; --muted: #5c6672; --line: #d8dee6;
  --panel: #f6f8fa; --observed: #c2352b; --onset: #b26a00; --affected: #4a6fa5;
  --ok: #2f7d44;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --fg: #e6e9ed; --muted: #9aa4b0; --line: #2c3238;
    --panel: #1c2024; --observed: #ff7b70; --onset: #f0a13a; --affected: #7fa8e0;
    --ok: #6fcf8a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
}
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 2.25rem 0 .6rem; letter-spacing: .01em; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: .9rem 1.1rem;
        background: var(--panel); margin-bottom: .75rem; }
.card.observed { border-left: 4px solid var(--observed); }
.card.onset { border-left: 4px solid var(--onset); }
.tag { font-size: .72rem; text-transform: uppercase; letter-spacing: .07em;
       color: var(--muted); display: block; margin-bottom: .3rem; }
.step { font-weight: 650; }
.meta { color: var(--muted); font-size: .86rem; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px;
          padding: .5rem; background: var(--panel); }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th { text-align: left; font-weight: 600; color: var(--muted); padding: .35rem .6rem;
     border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: .35rem .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
tr.is-observed td { background: color-mix(in srgb, var(--observed) 12%, transparent); }
tr.is-onset td { background: color-mix(in srgb, var(--onset) 14%, transparent); }
tr.is-affected td { background: color-mix(in srgb, var(--affected) 9%, transparent); }
code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .85em; }
.legend { display: flex; gap: 1.1rem; flex-wrap: wrap; color: var(--muted);
          font-size: .82rem; margin: .5rem 0 0; }
.swatch { display: inline-block; width: .7rem; height: .7rem; border-radius: 2px;
          margin-right: .35rem; vertical-align: -1px; }
.note { border: 1px solid var(--line); border-radius: 10px; padding: .8rem 1.1rem;
        margin-top: 1.5rem; color: var(--muted); font-size: .9rem; }
.note code { color: var(--fg); }
ul.plain { list-style: none; padding: 0; margin: .3rem 0 0; }
ul.plain li { padding: .1rem 0; color: var(--muted); font-size: .88rem; }
"""


def _fmt(value: object) -> str:
    text = str(value)
    if text.startswith("sha256:"):
        text = f"{text[:DIGEST_PREVIEW]}…"
    return escape(text)


def _node_role(node: TraceNode, bundle: DiagnosisBundle, onset: DiagnosisCandidate | None) -> str:
    """Which highlight a step gets. Order matters: the symptom outranks everything."""
    if node.node_id == bundle.observed_failure_node_id:
        return "observed"
    if onset is not None and node.node_id == onset.onset_node_id:
        return "onset"
    if onset is not None and node.node_id in onset.affected_node_ids:
        return "affected"
    return ""


def _redact_attributes(node: TraceNode, redact: bool) -> dict[str, object]:
    """Drop payload values for steps the scanner flagged.

    The trace format is the primary privacy control — content is referenced by hash, not
    stored — so this is a second layer covering the fields that do carry text, such as
    paths and memory keys. Timing, kind and status survive, because the shape of the run
    is what makes a redacted report still worth reading.
    """
    if not redact:
        return node.attributes
    flagged = bool(node.attributes.get("contains_secret"))
    if not flagged:
        return node.attributes
    preserved = {"name", "status", "exit_code", "duration_ms", "model", "operation"}
    return {
        key: (value if key in preserved else REDACTED) for key, value in node.attributes.items()
    }


def _ribbon(graph: TraceGraph, bundle: DiagnosisBundle, onset: DiagnosisCandidate | None) -> str:
    """A left-to-right map of the run with inferred propagation drawn above it."""
    nodes = [node for node in graph.in_order() if node.kind.value not in {"run", "agent"}]
    if not nodes:
        return ""

    index = {node.node_id: position for position, node in enumerate(nodes)}
    width = max(len(nodes) * NODE_SPACING, 320)
    baseline = RIBBON_HEIGHT - 46
    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {RIBBON_HEIGHT}" width="{width}" height="{RIBBON_HEIGHT}" '
        f'role="img" aria-label="Execution timeline with inferred propagation">'
    ]

    if onset is not None:
        for edge in infer_affects(graph, onset.onset_node_id):
            if edge.source_id not in index or edge.target_id not in index:
                continue
            x1 = index[edge.source_id] * NODE_SPACING + NODE_SPACING / 2
            x2 = index[edge.target_id] * NODE_SPACING + NODE_SPACING / 2
            lift = min(ARC_TOP + abs(x2 - x1) / 4, baseline - 8)
            parts.append(
                f'<path d="M {x1} {baseline} Q {(x1 + x2) / 2} {baseline - lift} {x2} {baseline}" '
                f'fill="none" stroke="var(--affected)" stroke-width="1.4" '
                f'stroke-dasharray="4 3" opacity="{edge.confidence:.2f}" />'
            )

    for node in nodes:
        position = index[node.node_id]
        x = position * NODE_SPACING + NODE_SPACING / 2
        role = _node_role(node, bundle, onset)
        colour = {
            "observed": "var(--observed)",
            "onset": "var(--onset)",
            "affected": "var(--affected)",
        }.get(role, "var(--line)")
        if position + 1 < len(nodes):
            parts.append(
                f'<line x1="{x + NODE_RADIUS}" y1="{baseline}" '
                f'x2="{x + NODE_SPACING - NODE_RADIUS}" y2="{baseline}" '
                f'stroke="var(--line)" stroke-width="1.5" />'
            )
        parts.append(
            f'<g id="ribbon-{escape(node.node_id)}">'
            f'<circle cx="{x}" cy="{baseline}" r="{NODE_RADIUS}" fill="{colour}" '
            f'stroke="var(--bg)" stroke-width="2"><title>step {node.sequence} '
            f"{escape(node.label)}</title></circle>"
            f'<text x="{x}" y="{baseline + 30}" text-anchor="middle" font-size="11" '
            f'fill="var(--muted)">{node.sequence}</text></g>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _timeline(
    graph: TraceGraph, bundle: DiagnosisBundle, onset: DiagnosisCandidate | None, redact: bool
) -> str:
    rows: list[str] = []
    for node in graph.in_order():
        if node.kind.value in {"run", "agent"}:
            continue
        role = _node_role(node, bundle, onset)
        attributes = _redact_attributes(node, redact)
        detail = ", ".join(
            f"{escape(key)}={_fmt(value)}"
            for key, value in sorted(attributes.items())
            if key not in {"state_delta"} and value not in (None, "", {}, [])
        )
        rows.append(
            f'<tr id="node-{escape(node.node_id)}" data-step="{node.sequence}"'
            f"{f' class=is-{role}' if role else ''}>"
            f"<td class=step>{node.sequence}</td>"
            f"<td>{escape(node.kind.value)}</td>"
            f"<td>{escape(node.label)}</td>"
            f"<td class=meta>{detail}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>step</th><th>kind</th><th>label</th>'
        f"<th>detail</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _candidate_card(candidate: DiagnosisCandidate, graph: TraceGraph, kind: str) -> str:
    node = graph.node(candidate.onset_node_id)
    where = (
        f"step {node.sequence} {escape(node.label)}" if node else escape(candidate.onset_node_id)
    )
    percent = round(candidate.confidence * 100)
    verdict = "validated" if candidate.is_definitive else "unverified"
    breakdown = "".join(
        f"<li>{escape(key)}: {value}</li>" for key, value in candidate.score_breakdown.items()
    )
    return (
        f'<div class="card {kind}">'
        f'<span class="tag">{escape(kind_label(kind))}</span>'
        f'<div class="step">{where}</div>'
        f"<div>{escape(candidate.summary)}</div>"
        f'<div class="meta">{percent}% confidence, {verdict}</div>'
        f'<ul class="plain">{breakdown}</ul>'
        f"</div>"
    )


def kind_label(kind: str) -> str:
    return {
        "observed": "Observed failure — what the run visibly got wrong",
        "onset": "Suspected onset — unverified",
    }.get(kind, kind)


def render_report(
    bundle: DiagnosisBundle,
    graph: TraceGraph,
    summary: RunSummary | None,
    *,
    redact: bool = True,
) -> str:
    """Build the complete HTML document as a string."""
    onset = next(
        (c for c in bundle.candidates if c.onset_node_id != bundle.observed_failure_node_id),
        None,
    )

    title = f"Runopsy — {bundle.run_id}"
    task = escape(summary.task) if summary and summary.task else ""
    meta = ""
    if summary is not None:
        state = summary.outcome.value if summary.is_finished else "unfinished"
        meta = f"{escape(summary.runtime)} · {summary.event_count} events · {escape(state)}"

    body: list[str] = [f"<h1>{escape(bundle.run_id)}</h1>"]
    if task:
        body.append(f'<p class="sub">{task}<br>{meta}</p>')
    elif meta:
        body.append(f'<p class="sub">{meta}</p>')

    if not bundle.candidates:
        body.append(
            '<div class="card"><span class="tag">Result</span>'
            "<div>Nothing detectable went wrong in this run.</div></div>"
        )
    else:
        if bundle.observed_failure_node_id is not None:
            observed = next(
                (
                    c
                    for c in bundle.candidates
                    if c.onset_node_id == bundle.observed_failure_node_id
                ),
                None,
            )
            if observed is not None:
                body.append(_candidate_card(observed, graph, "observed"))
        if onset is not None:
            body.append(_candidate_card(onset, graph, "onset"))

    ribbon = _ribbon(graph, bundle, onset)
    if ribbon:
        body.append("<h2>Execution and inferred propagation</h2>")
        body.append(f'<div class="scroll">{ribbon}</div>')
        body.append(
            '<p class="legend">'
            '<span><span class="swatch" style="background:var(--observed)"></span>'
            "observed failure</span>"
            '<span><span class="swatch" style="background:var(--onset)"></span>'
            "suspected onset</span>"
            '<span><span class="swatch" style="background:var(--affected)"></span>'
            "may have been affected</span>"
            "<span>dashed arcs are inferred, and fade as confidence drops</span></p>"
        )

    body.append("<h2>Timeline</h2>")
    body.append(_timeline(graph, bundle, onset, redact))

    if onset is not None and not onset.is_definitive:
        node = graph.node(onset.onset_node_id)
        step = f" --from-step {node.sequence}" if node else ""
        body.append(
            '<div class="note">No cause has been confirmed. Nothing here is proof of '
            "causation — the propagation arcs show reachability, not a demonstrated "
            "effect. To test the suspected onset, replay from it:<br>"
            f"<code>runopsy replay {escape(bundle.run_id)}{escape(step)}</code></div>"
        )

    if redact:
        body.append(
            '<p class="legend">Values from steps flagged as containing secrets are '
            "redacted. Prompts, arguments and file contents are referenced by hash and "
            "were never stored in the trace.</p>"
        )

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{''.join(body)}</main></body></html>\n"
    )
