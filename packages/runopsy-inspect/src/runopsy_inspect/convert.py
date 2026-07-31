"""Turning an Inspect AI eval log into a Runopsy trace.

Runopsy refuses to *import* OpenInference spans, on the grounds that reading somebody
else's attributes means guessing which of them mean what, and a wrong guess produces a
confident diagnosis of a trace we misunderstood. This module is the exception, and the
difference is worth being precise about rather than waving at.

OpenInference is a convention over free-form span attributes: two tools can both be
compliant and still disagree about where the exit code lives. An Inspect log is a typed
schema with its own reader — ``ToolEvent.function``, ``ToolEvent.arguments``,
``ToolEvent.error``, ``ModelEvent.output.usage`` — so the mapping below is reading
declared fields, not inferring meaning from names. Where Inspect does not state
something, this refuses to invent it: a tool call with no error is recorded as
successful, never as suspicious, because manufacturing a signal is the one failure mode
a diagnosis tool cannot come back from.

One sample becomes one run. Inspect evaluates the same task many times over, and a
single trace containing every epoch of every sample would let the propagation layer
reach from one attempt into another — which is exactly the "nothing may affect the past"
invariant, violated sideways.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from runopsy_adapter.recorder import PayloadStore
from runopsy_adapter.secrets import scan
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    Event,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    SecurityMetadata,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

if TYPE_CHECKING:  # pragma: no cover - import cost only matters at runtime
    from inspect_ai.log import EvalLog, EvalSample

RUNTIME = "inspect"


def _text(value: object) -> str | None:
    """A stable string for anything Inspect hands us, or None when there is nothing."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _preserve(vault: PayloadStore | None, *texts: str | None) -> None:
    """Keep payload text locally, redacted, so evidence and replay have something to read."""
    if vault is None:
        return
    for text in texts:
        if text:
            found = scan(text)
            vault.put(text, stored_text=found.redacted if found.found else None)


def _flagged(*texts: str | None) -> SecurityMetadata:
    found = [kind for text in texts if text for kind in scan(text).kinds]
    return SecurityMetadata(redacted=bool(found), contains_secret=bool(found))


def run_id_for(log: EvalLog, sample: EvalSample) -> str:
    """A run id that is stable across re-reads of the same log.

    Built from the eval id and the sample's own identity rather than from a clock, so
    importing the same log twice produces the same run and the collector's idempotent
    ingest recognises it instead of creating a duplicate.
    """
    parts = [
        str(getattr(log.eval, "eval_id", "") or log.eval.task),
        str(sample.id),
        str(sample.epoch),
    ]
    safe = "_".join(part for part in parts if part)
    return "".join(
        character if character.isalnum() or character in "-_." else "_" for character in safe
    )[:120]


def _outcome(sample: EvalSample) -> RunOutcome:
    """Whether the sample passed, from its scores.

    Inspect scores are task-defined and arbitrary; only the values it documents as
    meaning correct or incorrect are read. An unrecognised score is left as UNKNOWN
    rather than guessed at, because a sample wrongly recorded as failing would put a
    fabricated failure into the corpus this project measures itself against.
    """
    if sample.error is not None:
        return RunOutcome.FAILURE
    for score in (sample.scores or {}).values():
        value = getattr(score, "value", None)
        if value in ("C", 1, 1.0, True):
            return RunOutcome.SUCCESS
        if value in ("I", 0, 0.0, False):
            return RunOutcome.FAILURE
    return RunOutcome.UNKNOWN


def sample_to_events(
    log: EvalLog, sample: EvalSample, *, vault: PayloadStore | None = None
) -> list[Event]:
    """One Inspect sample as a Runopsy trace."""
    run_id = run_id_for(log, sample)
    started = sample.started_at or getattr(log.eval, "created", None) or datetime.now(UTC)
    if isinstance(started, str):
        started = datetime.fromisoformat(started)

    events: list[Event] = [
        RunStartEvent(
            event_id=f"{run_id}_evt_0000",
            run_id=run_id,
            sequence=0,
            timestamp=started,
            run=RunPayload(
                task=_text(sample.input) or log.eval.task,
                runtime=RUNTIME,
                model=getattr(log.eval, "model", None),
                provider="inspect",
            ),
        )
    ]

    sequence = 1
    for event in sample.events or ():
        mapped = _map_event(event, run_id=run_id, sequence=sequence, vault=vault)
        if mapped is not None:
            events.append(mapped)
            sequence += 1

    ended = sample.completed_at or started
    if isinstance(ended, str):
        ended = datetime.fromisoformat(ended)
    events.append(
        RunEndEvent(
            event_id=f"{run_id}_evt_{sequence:04d}",
            run_id=run_id,
            sequence=sequence,
            timestamp=ended,
            run=RunPayload(
                outcome=_outcome(sample),
                summary=_text(getattr(sample.error, "message", None)),
            ),
        )
    )
    return events


def _map_event(
    event: Any, *, run_id: str, sequence: int, vault: PayloadStore | None
) -> Event | None:
    """One Inspect event, or None for the kinds that are not steps of the run."""
    kind = getattr(event, "event", None)
    timestamp = getattr(event, "timestamp", None) or datetime.now(UTC)
    common = {
        "event_id": f"{run_id}_evt_{sequence:04d}",
        "run_id": run_id,
        "sequence": sequence,
        "timestamp": timestamp,
    }

    if kind == "tool":
        arguments = _text(event.arguments)
        result = _text(event.result)
        error = getattr(event, "error", None)
        # Inspect states the error explicitly. No error means the call succeeded; it is
        # never inferred from the look of the output.
        failed = error is not None
        _preserve(vault, arguments, result)
        return ToolCallEvent(
            **common,
            tool=ToolPayload(
                name=event.function or "tool",
                arguments_hash=hash_text(arguments) if arguments else None,
                output_hash=hash_text(result) if result else None,
                exit_code=1 if failed else 0,
                status=CallStatus.ERROR if failed else CallStatus.OK,
                error_type=getattr(error, "type", None) if failed else None,
            ),
            security=_flagged(arguments, result),
        )

    if kind == "model":
        output = getattr(event, "output", None)
        usage = getattr(output, "usage", None)
        error = getattr(event, "error", None)
        completion = _text(getattr(output, "completion", None))
        _preserve(vault, completion)
        return LlmCallEvent(
            **common,
            llm=LlmPayload(
                model=event.model or "unknown",
                provider="inspect",
                status=CallStatus.ERROR if error else CallStatus.OK,
                response_hash=hash_text(completion) if completion else None,
                tokens=TokenUsage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                ),
                cost_usd=float(getattr(usage, "total_cost", 0) or 0) or None,
            ),
            security=_flagged(completion),
        )

    # Everything else — spans, stores, state, logger noise — describes how Inspect ran
    # the sample rather than what the agent did, and inventing steps out of it would
    # inflate the very repetition the loop detectors look for.
    return None


def log_to_runs(log: EvalLog, *, vault: PayloadStore | None = None) -> dict[str, list[Event]]:
    """Every sample in an eval log, as one Runopsy trace each."""
    return {
        run_id_for(log, sample): sample_to_events(log, sample, vault=vault)
        for sample in (log.samples or ())
    }
