"""Fault injection tests.

Injection is only worth anything if the label is exactly right, so most of what follows
checks the *fixture generator* rather than the engine: one fault per trace, at the step
we say, with the run's outcome kept consistent with what was done to it.

The last class is the payoff — the engine scored against faults nobody hand-tuned it for.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import START, run_end, run_start
from runopsy_bench.injection import (
    FaultKind,
    applicable_kinds,
    inject,
    injection_campaign,
)
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import (
    CallStatus,
    Event,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    StateChange,
    ToolCallEvent,
    ToolPayload,
)

RUN = "run_healthy"


def tool(sequence: int, name: str = "step") -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=0),
        state_delta={"ready": StateChange(after=True)},
    )


def llm(sequence: int) -> LlmCallEvent:
    return LlmCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        llm=LlmPayload(model="local:qwen"),
    )


def healthy_run() -> list[Event]:
    """A clean run: everything succeeds, the outcome is success."""
    return [
        run_start(RUN, task="build and verify"),
        llm(1),
        tool(2, "fetch"),
        tool(3, "build"),
        tool(4, "test"),
        run_end(5, RUN, outcome=RunOutcome.SUCCESS),
    ]


def analyse(events: tuple[Event, ...]) -> AnalysisContext:
    return AnalysisContext.from_events(RUN, events)


class TestTheHealthyBaseline:
    def test_the_starting_run_produces_no_findings(self) -> None:
        """If the base run were not clean, every injected label would be contaminated."""
        bundle = diagnose(analyse(tuple(healthy_run())))

        assert bundle.candidates == ()


class TestLabelCorrectness:
    @pytest.mark.parametrize(
        "kind",
        [
            FaultKind.TOOL_FAILURE,
            FaultKind.TIMEOUT,
            FaultKind.RETRY_STORM,
            FaultKind.STALE_MEMORY,
            FaultKind.SILENT_WRONG_VALUE,
        ],
    )
    def test_the_onset_is_the_step_we_broke(self, kind: FaultKind) -> None:
        fault = inject(kind, healthy_run(), 3)

        assert fault.onset_step == 3

    def test_the_run_outcome_matches_what_was_done_to_it(self) -> None:
        """A run claiming success while carrying an injected failure is a different fault."""
        fault = inject(FaultKind.TOOL_FAILURE, healthy_run(), 3)

        ending = next(e for e in fault.events if isinstance(e, RunEndEvent))
        assert ending.run.outcome is RunOutcome.FAILURE

    def test_a_tool_failure_actually_marks_the_step_failed(self) -> None:
        fault = inject(FaultKind.TOOL_FAILURE, healthy_run(), 3)

        broken = next(e for e in fault.events if isinstance(e, ToolCallEvent) and e.sequence == 3)
        assert broken.tool.status is CallStatus.ERROR

    def test_a_retry_storm_keeps_sequences_contiguous(self) -> None:
        """A gap would make the trace look corrupted rather than looping."""
        fault = inject(FaultKind.RETRY_STORM, healthy_run(), 3)

        sequences = sorted(event.sequence for event in fault.events)
        assert sequences == list(range(min(sequences), max(sequences) + 1))

    def test_stale_memory_insertion_keeps_sequences_contiguous(self) -> None:
        fault = inject(FaultKind.STALE_MEMORY, healthy_run(), 3)

        sequences = sorted(event.sequence for event in fault.events)
        assert sequences == list(range(min(sequences), max(sequences) + 1))

    def test_dropping_events_is_labelled_as_an_evidence_problem(self) -> None:
        fault = inject(FaultKind.DROPPED_EVENTS, healthy_run(), 3)

        assert 3 not in {event.sequence for event in fault.events}
        assert "flag the gap" in fault.note

    def test_the_silent_fault_is_declared_undetectable(self) -> None:
        """Keeping an unreachable case honest is the point of labelling it."""
        fault = inject(FaultKind.SILENT_WRONG_VALUE, healthy_run(), 3)

        assert fault.deterministically_detectable is False

    def test_injecting_where_it_cannot_apply_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no model call"):
            inject(FaultKind.TRUNCATED_PLAN, healthy_run(), 3)


class TestApplicability:
    def test_a_tool_step_accepts_tool_faults(self) -> None:
        kinds = applicable_kinds(healthy_run(), 3)

        assert FaultKind.TOOL_FAILURE in kinds
        assert FaultKind.TRUNCATED_PLAN not in kinds

    def test_a_model_step_accepts_truncation(self) -> None:
        kinds = applicable_kinds(healthy_run(), 1)

        assert FaultKind.TRUNCATED_PLAN in kinds

    def test_an_unknown_step_accepts_nothing(self) -> None:
        assert applicable_kinds(healthy_run(), 99) == ()


class TestCampaign:
    def test_it_produces_one_fault_per_trace(self) -> None:
        """Two faults at once would leave the label ambiguous."""
        faults = injection_campaign(healthy_run())

        assert faults
        assert all(fault.onset_step is not None for fault in faults)

    def test_it_only_breaks_steps_that_were_healthy(self) -> None:
        faults = injection_campaign(healthy_run(), kinds=[FaultKind.TOOL_FAILURE])

        assert {fault.onset_step for fault in faults} == {2, 3, 4}

    def test_it_can_be_narrowed_to_one_kind(self) -> None:
        faults = injection_campaign(healthy_run(), kinds=[FaultKind.TIMEOUT])

        assert {fault.kind for fault in faults} == {FaultKind.TIMEOUT}


class TestTheEngineAgainstInjectedFaults:
    """Scored against faults the detectors were not written from."""

    def _score(self, kind: FaultKind) -> tuple[int, int]:
        """Return (exact hits, cases) for one fault kind across a healthy run."""
        hits = cases = 0
        for fault in injection_campaign(healthy_run(), kinds=[kind]):
            if not fault.deterministically_detectable:
                continue
            cases += 1
            context = analyse(fault.events)
            bundle = diagnose(context)
            positions = {node.node_id: node.sequence for node in context.graph.nodes}
            predicted = [positions.get(candidate.onset_node_id) for candidate in bundle.candidates]
            if predicted and predicted[0] == fault.onset_step:
                hits += 1
        return hits, cases

    @pytest.mark.parametrize(
        "kind", [FaultKind.TOOL_FAILURE, FaultKind.TIMEOUT, FaultKind.RETRY_STORM]
    )
    def test_the_injected_step_is_ranked_first(self, kind: FaultKind) -> None:
        hits, cases = self._score(kind)

        assert cases > 0
        assert hits == cases, f"{kind}: found {hits} of {cases}"

    def test_a_dropped_event_makes_the_engine_distrust_the_trace(self) -> None:
        """The right answer is 'this evidence has a hole', not a confident culprit."""
        fault = inject(FaultKind.DROPPED_EVENTS, healthy_run(), 3)
        context = analyse(fault.events)

        assert context.integrity.is_intact is False

    def test_the_silent_fault_is_still_missed_and_that_is_recorded(self) -> None:
        """Honest coverage: this is what the semantic layer and replay exist for."""
        fault = inject(FaultKind.SILENT_WRONG_VALUE, healthy_run(), 3)

        bundle = diagnose(analyse(fault.events))

        assert not any(candidate.onset_node_id == "evt_3" for candidate in bundle.candidates)


class TestScoringUsesTheRightQuestion:
    def test_a_removed_onset_is_scored_on_noticing_the_gap(self) -> None:
        """Reporting 0% localization there would be a meaningless number beside real ones."""
        from runopsy_bench import score_injections

        scores = score_injections(healthy_run(), kinds=[FaultKind.DROPPED_EVENTS])

        assert scores
        assert scores[0].measure == "gap noticed"
        assert scores[0].top1 == 1.0

    def test_localizable_faults_are_scored_on_the_onset(self) -> None:
        from runopsy_bench import score_injections

        scores = score_injections(healthy_run(), kinds=[FaultKind.TOOL_FAILURE])

        assert scores[0].measure == "onset"
        assert scores[0].top1 == 1.0

    def test_undetectable_faults_are_excluded_and_counted(self) -> None:
        from runopsy_bench import score_injections

        scores = score_injections(healthy_run(), kinds=[FaultKind.SILENT_WRONG_VALUE])

        assert all(score.scored == 0 for score in scores) or not scores
