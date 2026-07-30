"""Graph and diagnosis tests.

The invariants here are the product's epistemic guardrails, not stylistic preferences:
a diagnosis engine that can be talked into a confident wrong answer is worse than none.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from runopsy_core.schema import (
    AnalysisLayer,
    DiagnosisBundle,
    DiagnosisCandidate,
    DiagnosisStatus,
    EdgeKind,
    FailureCategory,
    FailureSignal,
    NodeKind,
    Severity,
    TraceEdge,
    TraceGraph,
    TraceNode,
)

NOW = datetime(2026, 7, 30, 9, 45, tzinfo=UTC)


def node(node_id: str, sequence: int, kind: NodeKind = NodeKind.TOOL_CALL) -> TraceNode:
    return TraceNode(
        node_id=node_id, kind=kind, run_id="run_0042", sequence=sequence, timestamp=NOW
    )


def candidate(**overrides: object) -> DiagnosisCandidate:
    defaults: dict[str, object] = {
        "candidate_id": "cand_1",
        "onset_node_id": "step_9",
        "category": FailureCategory.TOOL_ARGUMENTS,
        "status": DiagnosisStatus.SUSPECTED_ONSET,
        "confidence": 0.6,
        "score": 4.2,
        "summary": "wrong endpoint assumption for the test environment",
    }
    return DiagnosisCandidate(**{**defaults, **overrides})


class TestDefinitiveClaims:
    """A cause may only be stated as established when something actually established it."""

    def test_replay_supported_requires_a_replay_run(self) -> None:
        with pytest.raises(ValidationError, match="replay_run_id"):
            candidate(status=DiagnosisStatus.REPLAY_SUPPORTED)

    def test_human_verified_requires_a_verifier(self) -> None:
        with pytest.raises(ValidationError, match="verified_by"):
            candidate(status=DiagnosisStatus.HUMAN_VERIFIED)

    def test_replay_supported_is_accepted_once_a_replay_backs_it(self) -> None:
        result = candidate(
            status=DiagnosisStatus.REPLAY_SUPPORTED, replay_run_id="run_0042_replay_1"
        )

        assert result.is_definitive is True

    @pytest.mark.parametrize(
        "status",
        [
            DiagnosisStatus.SUSPECTED_ONSET,
            DiagnosisStatus.CORRELATED_CAUSE,
            DiagnosisStatus.OBSERVED_FAILURE,
            DiagnosisStatus.UNKNOWN,
        ],
    )
    def test_unvalidated_statuses_are_never_definitive(self, status: DiagnosisStatus) -> None:
        assert candidate(status=status).is_definitive is False


class TestFailureSignal:
    def test_a_semantic_detector_cannot_claim_a_token_free_layer(self) -> None:
        with pytest.raises(ValidationError, match="deterministic layer"):
            FailureSignal(
                signal_id="sig_1",
                node_id="step_9",
                category=FailureCategory.REASONING,
                severity=Severity.MEDIUM,
                layer=AnalysisLayer.L0_STRUCTURAL,
                detector="semantic:claim_evidence",
                summary="unsupported inference",
            )

    def test_a_semantic_detector_may_report_the_semantic_layer(self) -> None:
        signal = FailureSignal(
            signal_id="sig_1",
            node_id="step_9",
            category=FailureCategory.REASONING,
            severity=Severity.MEDIUM,
            layer=AnalysisLayer.L3_SEMANTIC,
            detector="semantic:claim_evidence",
            summary="unsupported inference",
        )

        assert signal.layer is AnalysisLayer.L3_SEMANTIC


class TestTraceGraph:
    def test_edges_pointing_at_missing_nodes_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown nodes"):
            TraceGraph(
                run_id="run_0042",
                nodes=(node("step_9", 9),),
                edges=(TraceEdge(source_id="step_9", target_id="step_14", kind=EdgeKind.AFFECTS),),
            )

    def test_descendants_follow_the_propagation_chain(self) -> None:
        graph = TraceGraph(
            run_id="run_0042",
            nodes=(node("step_9", 9), node("step_11", 11), node("step_14", 14)),
            edges=(
                TraceEdge(source_id="step_9", target_id="step_11", kind=EdgeKind.AFFECTS),
                TraceEdge(source_id="step_11", target_id="step_14", kind=EdgeKind.AFFECTS),
            ),
        )

        assert graph.descendants("step_9") == ("step_11", "step_14")

    def test_descendants_terminate_on_a_retry_loop(self) -> None:
        """Real traces cycle when an agent retries, so traversal cannot assume a DAG."""
        graph = TraceGraph(
            run_id="run_0042",
            nodes=(node("a", 1), node("b", 2)),
            edges=(
                TraceEdge(source_id="a", target_id="b", kind=EdgeKind.AFFECTS),
                TraceEdge(source_id="b", target_id="a", kind=EdgeKind.AFFECTS),
            ),
        )

        assert graph.descendants("a") == ("b",)

    def test_descendants_can_be_restricted_to_one_edge_kind(self) -> None:
        graph = TraceGraph(
            run_id="run_0042",
            nodes=(node("step_9", 9), node("step_11", 11), node("step_14", 14)),
            edges=(
                TraceEdge(source_id="step_9", target_id="step_11", kind=EdgeKind.AFFECTS),
                TraceEdge(source_id="step_11", target_id="step_14", kind=EdgeKind.PRECEDES),
            ),
        )

        reachable = graph.descendants("step_9", kinds=frozenset({EdgeKind.AFFECTS}))

        assert reachable == ("step_11",)

    def test_in_order_sorts_by_sequence_not_insertion(self) -> None:
        graph = TraceGraph(run_id="run_0042", nodes=(node("late", 14), node("early", 9)))

        assert [n.node_id for n in graph.in_order()] == ["early", "late"]


class TestDiagnosisBundle:
    def test_primary_is_the_highest_scoring_candidate(self) -> None:
        bundle = DiagnosisBundle(
            diagnosis_id="diag_1",
            run_id="run_0042",
            created_at=NOW,
            candidates=(
                candidate(candidate_id="weak", score=1.0),
                candidate(candidate_id="strong", score=7.5),
            ),
        )

        assert bundle.primary is not None
        assert bundle.primary.candidate_id == "strong"

    def test_an_empty_bundle_has_no_primary_rather_than_a_fabricated_one(self) -> None:
        bundle = DiagnosisBundle(diagnosis_id="diag_1", run_id="run_0042", created_at=NOW)

        assert bundle.primary is None

    def test_observed_failure_is_recorded_separately_from_the_hypotheses(self) -> None:
        """The fact and the guess must stay distinguishable in the data, not just the UI."""
        bundle = DiagnosisBundle(
            diagnosis_id="diag_1",
            run_id="run_0042",
            created_at=NOW,
            observed_failure_node_id="step_14",
            observed_failure_summary="integration test exit code 1",
            candidates=(candidate(onset_node_id="step_9"),),
        )

        assert bundle.observed_failure_node_id == "step_14"
        assert bundle.primary is not None
        assert bundle.primary.onset_node_id == "step_9"
        assert bundle.primary.status is DiagnosisStatus.SUSPECTED_ONSET
