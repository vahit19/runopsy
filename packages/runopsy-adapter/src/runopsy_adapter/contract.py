"""The contract every runtime adapter must satisfy.

Shipped as a reusable check rather than kept in this repository's tests, because the
adapters that matter most — Hermes first, others later — will live in their own
repositories and against their own runtime versions. An adapter that passes this
produces traces the engine can analyse; one that does not will produce diagnoses that
are quietly wrong, since every conclusion downstream assumes a well-formed trace.

Design document 6.2 calls for contract tests against runtime API changes. This is that.
"""

from __future__ import annotations

from collections.abc import Sequence

from runopsy_core import check_integrity
from runopsy_core.schema import Event, RunEndEvent, RunStartEvent


class ContractViolationError(AssertionError):
    """Raised when a trace would mislead the engine."""


def _fail(message: str) -> None:
    raise ContractViolationError(message)


def assert_adapter_contract(events: Sequence[Event], *, run_id: str | None = None) -> None:
    """Check that a trace an adapter produced is well formed.

    Each rule exists because breaking it produces a confident wrong answer rather than
    an obvious error, which is the failure mode this project is least willing to accept.
    """
    if not events:
        _fail("adapter produced no events")

    resolved = run_id or events[0].run_id

    foreign = {event.run_id for event in events} - {resolved}
    if foreign:
        _fail(
            f"trace mixes runs {sorted(foreign)} into {resolved}; one run's step would "
            "be blamed for another run's failure"
        )

    identifiers = [event.event_id for event in events]
    if len(identifiers) != len(set(identifiers)):
        _fail(
            "duplicate event ids: ingest deduplicates on them, so a real step would be "
            "silently dropped"
        )

    report = check_integrity(resolved, events)
    if report.duplicate_sequences:
        _fail(f"duplicate sequence numbers {report.duplicate_sequences}: step order is ambiguous")
    if report.missing_sequences:
        _fail(
            f"missing sequence numbers {report.missing_sequences}: a diagnosis drawn over "
            "a gap can be confidently wrong about where the run broke"
        )
    if report.out_of_order:
        _fail("events are not in sequence order")

    starts = [event for event in events if isinstance(event, RunStartEvent)]
    if len(starts) != 1:
        _fail(f"expected exactly one run_start, found {len(starts)}")
    if not isinstance(events[0], RunStartEvent):
        _fail("run_start must be the first event, or the task and runtime are unknown")

    ends = [event for event in events if isinstance(event, RunEndEvent)]
    if len(ends) > 1:
        _fail(f"expected at most one run_end, found {len(ends)}")
    if ends and not isinstance(events[-1], RunEndEvent):
        _fail("events were recorded after run_end")

    for event in events:
        if event.timestamp.tzinfo is None or event.timestamp.utcoffset() is None:
            _fail(
                f"event {event.event_id} has a naive timestamp; traces compared across "
                "machines would be misordered"
            )


def warn_about_state_keys(events: Sequence[Event]) -> tuple[str, ...]:
    """Report state keys that look like per-step readouts rather than beliefs.

    ``state_delta`` is for facts the run believes about the world, so that two steps
    disagreeing about one becomes a signal. A key that changes at nearly every step is
    not a belief — it is a restatement of what just happened — and recording it makes
    every run look like a state conflict.

    Returned as warnings rather than raised: this is a judgement about meaning, and the
    adapter author is better placed to make it than a heuristic is.
    """
    steps = [event for event in events if event.state_delta]
    if len(steps) < 4:
        return ()

    counts: dict[str, int] = {}
    for event in steps:
        for key in event.state_delta:
            counts[key] = counts.get(key, 0) + 1

    return tuple(
        f"state key {key!r} changes on {count} of {len(steps)} steps, which reads as a "
        "per-step readout rather than a belief about the world"
        for key, count in sorted(counts.items())
        if count == len(steps)
    )


def describe_contract() -> tuple[str, ...]:
    """The rules, for adapter documentation."""
    return (
        "exactly one run_start, and it comes first",
        "at most one run_end, and nothing is recorded after it",
        "event ids are unique within the run",
        "sequence numbers are contiguous and ascending",
        "every event belongs to the run being recorded",
        "every timestamp is timezone-aware",
        "state_delta records beliefs about the world, not per-step readouts",
    )
