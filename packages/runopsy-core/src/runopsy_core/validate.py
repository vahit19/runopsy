"""L4 validation: folding replay evidence back into a diagnosis.

This is the only code that may produce ``replay_supported``. The schema refuses the
status without a ``replay_run_id``, the language layer reserves causal wording for it,
and the ranking caps everything else below certainty — all of that machinery exists so
that the upgrade performed here means something when it finally happens.

The bar is deliberately narrow: an intervention was applied at the candidate's own step,
and the downstream failures that existed in the original run were re-run and passed.
Reproduction without an intervention upgrades nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from runopsy_core.impact import affected_nodes
from runopsy_core.schema import (
    DiagnosisBundle,
    DiagnosisCandidate,
    DiagnosisStatus,
    FailureCategory,
    TraceGraph,
)

REPLAY_SUPPORTED_CONFIDENCE: Final = 0.9
"""Confidence for a replay-backed cause.

Above the unvalidated cap, deliberately short of 1.0: a single replay on a
non-deterministic system is strong evidence, not proof, and the design document reserves
certainty for repeated seeds or a human verdict.
"""


@dataclass(frozen=True)
class ReplayEvidence:
    """The facts a replay experiment established, independent of how it was run."""

    replay_run_id: str
    parent_run_id: str
    intervention_target: int | None
    outcome_changed: bool
    intervened: bool

    @property
    def supports(self) -> bool:
        return self.intervened and self.outcome_changed


def apply_replay_evidence(
    bundle: DiagnosisBundle, graph: TraceGraph, evidence: ReplayEvidence
) -> DiagnosisBundle:
    """Fold one experiment's result into the bundle, touching only its target.

    A replay that helped one hypothesis says nothing about its rivals, so no other
    candidate moves. If no candidate exists at the target step — the silent-failure case
    the deterministic layers cannot see, because nothing at that step looked anomalous —
    a new one is created. That is not a loophole; it is the point of validation: an
    experiment can establish a cause that no amount of trace-reading could.
    """
    if not evidence.supports or evidence.parent_run_id != bundle.run_id:
        return bundle

    positions = {node.node_id: node.sequence for node in graph.nodes}
    upgraded: list[DiagnosisCandidate] = []
    changed = False

    for candidate in bundle.candidates:
        if positions.get(candidate.onset_node_id) != evidence.intervention_target:
            upgraded.append(candidate)
            continue
        changed = True
        # Rebuilt rather than model_copy'd so the schema validator re-runs and the
        # replay_run_id requirement is actually enforced at the moment it matters.
        upgraded.append(
            DiagnosisCandidate(
                **{
                    **candidate.model_dump(),
                    "status": DiagnosisStatus.REPLAY_SUPPORTED,
                    "replay_run_id": evidence.replay_run_id,
                    "confidence": max(candidate.confidence, REPLAY_SUPPORTED_CONFIDENCE),
                }
            )
        )

    if not changed:
        onset = next(
            (
                node
                for node in graph.nodes
                if node.sequence == evidence.intervention_target
                and node.kind.value not in {"run", "agent"}
            ),
            None,
        )
        if onset is None:
            return bundle
        changed = True
        upgraded.insert(
            0,
            DiagnosisCandidate(
                candidate_id=f"cand:{onset.node_id}:replay",
                onset_node_id=onset.node_id,
                category=FailureCategory.UNDETERMINED,
                status=DiagnosisStatus.REPLAY_SUPPORTED,
                confidence=REPLAY_SUPPORTED_CONFIDENCE,
                score=REPLAY_SUPPORTED_CONFIDENCE,
                summary=(
                    "nothing at this step looked anomalous, but changing it made the "
                    "downstream failures disappear"
                ),
                affected_node_ids=affected_nodes(graph, onset.node_id),
                replay_run_id=evidence.replay_run_id,
            ),
        )

    return DiagnosisBundle(**{**bundle.model_dump(), "candidates": tuple(upgraded)})
