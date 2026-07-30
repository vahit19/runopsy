"""Baseline comparison tests.

The load-bearing assertion is that the engine beats reading a log bottom-up. If that
stops being true, the product has no reason to exist, so it is checked rather than
assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_bench import (
    BenchmarkReport,
    LastFailure,
    NoDiagnosis,
    RuleOnly,
    all_cases,
    all_strategies,
    compare_strategies,
    comparison_markdown,
    evaluate_case,
    run_benchmark,
)
from runopsy_cli.main import app

runner = CliRunner()


@pytest.fixture(scope="module")
def reports() -> tuple[BenchmarkReport, ...]:
    return compare_strategies()


def scored(name: str, reports: tuple[BenchmarkReport, ...]) -> BenchmarkReport:
    return next(report for report in reports if report.strategy_name == name)


class TestBaselines:
    def test_doing_nothing_scores_nothing(self) -> None:
        """The floor exists so the other numbers can be read."""
        report = run_benchmark(strategy=NoDiagnosis())

        assert report.top1_accuracy == 0.0
        assert report.false_positive_rate == 0.0

    def test_the_human_heuristic_is_measured_not_assumed(self) -> None:
        report = run_benchmark(strategy=LastFailure())

        assert 0.0 < report.top1_accuracy < 1.0

    def test_last_failure_is_fooled_by_the_late_symptom_case(self) -> None:
        """This is the failure mode the product exists to remove."""
        case = next(c for c in all_cases() if c.name == "early_failure_late_symptom")

        result = evaluate_case(case, strategy=LastFailure())

        assert result.predicted == 20
        assert result.case.onset_step == 4
        assert result.is_exact is False

    def test_the_engine_gets_that_case_right(self) -> None:
        case = next(c for c in all_cases() if c.name == "early_failure_late_symptom")

        assert evaluate_case(case, strategy=RuleOnly()).predicted == 4

    def test_no_baseline_flags_a_healthy_run(self) -> None:
        for strategy in all_strategies():
            report = run_benchmark(strategy=strategy)
            assert report.false_positive_rate == 0.0, strategy.name


class TestTheEngineEarnsItsPlace:
    def test_it_beats_reading_the_log_bottom_up(self, reports: tuple[BenchmarkReport, ...]) -> None:
        """Free and instant is the bar. Clearing it is the product's whole premise."""
        engine = scored("rule_only", reports)
        human = scored("last_failure", reports)

        assert engine.top1_accuracy > human.top1_accuracy * 2
        assert engine.mean_step_distance < human.mean_step_distance

    def test_it_beats_the_bare_earliest_failure_intuition(
        self, reports: tuple[BenchmarkReport, ...]
    ) -> None:
        """Otherwise the ranking adds nothing over a one-line heuristic."""
        engine = scored("rule_only", reports)
        naive = scored("first_failure", reports)

        assert engine.top1_accuracy > naive.top1_accuracy

    def test_the_engine_is_reported_last(self, reports: tuple[BenchmarkReport, ...]) -> None:
        assert reports[-1].strategy_name == "rule_only"


class TestReportDocument:
    def test_it_names_every_strategy(self, reports: tuple[BenchmarkReport, ...]) -> None:
        document = comparison_markdown(reports)

        for strategy in all_strategies():
            assert f"`{strategy.name}`" in document

    def test_it_carries_no_timestamp(self, reports: tuple[BenchmarkReport, ...]) -> None:
        """A stamped report changes on every run, hiding real movement in the numbers."""
        document = comparison_markdown(reports)

        assert "2026" not in document
        assert comparison_markdown(reports) == document

    def test_it_states_the_limits_of_the_claim(self, reports: tuple[BenchmarkReport, ...]) -> None:
        """Synthetic accuracy is not evidence of helping on real work."""
        document = comparison_markdown(reports)

        assert "do not establish" in document
        assert "time-to-diagnosis" in document

    def test_it_lists_the_blind_spot(self, reports: tuple[BenchmarkReport, ...]) -> None:
        document = comparison_markdown(reports)

        assert "blind spot" in document
        assert "silent_wrong_config" in document

    def test_an_empty_comparison_does_not_crash(self) -> None:
        assert "No strategies" in comparison_markdown(())


class TestBenchCommand:
    def test_compare_prints_every_strategy(self) -> None:
        result = runner.invoke(app, ["bench", "--compare"])

        assert result.exit_code == 0, result.output
        assert "last_failure" in result.output
        assert "rule_only" in result.output

    def test_write_produces_a_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "nested" / "report.md"

        result = runner.invoke(app, ["bench", "--write", str(destination)])

        assert result.exit_code == 0, result.output
        assert destination.read_text(encoding="utf-8").startswith("# Runopsy benchmark")

    def test_the_written_report_is_byte_identical_across_runs(self, tmp_path: Path) -> None:
        first = tmp_path / "a.md"
        second = tmp_path / "b.md"

        runner.invoke(app, ["bench", "--write", str(first)])
        runner.invoke(app, ["bench", "--write", str(second)])

        assert first.read_bytes() == second.read_bytes()
