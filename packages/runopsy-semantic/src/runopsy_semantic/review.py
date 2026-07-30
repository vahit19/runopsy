"""Orchestrating a hybrid diagnosis.

The deterministic engine runs first and decides where to look. Only its top few
candidates are sent for review, in order, until the budget stops — which is the design's
"candidate-first hybrid" rather than handing a whole trajectory to a model and hoping.

The result is additive: semantic signals join the deterministic ones and the ranking
re-runs. A model finding cannot remove a candidate, cannot change a status, and cannot
push anything past the unvalidated confidence cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from runopsy_core import AnalysisContext, diagnose
from runopsy_core.detectors import default_registry
from runopsy_core.ranking import rank_candidates
from runopsy_core.schema import DiagnosisBundle, FailureSignal
from runopsy_semantic.budget import Budget, Ledger
from runopsy_semantic.cache import VerdictCache
from runopsy_semantic.evaluator import SemanticVerdict, review_span, to_signal
from runopsy_semantic.payload import PayloadLookup, build_packet, window_around
from runopsy_semantic.provider import OpenRouterClient

MAX_REVIEWED_CANDIDATES = 3
UNEXPLAINED_LOOKBACK = 3
"""Steps before an unexplained failure that the semantic layer may inspect.

Bounded, but not zero. Reviewing only the deterministic candidates was the first design
and it was blind by construction: a step that succeeds while doing the wrong thing never
becomes a candidate, and that is precisely the class this layer exists to catch. When the
engine can point at a failure but at nothing upstream that explains it, the steps just
before it are the only place an explanation could be.
"""


def _review_targets(
    bundle: DiagnosisBundle, positions: dict[str, int], max_candidates: int
) -> list[tuple[str, int]]:
    """Which steps to spend a call on, best prospect first.

    Deterministic candidates come first: they are already evidenced, so a model finding
    there is cheap corroboration. Only when the failure is *unexplained* — the sole
    candidate being the symptom itself — does the search widen to the steps before it.
    """
    targets: list[tuple[str, int]] = []
    seen: set[str] = set()

    for candidate in bundle.candidates[:max_candidates]:
        sequence = positions.get(candidate.onset_node_id)
        if sequence is not None and candidate.onset_node_id not in seen:
            seen.add(candidate.onset_node_id)
            targets.append((candidate.onset_node_id, sequence))

    explained = any(node_id != bundle.observed_failure_node_id for node_id, _ in targets)
    symptom_at = positions.get(bundle.observed_failure_node_id or "")
    if not explained and symptom_at is not None:
        earlier = sorted(
            ((node_id, seq) for node_id, seq in positions.items() if seq < symptom_at),
            key=lambda item: item[1],
            reverse=True,
        )
        for node_id, sequence in earlier[:UNEXPLAINED_LOOKBACK]:
            if node_id not in seen:
                seen.add(node_id)
                targets.append((node_id, sequence))

    return targets


@dataclass
class HybridResult:
    """The diagnosis, plus what the paid layer contributed and cost."""

    bundle: DiagnosisBundle
    ledger: Ledger
    verdicts: dict[str, SemanticVerdict] = field(default_factory=dict)
    withheld: tuple[str, ...] = ()

    @property
    def semantic_findings(self) -> int:
        return sum(1 for verdict in self.verdicts.values() if verdict.finding)


def review_diagnosis(
    context: AnalysisContext,
    client: OpenRouterClient,
    *,
    budget: Budget | None = None,
    vault: PayloadLookup | None = None,
    cache_dir: Path | None = None,
    max_candidates: int = MAX_REVIEWED_CANDIDATES,
) -> HybridResult:
    """Run the deterministic diagnosis, then review its leading candidates.

    Candidates are reviewed strongest first so that when the budget runs out, it ran out
    on the least promising question rather than the most.
    """
    ledger = Ledger(budget=budget or Budget())
    registry = default_registry()
    deterministic = registry.run(context)
    bundle = diagnose(context, registry=registry)

    if ledger.budget.disabled or not bundle.candidates:
        return HybridResult(bundle=bundle, ledger=ledger)

    cache = VerdictCache(cache_dir) if cache_dir is not None else None
    positions = {node.node_id: node.sequence for node in context.graph.nodes}
    verdicts: dict[str, SemanticVerdict] = {}
    extra: list[FailureSignal] = []
    withheld: set[str] = set()

    for node_id, sequence in _review_targets(bundle, positions, max_candidates):
        window = window_around(context.events, sequence)
        if not window:
            continue

        packet = build_packet(context.run_id, window, sequence, vault=vault)
        withheld.update(packet.withheld)

        verdict = review_span(packet, client, ledger, cache=cache)
        if verdict is None:
            # Budget exhausted or the provider declined; the deterministic answer stands.
            break
        verdicts[node_id] = verdict
        signal = to_signal(verdict, node_id)
        if signal is not None:
            extra.append(signal)

    if extra:
        combined = tuple(sorted({*deterministic, *extra}, key=lambda s: s.signal_id))
        bundle = DiagnosisBundle(
            **{
                **bundle.model_dump(),
                "candidates": rank_candidates(context, combined),
                "tokens_spent": ledger.total_tokens,
                "cost_usd": round(ledger.cost_usd, 6),
            }
        )
    else:
        bundle = DiagnosisBundle(
            **{
                **bundle.model_dump(),
                "tokens_spent": ledger.total_tokens,
                "cost_usd": round(ledger.cost_usd, 6),
            }
        )

    return HybridResult(
        bundle=bundle, ledger=ledger, verdicts=verdicts, withheld=tuple(sorted(withheld))
    )
