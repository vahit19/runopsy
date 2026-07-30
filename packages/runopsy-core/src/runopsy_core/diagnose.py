"""The diagnosis entry point.

Composes the deterministic pipeline end to end: normalize, detect, rank, and assemble
the bundle that ``runopsy diagnose`` prints. Nothing here calls a model or reads a clock,
so a diagnosis over an unchanged trace is byte-identical every time — which is what
makes caching by trace fingerprint sound, and what lets two people compare answers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from runopsy_core.detectors import DetectorRegistry, default_registry
from runopsy_core.detectors.base import AnalysisContext
from runopsy_core.hashing import hash_text
from runopsy_core.ranking import RankingWeights, observed_failure, rank_candidates
from runopsy_core.schema import DiagnosisBundle

FINGERPRINT_LENGTH = 12


def trace_fingerprint(context: AnalysisContext) -> str:
    """A stable short digest of the analysed trace.

    Derived from event identity and order rather than from content, so it changes when
    the run changes but not when an unrelated field is added to the schema.
    """
    material = "\n".join(
        f"{event.sequence}:{event.event_id}:{event.kind.value}" for event in context.events
    )
    return hash_text(material).removeprefix("sha256:")[:FINGERPRINT_LENGTH]


def diagnose(
    context: AnalysisContext,
    *,
    registry: DetectorRegistry | None = None,
    weights: RankingWeights | None = None,
    created_at: datetime | None = None,
) -> DiagnosisBundle:
    """Run the deterministic pipeline and assemble a diagnosis.

    ``created_at`` is a parameter rather than a call to the clock. The core stays a pure
    function of its input; the CLI supplies wall time when it wants it, and tests and
    the cache get reproducibility for free. When omitted it falls back to the last event
    in the trace, which is also when the analysis subject actually ended.
    """
    registry = registry or default_registry()
    signals = registry.run(context)
    candidates = rank_candidates(context, signals, weights=weights)
    symptom = observed_failure(context, signals)

    timestamp = created_at or (
        context.events[-1].timestamp if context.events else datetime(1970, 1, 1, tzinfo=UTC)
    )

    return DiagnosisBundle(
        diagnosis_id=f"diag:{context.run_id}:{trace_fingerprint(context)}",
        run_id=context.run_id,
        created_at=timestamp,
        observed_failure_node_id=symptom.node_id if symptom else None,
        observed_failure_summary=symptom.summary if symptom else "",
        candidates=candidates,
        tokens_spent=0,
        cost_usd=0.0,
    )
