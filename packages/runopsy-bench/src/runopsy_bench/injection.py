"""Fault injection: taking a healthy run and breaking it on purpose.

The synthetic suite proves the ranking behaves as designed on traces written to exercise
it. That is a weaker claim than it sounds, because the same hand wrote the fixture and
the detector. Injection closes some of that gap: start from a *real* recorded run,
introduce one known fault at a known step, and check whether the engine finds the step we
broke.

Ground truth is exact by construction — we chose the step — so accuracy here is a
measurement rather than a judgement. What it still cannot tell you is whether the fault
resembles how agents actually fail; that needs the opt-in corpus, and section 17.1 lists
it as a separate layer for exactly this reason.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from runopsy_core.schema import (
    CallStatus,
    Event,
    FailureCategory,
    LlmCallEvent,
    MemoryOperation,
    MemoryOpEvent,
    MemoryPayload,
    RunEndEvent,
    RunOutcome,
    ToolCallEvent,
)


class FaultKind(StrEnum):
    """What was done to the run."""

    TOOL_FAILURE = "tool_failure"
    """A succeeding step is made to fail."""

    TIMEOUT = "timeout"
    """A step is made to time out."""

    RETRY_STORM = "retry_storm"
    """A step is followed by repeated failing retries of itself."""

    TRUNCATED_PLAN = "truncated_plan"
    """A model call is cut short by the length limit."""

    STALE_MEMORY = "stale_memory"
    """A very old memory read is inserted before a step."""

    DROPPED_EVENTS = "dropped_events"
    """Events are removed, leaving a gap in the recording."""

    SILENT_WRONG_VALUE = "silent_wrong_value"
    """A step still succeeds but records a different state value.

    Included knowing the deterministic layers cannot reach it. A suite that only
    contains faults the engine can find measures nothing about its limits.
    """


@dataclass(frozen=True)
class InjectedFault:
    """A trace that was broken on purpose, and the record of how."""

    kind: FaultKind
    category: FailureCategory
    events: tuple[Event, ...]
    onset_step: int
    deterministically_detectable: bool = True
    scores_onset: bool = True
    """Whether "which step" is even the right question for this fault.

    False when the injection removes the onset from the trace: there is no step left to
    point at, and scoring it on localization would report a meaningless zero next to
    figures that mean something. Those cases are judged on whether the engine notices
    the evidence is incomplete.
    """

    note: str = ""


def _copy_with(event: Event, **changes: object) -> Event:
    return event.model_copy(update=changes)


def _tool_steps(events: Sequence[Event]) -> list[ToolCallEvent]:
    return [event for event in events if isinstance(event, ToolCallEvent)]


def _mark_failed(events: Sequence[Event], target: int) -> list[Event]:
    out: list[Event] = []
    for event in events:
        if isinstance(event, ToolCallEvent) and event.sequence == target:
            payload = event.tool.model_copy(update={"exit_code": 1, "status": CallStatus.ERROR})
            out.append(_copy_with(event, tool=payload))
        else:
            out.append(event)
    return out


def _fail_the_run(events: Sequence[Event]) -> list[Event]:
    """Make the recorded outcome match the fault we introduced.

    Without this the run would claim success while carrying an injected failure, which
    is a *different* fault (outcome mismatch) and would make the label wrong.
    """
    out: list[Event] = []
    for event in events:
        if isinstance(event, RunEndEvent):
            out.append(
                _copy_with(event, run=event.run.model_copy(update={"outcome": RunOutcome.FAILURE}))
            )
        else:
            out.append(event)
    return out


def inject_tool_failure(events: Sequence[Event], target: int) -> InjectedFault:
    return InjectedFault(
        kind=FaultKind.TOOL_FAILURE,
        category=FailureCategory.TOOL_EXECUTION,
        events=tuple(_fail_the_run(_mark_failed(events, target))),
        onset_step=target,
    )


def inject_timeout(events: Sequence[Event], target: int) -> InjectedFault:
    out: list[Event] = []
    for event in events:
        if isinstance(event, ToolCallEvent) and event.sequence == target:
            payload = event.tool.model_copy(update={"status": CallStatus.TIMEOUT, "exit_code": 124})
            out.append(_copy_with(event, tool=payload))
        else:
            out.append(event)
    return InjectedFault(
        kind=FaultKind.TIMEOUT,
        category=FailureCategory.TOOL_EXECUTION,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
    )


def inject_retry_storm(events: Sequence[Event], target: int, *, repeats: int = 4) -> InjectedFault:
    """Follow a step with failing retries of itself, renumbering what comes after."""
    ordered = sorted(events, key=lambda event: event.sequence)
    origin = next(
        (e for e in ordered if isinstance(e, ToolCallEvent) and e.sequence == target), None
    )
    if origin is None:
        msg = f"no tool call at step {target}"
        raise ValueError(msg)

    out: list[Event] = []
    for event in ordered:
        shifted = event.sequence + (repeats if event.sequence > target else 0)
        if event is origin:
            failed = origin.tool.model_copy(update={"exit_code": 1, "status": CallStatus.ERROR})
            out.append(_copy_with(origin, tool=failed))
            for index in range(repeats):
                retry = origin.tool.model_copy(
                    update={
                        "exit_code": 1,
                        "status": CallStatus.ERROR,
                        "retry_of": origin.event_id,
                    }
                )
                out.append(
                    _copy_with(
                        origin,
                        event_id=f"{origin.event_id}_retry{index}",
                        sequence=target + index + 1,
                        tool=retry,
                    )
                )
        else:
            out.append(_copy_with(event, sequence=shifted))
    return InjectedFault(
        kind=FaultKind.RETRY_STORM,
        category=FailureCategory.CONTROL_FLOW,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
    )


def inject_truncated_plan(events: Sequence[Event], target: int) -> InjectedFault:
    out: list[Event] = []
    found = False
    for event in events:
        if isinstance(event, LlmCallEvent) and event.sequence == target:
            found = True
            out.append(
                _copy_with(event, llm=event.llm.model_copy(update={"finish_reason": "length"}))
            )
        else:
            out.append(event)
    if not found:
        msg = f"no model call at step {target}"
        raise ValueError(msg)
    return InjectedFault(
        kind=FaultKind.TRUNCATED_PLAN,
        category=FailureCategory.PLANNING,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
    )


def inject_stale_memory(events: Sequence[Event], target: int) -> InjectedFault:
    """Insert an ancient memory read at ``target``, shifting later steps along."""
    ordered = sorted(events, key=lambda event: event.sequence)
    anchor = next((e for e in ordered if e.sequence == target), None)
    if anchor is None:
        msg = f"no step {target}"
        raise ValueError(msg)

    stale = MemoryOpEvent(
        event_id=f"{anchor.run_id}_injected_memory",
        run_id=anchor.run_id,
        sequence=target,
        timestamp=anchor.timestamp,
        memory=MemoryPayload(
            operation=MemoryOperation.READ, key="deploy_target", age_seconds=900_000
        ),
    )
    out: list[Event] = [stale]
    out.extend(
        _copy_with(event, sequence=event.sequence + (1 if event.sequence >= target else 0))
        for event in ordered
    )
    return InjectedFault(
        kind=FaultKind.STALE_MEMORY,
        category=FailureCategory.MEMORY,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
    )


def inject_dropped_events(events: Sequence[Event], target: int) -> InjectedFault:
    """Remove a step, leaving the gap a crashed collector would leave.

    The engine should report the trace as unreliable rather than confidently blaming
    whatever sits beside the hole.
    """
    out = [event for event in events if event.sequence != target]
    return InjectedFault(
        kind=FaultKind.DROPPED_EVENTS,
        category=FailureCategory.VALIDATION,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
        scores_onset=False,
        note="the onset itself was removed; the engine should flag the gap",
    )


def inject_silent_wrong_value(events: Sequence[Event], target: int) -> InjectedFault:
    """Change a state value without changing any status.

    Nothing about the step becomes anomalous, so no deterministic detector can see it.
    Labelled undetectable, kept in the suite, and reported as a coverage gap.
    """
    out: list[Event] = []
    for event in events:
        if event.sequence == target:
            out.append(_copy_with(event, state_delta={}))
        else:
            out.append(event)
    return InjectedFault(
        kind=FaultKind.SILENT_WRONG_VALUE,
        category=FailureCategory.TOOL_SELECTION,
        events=tuple(_fail_the_run(out)),
        onset_step=target,
        deterministically_detectable=False,
        note="no status changes; only semantic analysis or replay can reach this",
    )


INJECTORS: Final[dict[FaultKind, Callable[[Sequence[Event], int], InjectedFault]]] = {
    FaultKind.TOOL_FAILURE: inject_tool_failure,
    FaultKind.TIMEOUT: inject_timeout,
    FaultKind.RETRY_STORM: inject_retry_storm,
    FaultKind.TRUNCATED_PLAN: inject_truncated_plan,
    FaultKind.STALE_MEMORY: inject_stale_memory,
    FaultKind.DROPPED_EVENTS: inject_dropped_events,
    FaultKind.SILENT_WRONG_VALUE: inject_silent_wrong_value,
}


def applicable_kinds(events: Sequence[Event], target: int) -> tuple[FaultKind, ...]:
    """Which faults can be injected at this step of this run."""
    step = next((event for event in events if event.sequence == target), None)
    if step is None:
        return ()
    kinds = [FaultKind.STALE_MEMORY, FaultKind.DROPPED_EVENTS]
    if isinstance(step, ToolCallEvent):
        kinds += [
            FaultKind.TOOL_FAILURE,
            FaultKind.TIMEOUT,
            FaultKind.RETRY_STORM,
            FaultKind.SILENT_WRONG_VALUE,
        ]
    if isinstance(step, LlmCallEvent):
        kinds.append(FaultKind.TRUNCATED_PLAN)
    return tuple(sorted(kinds))


def inject(kind: FaultKind, events: Sequence[Event], target: int) -> InjectedFault:
    """Apply one named fault at one step."""
    return INJECTORS[kind](events, target)


@dataclass(frozen=True)
class InjectionScore:
    """How the engine did against faults it was not written from."""

    kind: FaultKind
    exact: int
    within_top3: int
    scored: int
    skipped_undetectable: int
    measure: str = "onset"
    """What the numbers mean: localizing the onset, or noticing a damaged trace."""

    @property
    def top1(self) -> float:
        return self.exact / self.scored if self.scored else 0.0

    @property
    def top3(self) -> float:
        return self.within_top3 / self.scored if self.scored else 0.0


def score_injections(
    events: Sequence[Event], *, kinds: Sequence[FaultKind] | None = None
) -> tuple[InjectionScore, ...]:
    """Inject every applicable fault into a run and score the engine on each kind.

    Imported here rather than at module scope so the fixture generators stay usable
    without pulling in the analysis engine.
    """
    from runopsy_core import AnalysisContext, diagnose

    tally: dict[FaultKind, list[int]] = {}
    skipped: dict[FaultKind, int] = {}
    measures: dict[FaultKind, str] = {}

    for fault in injection_campaign(events, kinds=kinds):
        if not fault.deterministically_detectable:
            skipped[fault.kind] = skipped.get(fault.kind, 0) + 1
            continue
        context = AnalysisContext.from_events(fault.events[0].run_id, fault.events)
        results = tally.setdefault(fault.kind, [0, 0, 0])
        results[2] += 1

        if not fault.scores_onset:
            # Nothing to localize: the step was removed. The question is whether the
            # engine reports the trace as incomplete instead of blaming a neighbour.
            measures[fault.kind] = "gap noticed"
            if not context.integrity.is_intact:
                results[0] += 1
                results[1] += 1
            continue

        measures[fault.kind] = "onset"
        bundle = diagnose(context)
        positions = {node.node_id: node.sequence for node in context.graph.nodes}
        predicted = [positions.get(c.onset_node_id) for c in bundle.candidates]
        if predicted and predicted[0] == fault.onset_step:
            results[0] += 1
        if fault.onset_step in predicted[:3]:
            results[1] += 1

    return tuple(
        InjectionScore(
            kind=kind,
            exact=values[0],
            within_top3=values[1],
            scored=values[2],
            skipped_undetectable=skipped.get(kind, 0),
            measure=measures.get(kind, "onset"),
        )
        for kind, values in sorted(tally.items(), key=lambda item: item[0].value)
    )


def injection_campaign(
    events: Sequence[Event], *, kinds: Sequence[FaultKind] | None = None
) -> tuple[InjectedFault, ...]:
    """Inject every applicable fault at every healthy step of a run.

    One fault per produced trace. Two at once would leave the label ambiguous — the same
    single-variable rule the replay executor holds itself to.
    """
    healthy = [
        event.sequence
        for event in _tool_steps(events)
        if event.tool.status is CallStatus.OK and not event.tool.exit_code
    ]
    faults: list[InjectedFault] = []
    for step in healthy:
        for kind in applicable_kinds(events, step):
            if kinds is not None and kind not in kinds:
                continue
            try:
                faults.append(inject(kind, events, step))
            except ValueError:
                continue
    return tuple(faults)
