"""The helper every runtime adapter builds its trace with.

An adapter's job is to know its runtime, not to know the trace format. Left to
hand-roll ids and sequence numbers, each adapter reinvents the same three bugs —
duplicate ids, gaps, events attributed to the wrong run — and the integrity checker then
reports them as corruption in the user's trace rather than as a defect in the adapter.

So the recorder owns identity and ordering, and adapters describe what happened.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

from runopsy_adapter.secrets import scan
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    CheckpointEvent,
    CheckpointPayload,
    Event,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    SecurityMetadata,
    StateChange,
    StatePayload,
    StateSnapshotEvent,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)


class EventSink(Protocol):
    """Where recorded events go. A collector is the usual one."""

    def record(self, event: Event) -> bool:
        """Persist one event, returning whether it was new."""
        ...


class PayloadStore(Protocol):
    """Where payload text goes so a replay can re-run it. A vault is the usual one."""

    def put(self, original_text: str, *, stored_text: str | None = None) -> str:
        """Store a payload, returning the digest of the original text."""
        ...


class ListSink:
    """An in-memory sink, for tests and for adapters that batch."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, event: Event) -> bool:
        self.events.append(event)
        return True

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunRecorder:
    """Builds a well-formed trace for one run.

    Sequence numbers are allocated here and nowhere else, so a trace produced through
    this class is contiguous by construction and any gap the integrity checker reports
    is a genuinely dropped write rather than an adapter counting badly.
    """

    def __init__(
        self,
        run_id: str,
        sink: EventSink,
        *,
        agent_id: str = "main",
        clock: Callable[[], datetime] = _utc_now,
        vault: PayloadStore | None = None,
    ) -> None:
        self.run_id = run_id
        self.sink = sink
        self.agent_id = agent_id
        self._clock = clock
        self._vault = vault
        self._sequence = 0
        self._started = False
        self._ended = False

    def _preserve(self, text: str | None) -> None:
        """Keep payload text in the local vault so a replay can re-run it.

        The redacted form is what gets stored: the vault lives on the user's machine,
        but a secret written anywhere is a secret that outlives the scan that found it.
        """
        if text is None or self._vault is None:
            return
        result = scan(text)
        self._vault.put(text, stored_text=result.redacted if result.found else None)

    @property
    def sequence(self) -> int:
        """The next sequence number this recorder will use."""
        return self._sequence

    def _next(self) -> tuple[int, str, datetime]:
        sequence = self._sequence
        self._sequence += 1
        return sequence, f"{self.run_id}_evt_{sequence:04d}", self._clock()

    def _emit(self, event: Event) -> str:
        self.sink.record(event)
        return event.event_id

    def start_run(
        self,
        *,
        task: str,
        runtime: str,
        repo: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        parent_run_id: str | None = None,
        intervention_kind: str | None = None,
        intervention_target: int | None = None,
    ) -> str:
        """Open the run. Must be called before anything else is recorded.

        A replay run must name its parent and what it varied; the comparison that can
        upgrade a suspicion to a supported cause keys off exactly these fields.
        """
        if self._started:
            msg = f"run {self.run_id} was already started"
            raise RuntimeError(msg)
        self._started = True
        sequence, event_id, moment = self._next()
        return self._emit(
            RunStartEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                sequence=sequence,
                timestamp=moment,
                run=RunPayload(
                    task=task,
                    repo=repo,
                    runtime=runtime,
                    provider=provider,
                    model=model,
                    parent_run_id=parent_run_id,
                    intervention_kind=intervention_kind,
                    intervention_target=intervention_target,
                ),
            )
        )

    def tool_call(
        self,
        name: str,
        *,
        arguments: str | None = None,
        output: str | None = None,
        exit_code: int | None = None,
        duration_ms: int = 0,
        status: CallStatus | None = None,
        retry_of: str | None = None,
        state: dict[str, object] | None = None,
        state_delta: dict[str, StateChange] | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Record an external action.

        ``arguments`` and ``output`` are scanned and hashed here rather than stored. The
        hash is what later tells one call from another; the text itself never enters the
        trace, so a command line carrying a token cannot leak through the journal.
        """
        sequence, event_id, moment = self._next()
        found: list[str] = []
        for text in (arguments, output):
            if text is not None:
                found.extend(scan(text).kinds)
                self._preserve(text)

        resolved = status or (CallStatus.ERROR if exit_code not in (None, 0) else CallStatus.OK)
        return self._emit(
            ToolCallEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                parent_id=parent_id,
                sequence=sequence,
                timestamp=moment,
                tool=ToolPayload(
                    name=name,
                    arguments_hash=hash_text(arguments) if arguments is not None else None,
                    output_hash=hash_text(output) if output is not None else None,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    status=resolved,
                    retry_of=retry_of,
                ),
                state_delta={
                    **{key: StateChange(after=value) for key, value in (state or {}).items()},
                    # Given whole, so a caller that knows the previous value can say so.
                    # Evidence reads better as "staging -> production" than as "production".
                    **(state_delta or {}),
                },
                security=SecurityMetadata(redacted=bool(found), contains_secret=bool(found)),
            )
        )

    def state_snapshot(self, values: dict[str, object]) -> str:
        """Record the observed state of the world at this point in the run.

        Separate from the ``state_delta`` carried on a step, and deliberately so. A delta
        is a claim that something changed and is read by the flapping detector; a
        snapshot is a description, read by nothing automatically. That is what makes it
        the safe home for values which legitimately repeat — the set of modified files in
        an edit-test-revert cycle returns to itself constantly, and as a delta it would
        manufacture a finding on a perfectly healthy run.
        """
        sequence, event_id, moment = self._next()
        return self._emit(
            StateSnapshotEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                sequence=sequence,
                timestamp=moment,
                state=StatePayload(values=values),
            )
        )

    def llm_call(
        self,
        model: str,
        *,
        prompt: str | None = None,
        response: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        status: CallStatus = CallStatus.OK,
        finish_reason: str | None = None,
        cost_usd: float | None = None,
        provider: str | None = None,
    ) -> str:
        """Record a model call, storing prompt and response by hash only."""
        sequence, event_id, moment = self._next()
        found: list[str] = []
        for text in (prompt, response):
            if text is not None:
                found.extend(scan(text).kinds)

        return self._emit(
            LlmCallEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                sequence=sequence,
                timestamp=moment,
                llm=LlmPayload(
                    model=model,
                    provider=provider,
                    prompt_hash=hash_text(prompt) if prompt is not None else None,
                    response_hash=hash_text(response) if response is not None else None,
                    tokens=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
                    latency_ms=latency_ms,
                    status=status,
                    finish_reason=finish_reason,
                    cost_usd=cost_usd,
                ),
                security=SecurityMetadata(redacted=bool(found), contains_secret=bool(found)),
            )
        )

    def checkpoint(self, checkpoint_id: str, *, repo_state: str | None = None) -> str:
        """Record a point the run could be returned to."""
        sequence, event_id, moment = self._next()
        return self._emit(
            CheckpointEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                sequence=sequence,
                timestamp=moment,
                checkpoint=CheckpointPayload(checkpoint_id=checkpoint_id, repo_state=repo_state),
            )
        )

    def end_run(self, outcome: RunOutcome, *, summary: str | None = None) -> str:
        """Close the run. Recording after this is a bug in the adapter."""
        if self._ended:
            msg = f"run {self.run_id} was already ended"
            raise RuntimeError(msg)
        self._ended = True
        sequence, event_id, moment = self._next()
        return self._emit(
            RunEndEvent(
                event_id=event_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                sequence=sequence,
                timestamp=moment,
                run=RunPayload(outcome=outcome, summary=summary),
            )
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close an open run even when the adapter crashed.

        An unfinished trace is still diagnosable, but only if it says so. Without this,
        a crashed adapter leaves a run that looks merely incomplete, and the engine
        cannot tell that apart from a process that was killed.
        """
        if self._started and not self._ended:
            outcome = RunOutcome.UNKNOWN if exc is None else RunOutcome.FAILURE
            summary = None if exc is None else f"adapter raised {type(exc).__name__}"
            self.end_run(outcome, summary=summary)
