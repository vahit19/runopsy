"""L0 structural detectors.

These read what the runtime already reported — exit codes, statuses, missing events —
and turn it into signals. Nothing here interprets meaning, so nothing here can be wrong
about meaning: an exit code of 1 is a fact, and the only judgement is that it is worth
surfacing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from runopsy_core.detectors.base import AnalysisContext, signal_id
from runopsy_core.normalize import node_id_for
from runopsy_core.schema import (
    AnalysisLayer,
    CallStatus,
    FailureCategory,
    FailureSignal,
    LlmCallEvent,
    RunEndEvent,
    RunOutcome,
    RunStartEvent,
    Severity,
    ToolCallEvent,
)

LAYER = AnalysisLayer.L0_STRUCTURAL


class ToolExecutionDetector:
    """Tool calls that failed outright."""

    name = "structural:tool_execution"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.of_kind(ToolCallEvent):
            failed_status = event.tool.status is CallStatus.ERROR
            failed_code = event.tool.exit_code is not None and event.tool.exit_code != 0
            if not (failed_status or failed_code):
                continue
            detail = (
                f"exit code {event.tool.exit_code}"
                if failed_code
                else f"status {event.tool.status.value}"
            )
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.TOOL_EXECUTION,
                severity=Severity.HIGH,
                layer=self.layer,
                detector=self.name,
                summary=f"tool {event.tool.name!r} failed with {detail}",
            )


class TimeoutDetector:
    """Tool or model calls that ran out of time.

    Separated from plain errors because a timeout says nothing about correctness — the
    work may have been fine and simply slow — and conflating the two sends people
    debugging logic when the real problem is a limit.
    """

    name = "structural:timeout"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.events:
            match event:
                case ToolCallEvent() if event.tool.status is CallStatus.TIMEOUT:
                    target, label = event.tool.name, "tool"
                case LlmCallEvent() if event.llm.status is CallStatus.TIMEOUT:
                    target, label = event.llm.model, "model"
                case _:
                    continue
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.TOOL_EXECUTION,
                severity=Severity.MEDIUM,
                layer=self.layer,
                detector=self.name,
                summary=f"{label} {target!r} timed out",
            )


class ModelCallDetector:
    """Model calls the provider rejected or truncated.

    A truncated response is reported because a plan cut off mid-sentence is a common and
    easily missed cause of later confusion: the agent proceeds on half an instruction.
    """

    name = "structural:model_call"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.of_kind(LlmCallEvent):
            if event.llm.status is CallStatus.ERROR:
                yield FailureSignal(
                    signal_id=signal_id(self.name, event.event_id),
                    node_id=event.event_id,
                    category=FailureCategory.PLANNING,
                    severity=Severity.HIGH,
                    layer=self.layer,
                    detector=self.name,
                    summary=f"model {event.llm.model!r} call failed",
                )
            elif event.llm.finish_reason == "length":
                yield FailureSignal(
                    signal_id=signal_id(self.name, event.event_id, "truncated"),
                    node_id=event.event_id,
                    category=FailureCategory.PLANNING,
                    severity=Severity.MEDIUM,
                    layer=self.layer,
                    detector=self.name,
                    summary=f"model {event.llm.model!r} response was truncated by length",
                )


class BlockedActionDetector:
    """Actions a policy gate refused.

    Reported as ``SAFETY`` rather than as an error: the block is the system working, but
    it still explains why the run could not finish, and hiding it leaves the user
    staring at a task that stopped for no visible reason.
    """

    name = "structural:blocked_action"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.of_kind(ToolCallEvent):
            if event.tool.status is not CallStatus.BLOCKED:
                continue
            reason = event.tool.blocked_reason or "policy"
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.SAFETY,
                severity=Severity.MEDIUM,
                layer=self.layer,
                detector=self.name,
                summary=f"tool {event.tool.name!r} was blocked: {reason}",
            )


class TraceIntegrityDetector:
    """Gaps, duplicates and reordering in the recorded stream.

    This is a signal about the *evidence*, not about the run. It is reported at high
    severity because every other detector's conclusion is conditional on the trace being
    complete, and a diagnosis drawn over a hole can be confidently wrong.
    """

    name = "structural:trace_integrity"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        report = context.integrity
        if report.is_intact:
            return
        anchor = context.events[0].event_id if context.events else context.run_id
        yield FailureSignal(
            signal_id=signal_id(self.name, context.run_id),
            node_id=anchor,
            category=FailureCategory.VALIDATION,
            severity=Severity.HIGH,
            layer=self.layer,
            detector=self.name,
            summary=(
                f"trace is not intact ({report.describe()}); "
                "conclusions drawn from it are unreliable"
            ),
        )


class IncompleteRunDetector:
    """Runs that never reported an ending.

    Deliberately not reported as a failure. The process may have been killed, the
    machine may have slept; calling that a failed task would be a conclusion the trace
    does not support.
    """

    name = "structural:incomplete_run"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        if not context.events:
            return
        if any(isinstance(event, RunEndEvent) for event in context.events):
            return
        yield FailureSignal(
            signal_id=signal_id(self.name, context.run_id),
            node_id=context.events[-1].event_id,
            category=FailureCategory.OUTCOME,
            severity=Severity.MEDIUM,
            layer=self.layer,
            detector=self.name,
            summary="run ended without a recorded outcome; it may have been interrupted",
        )


class OutcomeMismatchDetector:
    """Runs reported as successful while a step plainly failed.

    This is the "the agent said it was done" failure, and it is the most expensive one
    to catch by hand, because nothing in the final message invites suspicion.
    """

    name = "structural:outcome_mismatch"
    layer = LAYER

    def _failures(self, context: AnalysisContext) -> Iterator[str]:
        for event in context.of_kind(ToolCallEvent):
            if event.tool.status is CallStatus.ERROR or (
                event.tool.exit_code is not None and event.tool.exit_code != 0
            ):
                yield event.event_id

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        ended = next((event for event in context.events if isinstance(event, RunEndEvent)), None)
        if ended is None or ended.run.outcome is not RunOutcome.SUCCESS:
            return
        failures = tuple(self._failures(context))
        if not failures:
            return
        yield FailureSignal(
            signal_id=signal_id(self.name, node_id_for(ended)),
            node_id=node_id_for(ended),
            category=FailureCategory.OUTCOME,
            severity=Severity.CRITICAL,
            layer=self.layer,
            detector=self.name,
            summary=(f"run reported success while {len(failures)} tool call(s) failed"),
            evidence_node_ids=failures,
        )


class MissingRunStartDetector:
    """Streams that begin mid-run.

    Without a start event there is no task, repo or model on record, so any diagnosis is
    missing the context that decides whether a step was even wrong.
    """

    name = "structural:missing_run_start"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        if not context.events:
            return
        if any(isinstance(event, RunStartEvent) for event in context.events):
            return
        yield FailureSignal(
            signal_id=signal_id(self.name, context.run_id),
            node_id=context.events[0].event_id,
            category=FailureCategory.GOAL_INPUT,
            severity=Severity.LOW,
            layer=self.layer,
            detector=self.name,
            summary="no run_start recorded; task and runtime context are unknown",
        )
