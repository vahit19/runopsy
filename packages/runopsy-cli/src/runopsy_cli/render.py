"""Terminal rendering.

Layout follows the reading order a person actually needs: what broke, where it probably
started, and what to do next. Supporting detail comes after, so the answer is visible
without scrolling on a normal terminal.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from runopsy_cli.language import confidence_phrase, gloss, heading, next_step_hint, style
from runopsy_collector import RunSummary
from runopsy_core.schema import DiagnosisBundle, DiagnosisCandidate, TraceGraph
from runopsy_replay import ReplayPlan, StepAction

MAX_LISTED_CANDIDATES = 5
DIGEST_PREVIEW = 14


def _format_value(value: object) -> str:
    """Render one attribute compactly.

    Digests are truncated because a full 64-character hash wraps the line and buries the
    field beside it, while the first few characters are enough to compare two steps by
    eye. The complete value stays available in ``--json``.
    """
    if isinstance(value, str) and value.startswith("sha256:"):
        return f"{value[:DIGEST_PREVIEW]}…"
    return str(value)


def _state_delta_lines(delta: object) -> list[str]:
    """Show state changes as transitions, which is how they are read."""
    if not isinstance(delta, dict):
        return [str(delta)]
    lines = []
    for key, change in sorted(delta.items()):
        if isinstance(change, dict):
            before = change.get("before")
            after = change.get("after")
            lines.append(f"{key}: {before!r} → {after!r}")
        else:
            lines.append(f"{key}: {change!r}")
    return lines


def _step_of(graph: TraceGraph, node_id: str) -> int | None:
    node = graph.node(node_id)
    return None if node is None else node.sequence


def _describe(graph: TraceGraph, node_id: str) -> str:
    node = graph.node(node_id)
    if node is None:
        return node_id
    label = f" {node.label}" if node.label else ""
    return f"step {node.sequence}{label}"


def runs_table(runs: tuple[RunSummary, ...]) -> RenderableType:
    """One row per recorded run."""
    if not runs:
        return Text("No runs recorded yet.", style="dim")

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("run")
    table.add_column("task", overflow="ellipsis", max_width=40)
    table.add_column("runtime")
    table.add_column("events", justify="right")
    table.add_column("outcome")

    for run in runs:
        outcome = run.outcome.value if run.is_finished else "unfinished"
        outcome_style = {"failure": "red", "success": "green"}.get(outcome, "yellow")
        table.add_row(
            run.run_id,
            run.task or "-",
            run.runtime,
            str(run.event_count),
            Text(outcome, style=outcome_style),
        )
    return table


def _candidate_block(
    bundle: DiagnosisBundle, graph: TraceGraph, candidate: DiagnosisCandidate
) -> RenderableType:
    lines = Text()
    lines.append(heading(candidate.status), style=style(candidate.status))
    lines.append(f"  ({gloss(candidate.status)})\n", style="dim")
    lines.append(f"  {_describe(graph, candidate.onset_node_id)}\n", style="bold")
    lines.append(f"  {candidate.summary}\n")
    lines.append(f"  {confidence_phrase(candidate)}\n", style="dim")

    if candidate.affected_node_ids:
        shown = ", ".join(_describe(graph, node_id) for node_id in candidate.affected_node_ids[:3])
        more = len(candidate.affected_node_ids) - 3
        suffix = f" and {more} more" if more > 0 else ""
        lines.append(f"  may have affected {shown}{suffix}\n", style="dim")

    step = _step_of(graph, candidate.onset_node_id)
    if step is not None:
        lines.append(f"  evidence: runopsy evidence {bundle.run_id} --step {step}\n", style="dim")
    return lines


def diagnosis(
    bundle: DiagnosisBundle, graph: TraceGraph, summary: RunSummary | None
) -> RenderableType:
    """The full ``runopsy diagnose`` view."""
    parts: list[RenderableType] = []

    header = Text()
    header.append(f"Run {bundle.run_id}", style="bold")
    if summary is not None and summary.task:
        header.append(f" — {summary.task}")
    if summary is not None:
        state = summary.outcome.value if summary.is_finished else "unfinished"
        header.append(f"\n{summary.runtime} · {summary.event_count} events · {state}", style="dim")
    parts.append(header)

    if not bundle.candidates:
        parts.append(
            Text(
                "\nNothing detectable went wrong in this run.\n"
                "Deterministic analysis found no failing step, loop, or state conflict.",
                style="green",
            )
        )
        return Group(*parts)

    if bundle.observed_failure_node_id is not None:
        observed = Text("\nObserved failure", style="bold red")
        observed.append("  (what the run visibly got wrong)\n", style="dim")
        observed.append(f"  {_describe(graph, bundle.observed_failure_node_id)}\n", style="bold")
        observed.append(f"  {bundle.observed_failure_summary}\n")
        parts.append(observed)

    leading = [
        candidate
        for candidate in bundle.candidates
        if candidate.onset_node_id != bundle.observed_failure_node_id
    ]
    if leading:
        parts.append(Text(""))
        parts.append(_candidate_block(bundle, graph, leading[0]))

    remaining = leading[1:MAX_LISTED_CANDIDATES]
    if remaining:
        others = Text("\nOther candidates\n", style="bold")
        for candidate in remaining:
            others.append(
                f"  {_describe(graph, candidate.onset_node_id)} — {candidate.summary} "
                f"({confidence_phrase(candidate)})\n",
                style="dim",
            )
        parts.append(others)

    primary = leading[0] if leading else bundle.candidates[0]
    hint = next_step_hint(bundle.run_id, primary, _step_of(graph, primary.onset_node_id))
    if hint:
        parts.append(Panel(hint, border_style="dim", padding=(0, 1)))

    return Group(*parts)


_ACTION_STYLE = {
    StepAction.REPLAY: ("replay ", "green"),
    StepAction.SANDBOX: ("sandbox", "cyan"),
    StepAction.APPROVE: ("approve", "yellow"),
    StepAction.BLOCK: ("blocked", "red"),
}


def replay_plan(plan: ReplayPlan) -> RenderableType:
    """The ``runopsy replay`` view: a proposal, with its own limits stated."""
    parts: list[RenderableType] = []

    header = Text()
    header.append(f"Replay plan for {plan.parent_run_id}", style="bold")
    header.append(f"\nfrom step {plan.from_sequence} · {plan.level.value}", style="dim")
    if plan.checkpoint_sequence is not None:
        header.append(f" · checkpoint at step {plan.checkpoint_sequence}", style="dim")
    else:
        header.append(" · no checkpoint available", style="dim")
    if plan.intervention.changed_fields():
        changed = ", ".join(plan.intervention.changed_fields())
        header.append(f"\nchanging: {changed}", style="dim")
    parts.append(header)

    actionable = [step for step in plan.steps if step.action is not StepAction.SKIP]
    if actionable:
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("step", justify="right")
        table.add_column("action")
        table.add_column("what")
        table.add_column("why", overflow="fold")
        for step in actionable:
            label, colour = _ACTION_STYLE[step.action]
            table.add_row(str(step.sequence), Text(label, style=colour), step.label, step.reason)
        parts.append(Text(""))
        parts.append(table)

    summary = Text("\n")
    summary.append(f"{len(plan.replayable)} step(s) would re-run", style="dim")
    if plan.blocked:
        summary.append(f", {len(plan.blocked)} excluded", style="red")
    if plan.needs_approval:
        summary.append(f", {len(plan.needs_approval)} need approval", style="yellow")
    summary.append(".\n", style="dim")
    parts.append(summary)

    for warning in plan.warnings:
        parts.append(Text(f"! {warning}", style="yellow"))

    parts.append(
        Panel(
            "This is a plan. Nothing was executed, no file was touched, and no tool was "
            "called again.",
            border_style="dim",
            padding=(0, 1),
        )
    )
    return Group(*parts)


def evidence(bundle: DiagnosisBundle, graph: TraceGraph, node_id: str) -> RenderableType:
    """The ``runopsy evidence`` view for one step."""
    node = graph.node(node_id)
    if node is None:
        return Text(f"No step {node_id} in run {bundle.run_id}.", style="red")

    body = Text()
    body.append(f"{_describe(graph, node_id)}\n", style="bold")
    body.append(
        f"{node.kind.value} · agent {node.agent_id} · {node.timestamp:%H:%M:%S}\n\n", style="dim"
    )

    for key, value in sorted(node.attributes.items()):
        if value in (None, "", {}, []):
            continue
        if key == "state_delta":
            body.append("  state changes\n", style="dim")
            for line in _state_delta_lines(value):
                body.append(f"    {line}\n")
            continue
        body.append(f"  {key}: ", style="dim")
        body.append(f"{_format_value(value)}\n")

    candidate = next((c for c in bundle.candidates if c.onset_node_id == node_id), None)
    if candidate is None:
        body.append("\nNo failure signal is attached to this step.\n", style="dim")
        return body

    body.append(f"\n{heading(candidate.status)}", style=style(candidate.status))
    body.append(f"  ({confidence_phrase(candidate)})\n", style="dim")
    body.append(f"  {candidate.summary}\n")

    if candidate.signal_ids:
        body.append("\nSignals\n", style="bold")
        for signal_id in candidate.signal_ids:
            body.append(f"  {signal_id}\n", style="dim")

    if candidate.score_breakdown:
        body.append("\nWhy it ranked here\n", style="bold")
        for key, value in candidate.score_breakdown.items():
            body.append(f"  {key}: {value}\n", style="dim")

    if candidate.affected_node_ids:
        body.append("\nMay have affected\n", style="bold")
        for affected in candidate.affected_node_ids[:10]:
            body.append(f"  {_describe(graph, affected)}\n", style="dim")

    return body
