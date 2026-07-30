"""L1 behavioral detectors.

These look at patterns across steps rather than at a single reported status: repetition,
oscillating state, stale recall, incomplete handoff, runaway spend. Still no model call —
every judgement below is arithmetic over the recorded stream.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import ClassVar

from runopsy_core.detectors.base import AnalysisContext, signal_id
from runopsy_core.schema import (
    AnalysisLayer,
    ClaimEvent,
    FailureCategory,
    FailureSignal,
    HandoffEvent,
    LlmCallEvent,
    MemoryOperation,
    MemoryOpEvent,
    Severity,
    SupportStatus,
    ToolCallEvent,
)

LAYER = AnalysisLayer.L1_BEHAVIORAL


class RetryStormDetector:
    """A tool retried past the point of usefulness.

    Repetition is measured structurally through ``retry_of`` rather than by comparing
    outputs, so no content has to be read to see that an agent is stuck.
    """

    name = "behavioral:retry_storm"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        chains: dict[str, list[str]] = defaultdict(list)
        for event in context.of_kind(ToolCallEvent):
            if event.tool.retry_of:
                chains[event.tool.retry_of].append(event.event_id)

        threshold = context.settings.retry_threshold
        for origin, retries in chains.items():
            if len(retries) < threshold:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, origin),
                node_id=origin,
                category=FailureCategory.CONTROL_FLOW,
                severity=Severity.HIGH,
                layer=self.layer,
                detector=self.name,
                summary=f"call retried {len(retries)} times without resolving",
                evidence_node_ids=tuple(retries),
            )


class ToolLoopDetector:
    """The same call repeated with the same arguments.

    Identical arguments are the tell that distinguishes a loop from legitimate
    repetition: running the test suite four times while fixing it is progress, running
    it four times with an unchanged command and unchanged inputs is not.
    """

    name = "behavioral:tool_loop"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        occurrences: dict[tuple[str, str], list[str]] = defaultdict(list)
        for event in context.of_kind(ToolCallEvent):
            if event.tool.arguments_hash is None:
                continue
            occurrences[(event.tool.name, event.tool.arguments_hash)].append(event.event_id)

        threshold = context.settings.loop_threshold
        for (tool_name, _), node_ids in occurrences.items():
            if len(node_ids) < threshold:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, node_ids[0]),
                node_id=node_ids[0],
                category=FailureCategory.CONTROL_FLOW,
                severity=Severity.HIGH,
                layer=self.layer,
                detector=self.name,
                summary=(
                    f"tool {tool_name!r} called {len(node_ids)} times with identical arguments"
                ),
                evidence_node_ids=tuple(node_ids),
            )


class StateFlappingDetector:
    """A state key that keeps changing back to a value it already had.

    Oscillation means two parts of the run disagree about the same fact, and the step
    that first flipped it is a strong onset candidate — far stronger than the step where
    the contradiction finally caused a visible error.
    """

    name = "behavioral:state_flapping"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        history: dict[str, list[tuple[str, object]]] = defaultdict(list)
        for event in context.events:
            for key, change in event.state_delta.items():
                history[key].append((event.event_id, change.after))

        for key, entries in history.items():
            values = [value for _, value in entries]
            if len(values) < 3:
                continue
            distinct = Counter(repr(value) for value in values)
            revisits = sum(count - 1 for count in distinct.values() if count > 1)
            if revisits < 2 or len(distinct) < 2:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, entries[0][0], key),
                node_id=entries[0][0],
                category=FailureCategory.STATE,
                severity=Severity.MEDIUM,
                layer=self.layer,
                detector=self.name,
                summary=(
                    f"state key {key!r} changed {len(values)} times and returned to "
                    "earlier values; steps disagree about it"
                ),
                evidence_node_ids=tuple(node_id for node_id, _ in entries),
            )


class StaleMemoryDetector:
    """Recall of something written long enough ago to be out of date."""

    name = "behavioral:stale_memory"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        limit = context.settings.stale_memory_seconds
        for event in context.of_kind(MemoryOpEvent):
            if event.memory.operation is not MemoryOperation.READ:
                continue
            age = event.memory.age_seconds
            if age is None or age < limit:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.MEMORY,
                severity=Severity.LOW,
                layer=self.layer,
                detector=self.name,
                summary=(
                    f"memory key {event.memory.key!r} was read at an age of "
                    f"{age / 3600:.1f}h and may be out of date"
                ),
            )


class IncompleteHandoffDetector:
    """Context passed to a sub-agent with fields missing.

    Multi-agent failures are hard to reconstruct after the fact because the sub-agent's
    output looks plausible on its own; the omission is only visible at the boundary,
    which is exactly where this is recorded.
    """

    name = "behavioral:incomplete_handoff"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.of_kind(HandoffEvent):
            missing = event.handoff.missing_fields
            if not missing:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.HANDOFF,
                severity=Severity.HIGH,
                layer=self.layer,
                detector=self.name,
                summary=(f"handoff to {event.handoff.to_agent_id!r} omitted {', '.join(missing)}"),
            )


class UnsupportedClaimDetector:
    """Claims the runtime itself marked as unbacked or contradicted.

    This reads a status the adapter recorded; it does not judge the claim's content.
    Judging content is L3 semantic work and costs tokens, which is why it stays out of
    the always-on path.
    """

    name = "behavioral:unsupported_claim"
    layer = LAYER

    _SEVERITY: ClassVar[dict[SupportStatus, Severity]] = {
        SupportStatus.UNSUPPORTED: Severity.MEDIUM,
        SupportStatus.CONTRADICTED: Severity.HIGH,
    }

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        for event in context.of_kind(ClaimEvent):
            severity = self._SEVERITY.get(event.claim.support_status)
            if severity is None:
                continue
            yield FailureSignal(
                signal_id=signal_id(self.name, event.event_id),
                node_id=event.event_id,
                category=FailureCategory.REASONING,
                severity=severity,
                layer=self.layer,
                detector=self.name,
                summary=(
                    f"claim {event.claim.claim_id!r} is "
                    f"{event.claim.support_status.value} by the evidence on record"
                ),
            )


class BudgetDetector:
    """Spend past a configured ceiling.

    Off unless a ceiling is set. A budget that fires by default would be a guess about
    what a user considers expensive, and a wrong guess here is the fastest way to teach
    someone to ignore the tool.
    """

    name = "behavioral:budget"
    layer = LAYER

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        calls = tuple(context.of_kind(LlmCallEvent))
        if not calls:
            return
        anchor = calls[-1].event_id
        settings = context.settings

        tokens = sum(call.llm.tokens.total for call in calls)
        if settings.token_budget and tokens > settings.token_budget:
            yield FailureSignal(
                signal_id=signal_id(self.name, anchor, "tokens"),
                node_id=anchor,
                category=FailureCategory.BUDGET,
                severity=Severity.MEDIUM,
                layer=self.layer,
                detector=self.name,
                summary=f"run used {tokens} tokens against a budget of {settings.token_budget}",
            )

        cost = sum(call.llm.cost_usd or 0.0 for call in calls)
        if settings.cost_budget_usd and cost > settings.cost_budget_usd:
            yield FailureSignal(
                signal_id=signal_id(self.name, anchor, "cost"),
                node_id=anchor,
                category=FailureCategory.BUDGET,
                severity=Severity.MEDIUM,
                layer=self.layer,
                detector=self.name,
                summary=(
                    f"run cost ${cost:.4f} against a budget of ${settings.cost_budget_usd:.4f}"
                ),
            )
