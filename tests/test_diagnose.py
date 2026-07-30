"""Ranking, impact and end-to-end diagnosis tests.

The scenario at the bottom is the one the product exists for: the failure a user sees is
late, and the step that actually broke the run is much earlier.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import START, run_end, run_start
from runopsy_core import (
    MAX_UNVALIDATED_CONFIDENCE,
    AnalysisContext,
    affected_nodes,
    diagnose,
    infer_affects,
    trace_fingerprint,
)
from runopsy_core.impact import confidence_at
from runopsy_core.schema import (
    CallStatus,
    DiagnosisStatus,
    EdgeKind,
    Event,
    RunOutcome,
    StateChange,
    ToolCallEvent,
    ToolPayload,
)

RUN = "run_0042"
DIGEST = "sha256:" + "a" * 64


def tool(
    sequence: int,
    *,
    exit_code: int = 0,
    name: str = "terminal",
    state: dict[str, object] | None = None,
    retry_of: str | None = None,
    status: CallStatus = CallStatus.OK,
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code, retry_of=retry_of, status=status),
        state_delta={key: StateChange(after=value) for key, value in (state or {}).items()},
    )


def context(*events: Event) -> AnalysisContext:
    return AnalysisContext.from_events(RUN, events)


class TestImpact:
    def test_downstream_steps_are_reachable(self) -> None:
        ctx = context(tool(1), tool(2), tool(3))

        assert affected_nodes(ctx.graph, "evt_1") == ("evt_2", "evt_3")

    def test_nothing_affects_the_past(self) -> None:
        """A retry points back at the call it repeats; that must not reverse causality."""
        ctx = context(tool(1), tool(2, retry_of="evt_1"))

        assert affected_nodes(ctx.graph, "evt_2") == ()

    def test_confidence_decays_with_distance(self) -> None:
        ctx = context(tool(1), tool(2), tool(3))

        edges = {edge.target_id: edge.confidence for edge in infer_affects(ctx.graph, "evt_1")}

        assert edges["evt_2"] == confidence_at(1)
        assert edges["evt_3"] == confidence_at(2)
        assert edges["evt_3"] < edges["evt_2"]

    def test_inferred_edges_are_labelled_as_inference(self) -> None:
        """A proposed edge must never be mistaken for one the adapter reported."""
        ctx = context(tool(1), tool(2))

        edge = infer_affects(ctx.graph, "evt_1")[0]

        assert edge.kind is EdgeKind.AFFECTS
        assert edge.detector == "impact:reachability"
        assert edge.confidence < 1.0

    def test_propagation_stops_at_the_hop_limit(self) -> None:
        ctx = context(*(tool(i) for i in range(1, 12)))

        assert len(affected_nodes(ctx.graph, "evt_1", max_hops=3)) == 3

    def test_an_unknown_node_has_no_impact(self) -> None:
        ctx = context(tool(1))

        assert affected_nodes(ctx.graph, "nope") == ()


class TestConfidence:
    def test_no_unvalidated_candidate_reaches_certainty(self) -> None:
        """Weights are expert guesses, not fitted probabilities; the cap says so."""
        bundle = diagnose(context(run_start(RUN), tool(1, exit_code=1), run_end(2, RUN)))

        assert bundle.candidates
        for candidate in bundle.candidates:
            assert candidate.confidence <= MAX_UNVALIDATED_CONFIDENCE
            assert candidate.is_definitive is False

    def test_a_broken_trace_lowers_confidence(self) -> None:
        """Conclusions drawn over a hole in the evidence must be held more weakly."""
        intact = diagnose(context(run_start(RUN), tool(1, exit_code=1), tool(2)))
        gapped = diagnose(context(run_start(RUN), tool(1, exit_code=1), tool(3)))

        assert gapped.primary is not None
        assert intact.primary is not None
        assert gapped.primary.confidence < intact.primary.confidence


class TestCandidateSelection:
    def test_steps_after_the_symptom_are_not_nominated(self) -> None:
        """Cleanup clusters after a failure and would otherwise be blamed for it."""
        events = [
            run_start(RUN),
            tool(5, exit_code=1),
            tool(6, name="rm", status=CallStatus.BLOCKED),
        ]

        bundle = diagnose(context(*events))
        nominated = {candidate.onset_node_id for candidate in bundle.candidates}

        assert nominated == {"evt_5"}

    def test_the_earlier_of_two_failures_outranks_the_later_one(self) -> None:
        events = [run_start(RUN), tool(5, exit_code=1), tool(9, exit_code=1)]

        bundle = diagnose(context(*events))

        assert bundle.observed_failure_node_id == "evt_9"
        assert bundle.primary is not None
        assert bundle.primary.onset_node_id == "evt_5"

    def test_the_trace_integrity_signal_is_evidence_not_a_cause(self) -> None:
        """A gap in the recording says the evidence is weak, not that a step misbehaved."""
        bundle = diagnose(context(run_start(RUN), tool(1, exit_code=1), tool(3)))

        for candidate in bundle.candidates:
            assert "trace_integrity" not in " ".join(candidate.signal_ids)

    def test_a_healthy_run_yields_no_diagnosis(self) -> None:
        events = [run_start(RUN), tool(1), tool(2), run_end(3, RUN, outcome=RunOutcome.SUCCESS)]

        bundle = diagnose(context(*events))

        assert bundle.candidates == ()
        assert bundle.observed_failure_node_id is None
        assert bundle.primary is None

    def test_an_empty_trace_is_not_an_error(self) -> None:
        bundle = diagnose(context())

        assert bundle.candidates == ()


class TestReproducibility:
    def test_the_same_trace_produces_the_same_diagnosis(self) -> None:
        events = [run_start(RUN), tool(1, exit_code=1), tool(2), run_end(3, RUN)]

        first = diagnose(context(*events))
        second = diagnose(context(*events))

        assert first == second

    def test_the_fingerprint_changes_when_the_run_changes(self) -> None:
        base = context(run_start(RUN), tool(1))
        altered = context(run_start(RUN), tool(1), tool(2))

        assert trace_fingerprint(base) != trace_fingerprint(altered)

    def test_no_tokens_are_spent(self) -> None:
        """The deterministic path is the whole product for an offline user."""
        bundle = diagnose(context(run_start(RUN), tool(1, exit_code=1)))

        assert bundle.tokens_spent == 0
        assert bundle.cost_usd == 0.0


class TestTheScenarioTheProductExistsFor:
    """Design document section C: the test fails at step 14, the run broke at step 9."""

    def _events(self) -> list[Event]:
        return [
            run_start(RUN, task="fix the failing integration test"),
            tool(9, name="write_config", exit_code=1, state={"endpoint": "staging"}),
            tool(10, name="edit_file"),
            tool(11, name="edit_file"),
            tool(12, name="restart_service"),
            tool(13, name="edit_file"),
            tool(14, name="pytest", exit_code=1),
            run_end(15, RUN, outcome=RunOutcome.FAILURE),
        ]

    def test_the_observed_failure_is_the_late_visible_one(self) -> None:
        bundle = diagnose(context(*self._events()))

        assert bundle.observed_failure_node_id == "evt_14"
        assert "pytest" in bundle.observed_failure_summary

    def test_the_top_candidate_is_the_earlier_step_not_the_symptom(self) -> None:
        bundle = diagnose(context(*self._events()))

        assert bundle.primary is not None
        assert bundle.primary.onset_node_id == "evt_9"

    def test_the_onset_is_offered_as_a_suspicion_not_a_verdict(self) -> None:
        bundle = diagnose(context(*self._events()))

        assert bundle.primary is not None
        assert bundle.primary.status is DiagnosisStatus.SUSPECTED_ONSET
        assert bundle.primary.replay_run_id is None

    def test_the_symptom_is_still_listed_and_labelled_as_observed(self) -> None:
        bundle = diagnose(context(*self._events()))

        symptom = next(c for c in bundle.candidates if c.onset_node_id == "evt_14")

        assert symptom.status is DiagnosisStatus.OBSERVED_FAILURE

    def test_the_propagation_chain_reaches_the_failing_test(self) -> None:
        bundle = diagnose(context(*self._events()))

        assert bundle.primary is not None
        assert "evt_14" in bundle.primary.affected_node_ids

    def test_the_ranking_can_be_audited(self) -> None:
        """A user who disagrees with the ordering must be able to see what drove it."""
        bundle = diagnose(context(*self._events()))

        assert bundle.primary is not None
        breakdown = bundle.primary.score_breakdown

        assert set(breakdown) == {
            "severity",
            "precedence",
            "state_anomaly",
            "downstream_impact",
            "evaluator_support",
            "uncertainty_penalty",
        }
        assert breakdown["precedence"] > 0, "the early step should be credited for running early"
        assert breakdown["downstream_impact"] > 0, "it should be credited for what it touched"

    def test_the_symptom_earns_no_precedence_credit(self) -> None:
        """Being the last thing to fail is not evidence of being the first thing wrong."""
        bundle = diagnose(context(*self._events()))

        symptom = next(c for c in bundle.candidates if c.onset_node_id == "evt_14")

        assert symptom.score_breakdown["precedence"] == 0.0
