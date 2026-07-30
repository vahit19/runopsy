"""Benchmark tests.

The thresholds here are regression guards, not certificates of quality. They sit below
measured performance so a real drop fails the build, while leaving room for the numbers
to move as the suite grows. Pinning them to today's exact score would turn every
honest experiment into a red build.
"""

from __future__ import annotations

import pytest

from runopsy_bench import BenchmarkReport, all_cases, evaluate_case, run_benchmark
from runopsy_bench.cases import SyntheticCase

# Measured at the time of writing: top-1 94.4%, top-3 100%, step distance 0.11.
MIN_TOP1_ACCURACY = 0.85
MIN_TOP3_RECALL = 0.95
MAX_MEAN_STEP_DISTANCE = 1.0


@pytest.fixture(scope="module")
def report() -> BenchmarkReport:
    return run_benchmark()


class TestSuiteShape:
    def test_the_suite_has_twenty_labelled_cases(self) -> None:
        assert len(all_cases()) == 20

    def test_case_names_are_unique(self) -> None:
        names = [case.name for case in all_cases()]

        assert len(names) == len(set(names))

    def test_it_contains_a_healthy_negative_control(self) -> None:
        """Without one, an engine that flags everything would score perfectly."""
        assert any(case.is_healthy for case in all_cases())

    def test_it_contains_a_labelled_blind_spot(self) -> None:
        """A suite of only solvable problems measures nothing."""
        assert any(not case.deterministically_detectable for case in all_cases())

    def test_every_failing_case_declares_where_it_broke(self) -> None:
        for case in all_cases():
            if case.is_healthy:
                continue
            steps = {event.sequence for event in case.events}
            assert case.onset_step in steps, case.name


class TestScores:
    def test_onset_top1_accuracy_holds(self, report: BenchmarkReport) -> None:
        assert report.top1_accuracy >= MIN_TOP1_ACCURACY

    def test_onset_top3_recall_holds(self, report: BenchmarkReport) -> None:
        assert report.top3_recall >= MIN_TOP3_RECALL

    def test_mean_step_distance_stays_small(self, report: BenchmarkReport) -> None:
        assert report.mean_step_distance <= MAX_MEAN_STEP_DISTANCE

    def test_a_healthy_run_is_never_flagged(self, report: BenchmarkReport) -> None:
        """Any false positive is a defect, not a tuning parameter.

        Spurious findings are what get a diagnosis tool switched off, so this threshold
        is exact rather than approximate.
        """
        assert report.false_positive_rate == 0.0

    def test_something_is_nominated_for_every_detectable_failure(
        self, report: BenchmarkReport
    ) -> None:
        assert report.localized == 1.0


class TestBlindSpots:
    def test_a_blind_spot_is_excluded_from_the_accuracy_figure(
        self, report: BenchmarkReport
    ) -> None:
        """Scoring an unreachable case would measure luck and inflate the headline."""
        scored_names = {result.case.name for result in report.scored}

        assert "silent_wrong_config" not in scored_names

    def test_a_blind_spot_is_still_reported(self, report: BenchmarkReport) -> None:
        blind = {result.case.name for result in report.blind_spots}

        assert "silent_wrong_config" in blind


class TestReproducibility:
    def test_the_suite_scores_identically_twice(self) -> None:
        assert run_benchmark().top1_accuracy == run_benchmark().top1_accuracy

    @pytest.mark.parametrize("case", all_cases(), ids=lambda case: case.name)
    def test_every_case_evaluates_without_error(self, case: SyntheticCase) -> None:
        result = evaluate_case(case)

        assert result.case.name == case.name

    def test_the_flagship_case_localizes_exactly(self) -> None:
        """The run breaks at step 4 and nothing visible happens until step 20."""
        case = next(c for c in all_cases() if c.name == "early_failure_late_symptom")

        result = evaluate_case(case)

        assert result.predicted == 4
        assert result.step_distance == 0

    def test_the_healthy_case_produces_nothing(self) -> None:
        case = next(c for c in all_cases() if c.is_healthy)

        result = evaluate_case(case)

        assert result.predicted_steps == ()
        assert result.is_false_positive is False
