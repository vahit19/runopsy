"""Strategies to score the engine against.

The comparison that matters is not against nothing — it is against what a person
already does for free. Opening a log and blaming the last thing that failed takes
seconds and costs nothing, so an engine that does not clearly beat it has no reason to
exist, however sophisticated its internals.

Every baseline here is deterministic and token-free, so the whole comparison runs
offline and reproduces exactly. Model-based baselines from section 17.3 arrive with the
semantic layer.
"""

from __future__ import annotations

from typing import Protocol

from runopsy_core import AnalysisContext, diagnose
from runopsy_core.detectors import default_registry
from runopsy_core.schema import CallStatus, LlmCallEvent, ToolCallEvent


class Strategy(Protocol):
    """Something that nominates onset steps, best first."""

    name: str
    description: str

    def predict(self, context: AnalysisContext) -> tuple[int, ...]:
        """Ranked candidate steps for this run."""
        ...


def _failed(event: object) -> bool:
    if isinstance(event, ToolCallEvent):
        return event.tool.status is CallStatus.ERROR or bool(event.tool.exit_code)
    if isinstance(event, LlmCallEvent):
        return event.llm.status is CallStatus.ERROR
    return False


class NoDiagnosis:
    """The floor: raw trace, no analysis.

    Scores zero by construction. It is here to make the other columns legible — a
    percentage means nothing without knowing what doing nothing would have earned.
    """

    name = "no_diagnosis"
    description = "read the trace, nominate nothing"

    def predict(self, context: AnalysisContext) -> tuple[int, ...]:
        return ()


class LastFailure:
    """What a person does: scroll to the bottom and blame the last thing that broke.

    This is the baseline the product must beat. It is free, instant, and often right —
    and when it is wrong it is wrong in exactly the expensive way, because the last
    failure is usually a symptom of something that went wrong much earlier.
    """

    name = "last_failure"
    description = "blame the last failing step, as a human reading a log would"

    def predict(self, context: AnalysisContext) -> tuple[int, ...]:
        failures = [event.sequence for event in context.events if _failed(event)]
        return tuple(reversed(failures))


class FirstFailure:
    """The naive opposite: blame the earliest failing step.

    Included because "earlier is better" is the intuition behind onset localization, and
    a reader deserves to know how much of the engine's score comes from that intuition
    alone rather than from the ranking built on top of it.
    """

    name = "first_failure"
    description = "blame the earliest failing step"

    def predict(self, context: AnalysisContext) -> tuple[int, ...]:
        return tuple(event.sequence for event in context.events if _failed(event))


class RuleOnly:
    """The deterministic engine: detectors, graph impact and composite ranking."""

    name = "rule_only"
    description = "Runopsy deterministic engine, no model calls"

    def predict(self, context: AnalysisContext) -> tuple[int, ...]:
        bundle = diagnose(context, registry=default_registry())
        positions = {node.node_id: node.sequence for node in context.graph.nodes}
        return tuple(
            positions[candidate.onset_node_id]
            for candidate in bundle.candidates
            if candidate.onset_node_id in positions
        )


def all_strategies() -> tuple[Strategy, ...]:
    """Every baseline, weakest first, with the engine last."""
    return (NoDiagnosis(), LastFailure(), FirstFailure(), RuleOnly())


DEFAULT_STRATEGY: Strategy = RuleOnly()
