"""Terminal rendering.

Layout follows the reading order a person actually needs: what broke, where it probably
started, and what to do next. Supporting detail comes after, so the answer is visible
without scrolling on a normal terminal.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from runopsy_cli.language import confidence_phrase, gloss, heading, next_step_hint, style
from runopsy_collector import PrunePlan, RunSummary
from runopsy_core.schema import DiagnosisBundle, DiagnosisCandidate, RunOutcome, TraceGraph
from runopsy_replay import ReplayPlan, ReplayVerdict, StepAction

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

    # A run can end successfully with failed steps inside it — an agent that retries and
    # recovers is the ordinary case, not an anomaly. Calling that "what the run visibly
    # got wrong" overstates it: the run got nothing wrong, a step did. The first real
    # session recorded ended in success with three failed patches in the middle of it.
    recovered = (
        summary is not None and summary.is_finished and summary.outcome is RunOutcome.SUCCESS
    )
    if bundle.observed_failure_node_id is not None:
        if recovered:
            observed = Text("\nRecovered failure", style="bold yellow")
            observed.append("  (the run succeeded; this step did not)\n", style="dim")
        else:
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


def replay_verdict(verdict: ReplayVerdict) -> RenderableType:
    """What the experiment showed — with its epistemic weight stated, not implied."""
    parts: list[RenderableType] = []

    header = Text()
    header.append(f"Replay {verdict.replay_run_id}", style="bold")
    if verdict.intervened:
        header.append(
            f"\nintervention: {verdict.intervention_kind} at step {verdict.intervention_target}",
            style="dim",
        )
    else:
        header.append("\nno intervention: a straight re-run", style="dim")
    parts.append(header)

    ran = [step for step in verdict.executed if step.ran]
    lines = Text("\n")
    lines.append(f"{len(ran)} step(s) re-ran", style="dim")
    if verdict.skipped:
        lines.append(f", {len(verdict.skipped)} skipped", style="dim")
    lines.append(".\n", style="dim")
    for step in verdict.skipped:
        lines.append(f"  step {step.original_sequence}: {step.skipped_reason}\n", style="dim")
    parts.append(lines)

    if verdict.supports_onset:
        outcome = Text()
        outcome.append("Cause, supported by replay\n", style="bold green")
        outcome.append(
            "Changing the suspected onset made the downstream failures disappear. "
            "The diagnosis for the original run now reflects this:\n",
        )
        outcome.append(f"  runopsy diagnose {verdict.parent_run_id}\n", style="dim")
        parts.append(outcome)
    elif verdict.intervened:
        outcome = Text()
        outcome.append("Not supported\n", style="bold yellow")
        outcome.append(
            "The downstream failures did not disappear when the onset was changed. "
            "Either this step is not the cause, or the failure depends on state the "
            "sandbox could not reproduce.\n"
        )
        parts.append(outcome)
    elif verdict.reproduced:
        outcome = Text()
        outcome.append("Reproduced\n", style="bold")
        outcome.append(
            "The same steps failed again. That shows the failure is stable enough to "
            "study — it does not show what caused it. To test the suspected onset, "
            "re-run with --skip-onset or --substitute.\n",
            style="dim",
        )
        parts.append(outcome)
    else:
        parts.append(
            Text(
                "Inconclusive: the failing steps could not be compared, so this replay "
                "neither supports nor weakens the diagnosis.",
                style="yellow",
            )
        )

    return Group(*parts)


MAX_PAYLOAD_LINES = 12
MAX_PAYLOAD_CHARS = 1200


def _payload_block(
    body: Text,
    node: object,
    resolve: Callable[[str], str | None] | None,
    *,
    reveal: bool,
) -> None:
    """Show what the step actually ran and what came back.

    A hash answers "was it the same call as before" and nothing else. Someone reading
    the evidence for a flagged step wants the command, and printing only digests made
    this view technically complete and practically useless.

    Steps flagged as carrying a credential stay withheld unless asked for, matching
    ``runopsy export``. What the vault holds is already redacted, so revealing shows the
    censored form rather than the secret.
    """
    if resolve is None:
        return
    attributes = getattr(node, "attributes", {}) or {}
    flagged = bool(attributes.get("contains_secret"))

    for label, key in (("command", "arguments_hash"), ("output", "output_hash")):
        digest = attributes.get(key)
        if not isinstance(digest, str) or not digest:
            continue
        if flagged and not reveal:
            body.append(f"  {label}: ", style="dim")
            body.append("withheld — flagged as carrying a credential; ", style="yellow")
            body.append("pass --include-sensitive\n", style="dim")
            continue
        text = resolve(digest)
        if text is None:
            body.append(f"  {label}: ", style="dim")
            body.append("not kept locally (vault off, or pruned)\n", style="dim")
            continue
        body.append(f"  {label}\n", style="dim")
        for line in _payload_lines(text):
            body.append(f"    {line}\n")


def _payload_lines(text: str) -> list[str]:
    truncated = text[:MAX_PAYLOAD_CHARS]
    lines = truncated.splitlines() or [""]
    if len(lines) > MAX_PAYLOAD_LINES:
        hidden = len(lines) - MAX_PAYLOAD_LINES
        lines = [*lines[:MAX_PAYLOAD_LINES], f"… {hidden} more line(s)"]
    elif len(text) > MAX_PAYLOAD_CHARS:
        lines.append(f"… {len(text) - MAX_PAYLOAD_CHARS} more character(s)")
    return lines


def evidence(
    bundle: DiagnosisBundle,
    graph: TraceGraph,
    node_id: str,
    *,
    resolve_payload: Callable[[str], str | None] | None = None,
    include_sensitive: bool = False,
) -> RenderableType:
    """The ``runopsy evidence`` view for one step."""
    node = graph.node(node_id)
    if node is None:
        return Text(f"No step {node_id} in run {bundle.run_id}.", style="red")

    body = Text()
    body.append(f"{_describe(graph, node_id)}\n", style="bold")
    body.append(
        f"{node.kind.value} · agent {node.agent_id} · {node.timestamp:%H:%M:%S}\n\n", style="dim"
    )
    _payload_block(body, node, resolve_payload, reveal=include_sensitive)

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


def prune_plan(plan: PrunePlan, retain_days: int) -> RenderableType:
    """What retention would remove, stated before anything is deleted."""
    parts: list[RenderableType] = []

    header = Text()
    header.append(f"Retention: keep runs from the last {retain_days} day(s)\n", style="bold")
    header.append(f"cutoff {plan.older_than:%Y-%m-%d %H:%M} UTC", style="dim")
    parts.append(header)

    if plan.is_empty:
        parts.append(Text("\nNothing has expired.", style="green"))
    else:
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("run")
        table.add_column("started")
        table.add_column("events", justify="right")
        table.add_column("task", overflow="ellipsis", max_width=34)
        for run in plan.expiring:
            started = f"{run.started_at:%Y-%m-%d}" if run.started_at else "unknown"
            table.add_row(run.run_id, started, str(run.event_count), run.task or "-")
        parts.append(Text(""))
        parts.append(table)
        parts.append(Text(f"\nWould remove {plan.describe()}.", style="yellow"))

    if plan.kept:
        parts.append(Text(f"Keeping {len(plan.kept)} run(s) inside the window.", style="dim"))
    if plan.undated:
        # Reported rather than silently skipped: a user wondering why a run survived
        # deserves the reason, and the reason is that we refuse to guess its age.
        parts.append(
            Text(
                f"{len(plan.undated)} run(s) have no recorded start and are never "
                "expired; an unknown age is not an old age.",
                style="dim",
            )
        )
    return Group(*parts)
