"""Scoring a diagnosis against known ground truth.

The metrics follow section 17.2 of the design document: onset top-1 accuracy, top-3
recall, and mean absolute step distance. Two additions make the headline number
trustworthy rather than flattering.

A **false positive rate** over healthy runs is reported beside accuracy. An engine that
nominates something on every trace would otherwise score perfectly on localization while
being unusable, and spurious findings are what actually get a diagnosis tool turned off.

**Blind spots are counted, not dropped.** Cases whose onset leaves no structural trace
are excluded from accuracy — scoring them would only measure luck — but they appear in
the report as coverage the deterministic layer does not have.
"""

from __future__ import annotations

from dataclasses import dataclass

from runopsy_bench.cases import SyntheticCase, all_cases
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.detectors import DetectorRegistry

TOP_K = 3


@dataclass(frozen=True)
class CaseResult:
    """What the engine said about one labelled case."""

    case: SyntheticCase
    predicted_steps: tuple[int, ...]
    """Candidate onset steps, best first."""

    @property
    def predicted(self) -> int | None:
        return self.predicted_steps[0] if self.predicted_steps else None

    @property
    def is_exact(self) -> bool:
        return self.predicted is not None and self.predicted == self.case.onset_step

    @property
    def in_top_k(self) -> bool:
        return self.case.onset_step in self.predicted_steps[:TOP_K]

    @property
    def step_distance(self) -> int | None:
        if self.predicted is None or self.case.onset_step is None:
            return None
        return abs(self.predicted - self.case.onset_step)

    @property
    def is_false_positive(self) -> bool:
        return self.case.is_healthy and bool(self.predicted_steps)


@dataclass(frozen=True)
class BenchmarkReport:
    """Aggregate scores over the suite."""

    results: tuple[CaseResult, ...]

    @property
    def scored(self) -> tuple[CaseResult, ...]:
        """Cases that count toward accuracy: failures the trace can actually reveal."""
        return tuple(
            result
            for result in self.results
            if not result.case.is_healthy and result.case.deterministically_detectable
        )

    @property
    def blind_spots(self) -> tuple[CaseResult, ...]:
        return tuple(
            result for result in self.results if not result.case.deterministically_detectable
        )

    @property
    def healthy(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if result.case.is_healthy)

    @property
    def top1_accuracy(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return sum(result.is_exact for result in scored) / len(scored)

    @property
    def top3_recall(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return sum(result.in_top_k for result in scored) / len(scored)

    @property
    def mean_step_distance(self) -> float:
        distances = [
            result.step_distance for result in self.scored if result.step_distance is not None
        ]
        if not distances:
            return 0.0
        return sum(distances) / len(distances)

    @property
    def localized(self) -> float:
        """Share of scored cases where the engine nominated anything at all."""
        scored = self.scored
        if not scored:
            return 0.0
        return sum(bool(result.predicted_steps) for result in scored) / len(scored)

    @property
    def false_positive_rate(self) -> float:
        healthy = self.healthy
        if not healthy:
            return 0.0
        return sum(result.is_false_positive for result in healthy) / len(healthy)

    def failures(self) -> tuple[CaseResult, ...]:
        """Scored cases the engine got wrong, for inspection."""
        return tuple(result for result in self.scored if not result.is_exact)


def evaluate_case(case: SyntheticCase, *, registry: DetectorRegistry | None = None) -> CaseResult:
    """Run the deterministic pipeline over one case and record what it predicted."""
    context = AnalysisContext.from_events(case.events[0].run_id, case.events)
    bundle = diagnose(context, registry=registry)
    positions = {node.node_id: node.sequence for node in context.graph.nodes}
    predicted = tuple(
        positions[candidate.onset_node_id]
        for candidate in bundle.candidates
        if candidate.onset_node_id in positions
    )
    return CaseResult(case=case, predicted_steps=predicted)


def run_benchmark(
    cases: tuple[SyntheticCase, ...] | None = None,
    *,
    registry: DetectorRegistry | None = None,
) -> BenchmarkReport:
    """Score the whole suite."""
    selected = cases if cases is not None else all_cases()
    return BenchmarkReport(
        results=tuple(evaluate_case(case, registry=registry) for case in selected)
    )
