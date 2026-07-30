"""CLI tests.

The wording assertions here are not style checks. A diagnosis tool is only worth having
if its confident statements can be trusted, so the tests treat "claimed a cause without
validating it" as a defect of the same rank as a crash.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_cli.language import asserts_causation
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core.schema import Event, RunOutcome, StateChange, ToolCallEvent, ToolPayload

RUN = "run_0042"
runner = CliRunner()


def tool(sequence: int, *, name: str = "terminal", exit_code: int = 0) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
        state_delta={"endpoint": StateChange(after="staging")} if sequence == 9 else {},
    )


def failing_run() -> list[Event]:
    return [
        run_start(RUN, task="fix the failing integration test"),
        tool(9, name="write_config", exit_code=1),
        tool(10, name="edit_file"),
        tool(12, name="restart_service"),
        tool(14, name="pytest", exit_code=1),
        run_end(15, RUN, outcome=RunOutcome.FAILURE),
    ]


def healthy_run() -> list[Event]:
    return [
        run_start(RUN, task="add a changelog entry"),
        tool(1, name="edit_file"),
        run_end(2, RUN, outcome=RunOutcome.SUCCESS),
    ]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    with Collector.open(root) as collector:
        collector.record_all(failing_run())
    return root


@pytest.fixture
def healthy_store(tmp_path: Path) -> Path:
    root = tmp_path / "healthy"
    with Collector.open(root) as collector:
        collector.record_all(healthy_run())
    return root


def invoke(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


class TestDiagnose:
    def test_it_separates_the_symptom_from_the_suspicion(self, store: Path) -> None:
        output = invoke("diagnose", RUN, "--store", str(store))

        assert "Observed failure" in output
        assert "Suspected onset" in output

    def test_it_points_at_the_earlier_step_not_the_late_one(self, store: Path) -> None:
        output = invoke("diagnose", RUN, "--store", str(store))
        onset_line = output.split("Suspected onset", 1)[1]

        assert "step 9" in onset_line

    def test_it_never_claims_a_cause_it_has_not_validated(self, store: Path) -> None:
        """The single wording rule the product cannot break."""
        output = invoke("diagnose", RUN, "--store", str(store))

        assert not asserts_causation(output), output

    def test_it_says_confidence_is_unverified(self, store: Path) -> None:
        output = invoke("diagnose", RUN, "--store", str(store))

        assert "unverified" in output

    def test_it_tells_the_user_how_to_confirm_the_finding(self, store: Path) -> None:
        """Making certainty cheap matters more than sounding certain."""
        output = invoke("diagnose", RUN, "--store", str(store))

        assert "runopsy replay" in output

    def test_a_healthy_run_is_reported_as_clean(self, healthy_store: Path) -> None:
        output = invoke("diagnose", RUN, "--store", str(healthy_store))

        assert "Nothing detectable went wrong" in output
        assert "Suspected onset" not in output

    def test_latest_resolves_without_naming_a_run(self, store: Path) -> None:
        output = invoke("diagnose", "--store", str(store))

        assert RUN in output

    def test_an_unknown_run_fails_with_a_usable_message(self, store: Path) -> None:
        result = runner.invoke(app, ["diagnose", "run_nope", "--store", str(store)])

        assert result.exit_code == 2
        assert "No events recorded" in result.output

    def test_an_empty_store_explains_itself(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["diagnose", "--store", str(tmp_path / "empty")])

        assert result.exit_code == 2
        assert "No runs recorded" in result.output


class TestJsonOutput:
    def test_the_bundle_is_machine_readable(self, store: Path) -> None:
        payload = json.loads(invoke("diagnose", RUN, "--store", str(store), "--json"))

        assert payload["run_id"] == RUN
        assert payload["candidates"]

    def test_each_candidate_carries_evidence_confidence_and_affected_nodes(
        self, store: Path
    ) -> None:
        """Design document section 18.1 requires exactly these on every candidate."""
        payload = json.loads(invoke("diagnose", RUN, "--store", str(store), "--json"))

        for candidate in payload["candidates"]:
            assert "confidence" in candidate
            assert "affected_node_ids" in candidate
            assert "signal_ids" in candidate
            assert candidate["status"] in {
                "observed_failure",
                "suspected_onset",
                "correlated_cause",
            }

    def test_no_candidate_is_serialized_as_validated(self, store: Path) -> None:
        payload = json.loads(invoke("diagnose", RUN, "--store", str(store), "--json"))

        for candidate in payload["candidates"]:
            assert candidate["replay_run_id"] is None
            assert candidate["verified_by"] is None


class TestExitCodes:
    def test_findings_do_not_fail_the_command_by_default(self, store: Path) -> None:
        result = runner.invoke(app, ["diagnose", RUN, "--store", str(store)])

        assert result.exit_code == 0

    def test_ci_can_opt_into_failing_on_a_finding(self, store: Path) -> None:
        result = runner.invoke(app, ["diagnose", RUN, "--store", str(store), "--fail-on-finding"])

        assert result.exit_code == 1

    def test_a_clean_run_passes_even_in_ci_mode(self, healthy_store: Path) -> None:
        result = runner.invoke(
            app, ["diagnose", RUN, "--store", str(healthy_store), "--fail-on-finding"]
        )

        assert result.exit_code == 0


class TestEvidence:
    def test_it_shows_the_signals_behind_a_step(self, store: Path) -> None:
        output = invoke("evidence", RUN, "--step", "9", "--store", str(store))

        assert "step 9" in output
        assert "structural:tool_execution" in output

    def test_it_explains_why_the_step_ranked_where_it_did(self, store: Path) -> None:
        output = invoke("evidence", RUN, "--step", "9", "--store", str(store))

        assert "Why it ranked here" in output
        assert "precedence" in output

    def test_it_lists_what_the_step_may_have_affected(self, store: Path) -> None:
        output = invoke("evidence", RUN, "--step", "9", "--store", str(store))

        assert "May have affected" in output

    def test_it_does_not_claim_causation_either(self, store: Path) -> None:
        output = invoke("evidence", RUN, "--step", "9", "--store", str(store))

        assert not asserts_causation(output), output

    def test_a_step_without_findings_says_so(self, store: Path) -> None:
        output = invoke("evidence", RUN, "--step", "10", "--store", str(store))

        assert "No failure signal" in output

    def test_a_missing_step_fails_with_a_usable_message(self, store: Path) -> None:
        result = runner.invoke(app, ["evidence", RUN, "--step", "999", "--store", str(store)])

        assert result.exit_code == 2
        assert "no step 999" in result.output.lower()

    def test_omitting_the_step_explains_what_to_do(self, store: Path) -> None:
        result = runner.invoke(app, ["evidence", RUN, "--store", str(store)])

        assert result.exit_code == 2
        assert "--step" in result.output


class TestRunsAndDoctor:
    def test_runs_lists_recorded_runs(self, store: Path) -> None:
        output = invoke("runs", "--store", str(store))

        assert RUN in output
        assert "failure" in output

    def test_runs_is_not_an_error_when_there_is_nothing(self, tmp_path: Path) -> None:
        output = invoke("runs", "--store", str(tmp_path / "empty"))

        assert "No runs recorded yet" in output

    def test_doctor_reports_configuration(self, store: Path) -> None:
        output = invoke("doctor", "--store", str(store))

        assert "detectors" in output
        assert "deterministic" in output

    def test_doctor_never_prints_a_key(self, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A key printed to a terminal is a key in scrollback, screenshots and logs."""
        secret = "sk-or-v1-averysecretvalue"
        monkeypatch.setenv("OPENROUTER_API_KEY", secret)

        output = invoke("doctor", "--store", str(store))

        assert secret not in output
        assert "set in environment" in output

    def test_doctor_says_offline_use_needs_no_key(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        output = invoke("doctor", "--store", str(store))

        assert "not set" in output
        assert "no provider key" in output
