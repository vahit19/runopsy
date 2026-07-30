"""Detector tests.

Every detector must be a pure function of the trace: no clock, no network, no model.
The registry tests at the end assert that property for the whole default set, because
it is the guarantee the token-free promise rests on.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

import pytest

from conftest import START, run_end, run_start, tool_call
from runopsy_core import AnalysisContext, DetectorSettings, default_registry
from runopsy_core.detectors import (
    BudgetDetector,
    DetectorRegistry,
    IncompleteHandoffDetector,
    OutcomeMismatchDetector,
    RetryStormDetector,
    StaleMemoryDetector,
    StateFlappingDetector,
    ToolExecutionDetector,
    ToolLoopDetector,
    UnsupportedClaimDetector,
)
from runopsy_core.schema import (
    AnalysisLayer,
    CallStatus,
    ClaimEvent,
    ClaimPayload,
    Event,
    FailureCategory,
    FailureSignal,
    HandoffEvent,
    HandoffPayload,
    LlmCallEvent,
    LlmPayload,
    MemoryOperation,
    MemoryOpEvent,
    MemoryPayload,
    RunOutcome,
    Severity,
    StateChange,
    SupportStatus,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
RUN = "run_0042"


def context(*events: Event, **overrides: object) -> AnalysisContext:
    settings = DetectorSettings().with_overrides(**overrides) if overrides else None
    return AnalysisContext.from_events(RUN, events, settings)


def llm_call(sequence: int, **payload: object) -> LlmCallEvent:
    defaults: dict[str, object] = {"model": "local:qwen"}
    return LlmCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        llm=LlmPayload(**{**defaults, **payload}),
    )


def tool(sequence: int, **payload: object) -> ToolCallEvent:
    defaults: dict[str, object] = {"name": "terminal"}
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(**{**defaults, **payload}),
    )


def claim(sequence: int, status: SupportStatus) -> ClaimEvent:
    return ClaimEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        claim=ClaimPayload(claim_id=f"c{sequence}", text_hash=DIGEST, support_status=status),
    )


def with_state(sequence: int, value: object) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name="terminal"),
        state_delta={"tests_passed": StateChange(after=value)},
    )


def only(signals: Iterable[FailureSignal]) -> FailureSignal:
    materialized = tuple(signals)
    assert len(materialized) == 1, f"expected exactly one signal, got {materialized}"
    return materialized[0]


class TestToolExecution:
    def test_a_nonzero_exit_code_is_reported(self) -> None:
        signal = only(ToolExecutionDetector().detect(context(tool(1, exit_code=1))))

        assert signal.category is FailureCategory.TOOL_EXECUTION
        assert "exit code 1" in signal.summary

    def test_a_successful_call_is_silent(self) -> None:
        assert tuple(ToolExecutionDetector().detect(context(tool(1, exit_code=0)))) == ()

    def test_a_timeout_is_not_double_reported_as_an_execution_error(self) -> None:
        """Timeout and error are different problems and lead to different fixes."""
        signals = ToolExecutionDetector().detect(context(tool(1, status=CallStatus.TIMEOUT)))

        assert tuple(signals) == ()


class TestRetryAndLoop:
    def test_retries_below_the_threshold_are_silent(self) -> None:
        events = [tool(1), *(tool(i, retry_of="evt_1") for i in range(2, 4))]

        assert tuple(RetryStormDetector().detect(context(*events))) == ()

    def test_a_retry_storm_is_reported_against_the_original_call(self) -> None:
        events = [tool(1), *(tool(i, retry_of="evt_1") for i in range(2, 5))]

        signal = only(RetryStormDetector().detect(context(*events)))

        assert signal.node_id == "evt_1"
        assert len(signal.evidence_node_ids) == 3

    def test_identical_calls_are_a_loop(self) -> None:
        events = [tool(i, arguments_hash=DIGEST) for i in range(1, 4)]

        signal = only(ToolLoopDetector().detect(context(*events)))

        assert "3 times with identical arguments" in signal.summary

    def test_repeating_a_tool_with_changing_arguments_is_progress_not_a_loop(self) -> None:
        """Re-running a test suite while fixing it must not look like being stuck."""
        events = [
            tool(1, arguments_hash=DIGEST),
            tool(2, arguments_hash=OTHER_DIGEST),
            tool(3, arguments_hash=DIGEST),
        ]

        assert tuple(ToolLoopDetector().detect(context(*events))) == ()

    def test_calls_without_recorded_arguments_cannot_be_judged(self) -> None:
        events = [tool(i) for i in range(1, 5)]

        assert tuple(ToolLoopDetector().detect(context(*events))) == ()


class TestStateFlapping:
    def test_oscillation_between_values_is_reported(self) -> None:
        events = [
            with_state(1, True),
            with_state(2, False),
            with_state(3, True),
            with_state(4, False),
        ]

        signal = only(StateFlappingDetector().detect(context(*events)))

        assert signal.category is FailureCategory.STATE

    def test_a_key_that_settles_is_not_flapping(self) -> None:
        events = [with_state(1, False), with_state(2, True), with_state(3, True)]

        assert tuple(StateFlappingDetector().detect(context(*events))) == ()

    def test_a_single_change_is_not_flapping(self) -> None:
        events = [with_state(1, False), with_state(2, True)]

        assert tuple(StateFlappingDetector().detect(context(*events))) == ()


class TestOutcomeMismatch:
    def test_reported_success_alongside_a_failed_step_is_critical(self) -> None:
        events = [
            run_start(RUN),
            tool(1, exit_code=1),
            run_end(2, RUN, outcome=RunOutcome.SUCCESS),
        ]

        signal = only(OutcomeMismatchDetector().detect(context(*events)))

        assert signal.severity is Severity.CRITICAL
        assert signal.evidence_node_ids == ("evt_1",)

    def test_reported_failure_is_not_a_mismatch(self) -> None:
        events = [run_start(RUN), tool(1, exit_code=1), run_end(2, RUN)]

        assert tuple(OutcomeMismatchDetector().detect(context(*events))) == ()

    def test_clean_success_is_not_a_mismatch(self) -> None:
        events = [run_start(RUN), tool(1), run_end(2, RUN, outcome=RunOutcome.SUCCESS)]

        assert tuple(OutcomeMismatchDetector().detect(context(*events))) == ()


class TestMemoryHandoffAndClaims:
    def test_a_stale_read_is_reported(self) -> None:
        event = MemoryOpEvent(
            event_id="evt_1",
            run_id=RUN,
            sequence=1,
            timestamp=START,
            memory=MemoryPayload(
                operation=MemoryOperation.READ, key="deploy_target", age_seconds=200_000
            ),
        )

        signal = only(StaleMemoryDetector().detect(context(event)))

        assert signal.category is FailureCategory.MEMORY

    def test_a_read_without_a_recorded_age_is_not_guessed_about(self) -> None:
        event = MemoryOpEvent(
            event_id="evt_1",
            run_id=RUN,
            sequence=1,
            timestamp=START,
            memory=MemoryPayload(operation=MemoryOperation.READ, key="plan"),
        )

        assert tuple(StaleMemoryDetector().detect(context(event))) == ()

    def test_a_handoff_missing_fields_is_reported(self) -> None:
        event = HandoffEvent(
            event_id="evt_1",
            run_id=RUN,
            sequence=1,
            timestamp=START,
            handoff=HandoffPayload(
                from_agent_id="main",
                to_agent_id="tester",
                missing_fields=("repo", "branch"),
            ),
        )

        signal = only(IncompleteHandoffDetector().detect(context(event)))

        assert "repo, branch" in signal.summary

    def test_a_contradicted_claim_outranks_an_unsupported_one(self) -> None:
        signals = tuple(
            UnsupportedClaimDetector().detect(
                context(
                    claim(1, SupportStatus.UNSUPPORTED),
                    claim(2, SupportStatus.CONTRADICTED),
                    claim(3, SupportStatus.SUPPORTED),
                )
            )
        )

        assert [signal.severity for signal in signals] == [Severity.MEDIUM, Severity.HIGH]


class TestBudget:
    def test_budgets_are_off_until_a_ceiling_is_set(self) -> None:
        """A default ceiling would be a guess about what the user finds expensive."""
        event = llm_call(1, tokens=TokenUsage(input_tokens=10_000, output_tokens=10_000))

        assert tuple(BudgetDetector().detect(context(event))) == ()

    def test_exceeding_a_token_ceiling_is_reported(self) -> None:
        event = llm_call(1, tokens=TokenUsage(input_tokens=600, output_tokens=600))

        signal = only(BudgetDetector().detect(context(event, token_budget=1000)))

        assert signal.category is FailureCategory.BUDGET

    def test_exceeding_a_cost_ceiling_is_reported(self) -> None:
        event = llm_call(1, cost_usd=0.25)

        signal = only(BudgetDetector().detect(context(event, cost_budget_usd=0.10)))

        assert "$0.2500" in signal.summary


class TestRegistry:
    def test_the_default_registry_meets_the_mvp_floor(self) -> None:
        """Design document section 18.1 requires at least eight deterministic detectors."""
        assert len(default_registry()) >= 8

    def test_every_default_detector_is_token_free(self) -> None:
        """The always-on path may never contain a detector that calls a model."""
        deterministic = {
            AnalysisLayer.L0_STRUCTURAL,
            AnalysisLayer.L1_BEHAVIORAL,
            AnalysisLayer.L2_GRAPH_IMPACT,
        }

        for detector in default_registry().detectors:
            assert detector.layer in deterministic, detector.name
            assert not detector.name.startswith("semantic:")

    def test_duplicate_detector_names_are_refused(self) -> None:
        registry = DetectorRegistry([ToolExecutionDetector()])

        with pytest.raises(ValueError, match="already registered"):
            registry.register(ToolExecutionDetector())

    def test_signals_are_ordered_so_the_earliest_problem_reads_first(self) -> None:
        events = [run_start(RUN), tool(1), tool(5, exit_code=1), tool(9, exit_code=2)]

        signals = default_registry().run(context(*events))
        failures = [s for s in signals if s.detector == "structural:tool_execution"]

        assert [signal.node_id for signal in failures] == ["evt_5", "evt_9"]

    def test_analysis_is_reproducible(self) -> None:
        """Two runs over the same trace must produce identical signals."""
        events = [
            run_start(RUN),
            tool(1, exit_code=1),
            *(tool(i, retry_of="evt_1") for i in range(2, 6)),
            run_end(6, RUN, outcome=RunOutcome.SUCCESS),
        ]

        first = default_registry().run(context(*events))
        second = default_registry().run(context(*events))

        assert first == second

    def test_an_empty_trace_produces_no_signals_rather_than_an_error(self) -> None:
        assert default_registry().run(context()) == ()

    def test_a_healthy_run_produces_no_signals(self) -> None:
        """False positives on ordinary work would train users to ignore the tool."""
        events = [
            run_start(RUN),
            tool_call(1, RUN),
            llm_call(2),
            run_end(3, RUN, outcome=RunOutcome.SUCCESS),
        ]

        assert default_registry().run(context(*events)) == ()
