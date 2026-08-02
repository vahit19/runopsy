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
from runopsy_core.schema import DiagnosisBundle, FailureSignal, NodeKind
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


_CONTAINER_KINDS = frozenset({NodeKind.RUN, NodeKind.AGENT})
"""Nodes that are not steps, so cannot be a place to look."""


def _ended_in_failure(context: AnalysisContext) -> bool:
    """Whether the run reported that it failed."""
    from runopsy_core.schema import RunEndEvent, RunOutcome

    ended = next((event for event in context.events if isinstance(event, RunEndEvent)), None)
    return ended is not None and ended.run.outcome is RunOutcome.FAILURE


def _unexplained_targets(context: AnalysisContext, limit: int) -> list[tuple[str, int]]:
    """Where to look when a run failed and the deterministic layers found nothing.

    This is the case worth paying for, and until now it was the one case hybrid mode
    refused to run: no candidate meant an immediate return, so a user whose agent had
    failed silently — nothing errored, nothing timed out, the answer was simply wrong —
    asked for a model's opinion and was charged for nothing while being told the run was
    clean. Measured on an external benchmark of annotated multi-agent failures, every
    single case is that shape.

    The search starts at the end and walks back, because a failure that left no
    structural trace is most likely to be legible where its consequences are: the last
    thing the run did before it stopped. Nothing here claims that is *where* it went
    wrong — the model is asked, the answer arrives as an ordinary semantic signal, and
    it is capped below certainty like every other unvalidated finding.
    """
    steps = [
        node
        for node in sorted(context.graph.nodes, key=lambda node: node.sequence)
        if node.kind not in _CONTAINER_KINDS
    ]
    return [(node.node_id, node.sequence) for node in steps[-limit:]]


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

    if ledger.budget.disabled:
        return HybridResult(bundle=bundle, ledger=ledger)

    if not bundle.candidates and not _ended_in_failure(context):
        # Nothing detected and nothing reported wrong: there is no question to ask, and
        # asking one anyway would spend a user's money to be told a healthy run is
        # healthy.
        return HybridResult(bundle=bundle, ledger=ledger)

    cache = VerdictCache(cache_dir) if cache_dir is not None else None
    positions = {node.node_id: node.sequence for node in context.graph.nodes}
    verdicts: dict[str, SemanticVerdict] = {}
    extra: list[FailureSignal] = []
    withheld: set[str] = set()

    targets = (
        _review_targets(bundle, positions, max_candidates)
        if bundle.candidates
        else _unexplained_targets(context, max_candidates)
    )
    for node_id, sequence in targets:
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
