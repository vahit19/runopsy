"""Causal ranking: turning detector signals into ordered onset candidates.

The composite score follows section 8.2 of the design document. Two properties matter
more than the arithmetic:

**Confidence is capped.** The weights here are expert guesses, not calibrated
probabilities — nothing has been fitted to labelled data yet. Reporting a raw score as
if it were a probability is precisely the false-confidence failure this product exists
to prevent, so confidence is scaled into a band that stays visibly short of certainty.
Only a counterfactual replay or a human verdict can lift a candidate out of that band,
and the schema enforces that separately.

**A step cannot cause something that already happened.** Candidacy is restricted to
steps at or before the observed failure. This is a hard filter rather than a penalty:
retry loops and repair attempts cluster densely just after a failure, and without it the
ranking would keep nominating the cleanup as the cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from runopsy_core.detectors.base import AnalysisContext
from runopsy_core.impact import affected_nodes
from runopsy_core.schema import (
    DiagnosisCandidate,
    DiagnosisStatus,
    FailureCategory,
    FailureSignal,
    NodeKind,
    Severity,
)

MAX_UNVALIDATED_CONFIDENCE: Final = 0.75
"""Ceiling on confidence for any candidate that no replay or human has confirmed."""

IMPACT_SATURATION: Final = 5
"""Downstream count at which the impact component is considered maximal."""

_SEVERITY_RANK: Final = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

_SYMPTOM_CATEGORIES: Final = frozenset(
    {FailureCategory.TOOL_EXECUTION, FailureCategory.OUTCOME, FailureCategory.VALIDATION}
)
"""Categories that describe something the user actually saw go wrong."""

_EVIDENCE_ONLY_DETECTORS: Final = frozenset(
    {"structural:trace_integrity", "structural:missing_run_start"}
)
"""Detectors that judge the recording rather than the run.

"Events are missing" and "the task was never recorded" both say the evidence is
incomplete. Neither says a step misbehaved, so neither may be nominated as a cause —
they lower confidence in every candidate instead.
"""

_NON_STEP_KINDS: Final = frozenset({NodeKind.RUN, NodeKind.AGENT})
"""Container nodes that cannot themselves be an onset.

A run or an agent is not something that happened at a point in time; it is the thing
inside which steps happened. Left eligible, the run node sits at sequence zero and wins
the precedence term outright, so every diagnosis would nominate "the run" as the
earliest suspect and say nothing useful.
"""


@dataclass(frozen=True)
class RankingWeights:
    """Relative contribution of each scoring component. Should sum to 1."""

    severity: float = 0.35
    precedence: float = 0.25
    state_anomaly: float = 0.15
    downstream_impact: float = 0.20
    evaluator_support: float = 0.05

    def total(self) -> float:
        return (
            self.severity
            + self.precedence
            + self.state_anomaly
            + self.downstream_impact
            + self.evaluator_support
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    """Why a candidate scored what it did.

    Travels onto the candidate so a user who disagrees with the ordering can see which
    term drove it rather than being asked to trust a single number.
    """

    severity: float
    precedence: float
    state_anomaly: float
    downstream_impact: float
    evaluator_support: float
    uncertainty_penalty: float

    def weighted_total(self, weights: RankingWeights) -> float:
        """The composite score before the uncertainty penalty is applied."""
        return (
            weights.severity * self.severity
            + weights.precedence * self.precedence
            + weights.state_anomaly * self.state_anomaly
            + weights.downstream_impact * self.downstream_impact
            + weights.evaluator_support * self.evaluator_support
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "severity": round(self.severity, 4),
            "precedence": round(self.precedence, 4),
            "state_anomaly": round(self.state_anomaly, 4),
            "downstream_impact": round(self.downstream_impact, 4),
            "evaluator_support": round(self.evaluator_support, 4),
            "uncertainty_penalty": round(self.uncertainty_penalty, 4),
        }


def _severity_component(signals: tuple[FailureSignal, ...]) -> float:
    ranks = [_SEVERITY_RANK[signal.severity] for signal in signals]
    return max(ranks, default=0) / 4


def _precedence_component(position: int, observed_position: int, earliest: int) -> float:
    """How much earlier than the symptom this step ran, normalized to the trace window.

    Earlier is stronger, because the value of the product is finding where a run started
    going wrong rather than where it finally stopped.
    """
    span = observed_position - earliest
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (observed_position - position) / span))


def _state_component(signals: tuple[FailureSignal, ...], has_state_change: bool) -> float:
    if any(signal.category is FailureCategory.STATE for signal in signals):
        return 1.0
    return 0.5 if has_state_change else 0.0


def observed_failure(
    context: AnalysisContext, signals: tuple[FailureSignal, ...]
) -> FailureSignal | None:
    """The symptom: the latest, most severe thing the run visibly got wrong.

    Reported separately from every candidate because it is a fact rather than a
    hypothesis, and conflating the two is the reading error that sends people to fix the
    wrong step.
    """
    positions = {node.node_id: node.sequence for node in context.graph.nodes}
    symptoms = [
        signal
        for signal in signals
        if signal.category in _SYMPTOM_CATEGORIES
        and signal.detector not in _EVIDENCE_ONLY_DETECTORS
        and _SEVERITY_RANK[signal.severity] >= _SEVERITY_RANK[Severity.HIGH]
    ]
    if not symptoms:
        return None
    return max(
        symptoms,
        key=lambda signal: (
            positions.get(signal.node_id, 0),
            _SEVERITY_RANK[signal.severity],
            signal.signal_id,
        ),
    )


def rank_candidates(
    context: AnalysisContext,
    signals: tuple[FailureSignal, ...],
    *,
    weights: RankingWeights | None = None,
) -> tuple[DiagnosisCandidate, ...]:
    """Score every step that carries a signal and could plausibly be the onset."""
    weights = weights or RankingWeights()
    if not signals:
        return ()

    positions = {node.node_id: node.sequence for node in context.graph.nodes}
    state_changed = {event.event_id for event in context.events if event.state_delta}

    symptom = observed_failure(context, signals)
    observed_position = (
        positions.get(symptom.node_id, 0) if symptom else max(positions.values(), default=0)
    )
    earliest = min(positions.values(), default=0)
    penalty = 0.0 if context.integrity.is_intact else 0.5

    kinds = {node.node_id: node.kind for node in context.graph.nodes}

    by_node: dict[str, list[FailureSignal]] = {}
    for signal in signals:
        if signal.detector in _EVIDENCE_ONLY_DETECTORS:
            continue
        if kinds.get(signal.node_id) in _NON_STEP_KINDS:
            continue
        if positions.get(signal.node_id, 0) > observed_position:
            continue
        by_node.setdefault(signal.node_id, []).append(signal)

    candidates = [
        _build_candidate(
            context=context,
            node_id=node_id,
            node_signals=tuple(node_signals),
            position=positions.get(node_id, 0),
            observed_position=observed_position,
            earliest=earliest,
            has_state_change=node_id in state_changed,
            penalty=penalty,
            weights=weights,
            symptom=symptom,
        )
        for node_id, node_signals in by_node.items()
    ]
    return tuple(sorted(candidates, key=lambda c: (-c.score, positions.get(c.onset_node_id, 0))))


def _build_candidate(
    *,
    context: AnalysisContext,
    node_id: str,
    node_signals: tuple[FailureSignal, ...],
    position: int,
    observed_position: int,
    earliest: int,
    has_state_change: bool,
    penalty: float,
    weights: RankingWeights,
    symptom: FailureSignal | None,
) -> DiagnosisCandidate:
    affected = affected_nodes(context.graph, node_id)
    breakdown = ScoreBreakdown(
        severity=_severity_component(node_signals),
        precedence=_precedence_component(position, observed_position, earliest),
        state_anomaly=_state_component(node_signals, has_state_change),
        downstream_impact=min(len(affected) / IMPACT_SATURATION, 1.0),
        evaluator_support=0.0,
        uncertainty_penalty=penalty,
    )
    score = round(breakdown.weighted_total(weights) * (1.0 - penalty), 4)

    strongest = max(node_signals, key=lambda signal: _SEVERITY_RANK[signal.severity])
    is_symptom = symptom is not None and symptom.node_id == node_id

    return DiagnosisCandidate(
        candidate_id=f"cand:{node_id}",
        onset_node_id=node_id,
        category=strongest.category,
        status=DiagnosisStatus.OBSERVED_FAILURE if is_symptom else _hypothesis_status(node_signals),
        confidence=round(min(score, 1.0) * MAX_UNVALIDATED_CONFIDENCE, 3),
        score=score,
        summary=strongest.summary,
        signal_ids=tuple(signal.signal_id for signal in node_signals),
        evidence_node_ids=tuple(
            dict.fromkeys(node for signal in node_signals for node in signal.evidence_node_ids)
        ),
        affected_node_ids=affected,
        score_breakdown=breakdown.as_dict(),
    )


def _hypothesis_status(node_signals: tuple[FailureSignal, ...]) -> DiagnosisStatus:
    """Distinguish a step that misbehaved from one that merely sits nearby.

    A node carrying its own failure signal is a suspected onset. A node that only shows
    up because something correlates with it is exactly that — correlated — and saying so
    is the difference between a lead and a claim.
    """
    if any(
        _SEVERITY_RANK[signal.severity] >= _SEVERITY_RANK[Severity.MEDIUM]
        for signal in node_signals
    ):
        return DiagnosisStatus.SUSPECTED_ONSET
    return DiagnosisStatus.CORRELATED_CAUSE
