"""Adapter toolkit tests.

The shell adapter is the first thing here that records runs nobody staged, so these
tests use real subprocesses. The contract checks matter most: an adapter that violates
them produces a trace the engine will analyse confidently and wrongly.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_adapter import (
    ContractViolationError,
    ListSink,
    RunRecorder,
    assert_adapter_contract,
    contains_secret,
    record_steps,
    scan,
    warn_about_state_keys,
)
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import RunEndEvent, RunOutcome, ToolCallEvent

runner = CliRunner()

PY = sys.executable


def ok(message: str = "fine") -> str:
    return f'{PY} -c "print({message!r})"'


def fails(code: int = 1) -> str:
    return f'{PY} -c "import sys; sys.exit({code})"'


class TestSecretScanner:
    @pytest.mark.parametrize(
        "text",
        [
            "export OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
            "token: ghp_abcdefghijklmnopqrstuvwxyz0123",
            "AWS key AKIAIOSFODNN7EXAMPLE here",
            "-----BEGIN RSA PRIVATE KEY-----",
            "Authorization: Bearer abcdefghijklmnopqrstuvwx",
            "api_key = supersecretvalue123",
        ],
    )
    def test_credential_shapes_are_found(self, text: str) -> None:
        assert contains_secret(text)

    def test_ordinary_output_is_not_flagged(self) -> None:
        """A scanner that fires on normal logs gets disabled, taking the real catches with it."""
        assert not contains_secret("running 42 tests, 3 failed in 1.2s")
        assert not contains_secret("commit a0ac5acbbab21951f4554e708155b49b4dbc58d6")

    def test_the_value_is_removed_not_just_reported(self) -> None:
        result = scan("key=ghp_abcdefghijklmnopqrstuvwxyz0123 rest")

        assert "ghp_" not in result.redacted
        assert "[REDACTED]" in result.redacted
        assert result.found


class TestRecorder:
    def test_sequences_are_contiguous_by_construction(self) -> None:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            for index in range(5):
                recorder.tool_call(f"tool_{index}")
            recorder.end_run(RunOutcome.SUCCESS)

        assert [event.sequence for event in sink.events] == list(range(7))

    def test_it_produces_a_contract_valid_trace(self) -> None:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("pytest", exit_code=1)
            recorder.end_run(RunOutcome.FAILURE)

        assert_adapter_contract(sink.events)

    def test_command_text_is_hashed_not_stored(self) -> None:
        """A command line carrying a token must not survive in the journal."""
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("curl", arguments="curl -H 'Authorization: Bearer abc123xyz789def'")

        serialized = str([event.model_dump() for event in sink.events])
        assert "abc123xyz789def" not in serialized

    def test_a_credential_in_output_sets_the_flag(self) -> None:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("env", output="GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123")

        flagged = [e for e in sink.events if e.security.contains_secret]
        assert len(flagged) == 1

    def test_ordinary_output_leaves_the_flag_clear(self) -> None:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("pytest", output="3 passed in 0.4s")

        assert not any(e.security.contains_secret for e in sink.events)

    def test_a_crash_still_closes_the_run(self) -> None:
        """An unfinished trace is diagnosable only if it says it is unfinished."""
        sink = ListSink()

        def crash() -> None:
            with RunRecorder("run_x", sink) as recorder:
                recorder.start_run(task="t", runtime="test")
                recorder.tool_call("build")
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            crash()

        closing = sink.events[-1]
        assert isinstance(closing, RunEndEvent)
        assert closing.run.outcome is RunOutcome.FAILURE

    def test_starting_twice_is_refused(self) -> None:
        recorder = RunRecorder("run_x", ListSink())
        recorder.start_run(task="t", runtime="test")

        with pytest.raises(RuntimeError, match="already started"):
            recorder.start_run(task="t", runtime="test")


class TestContract:
    def _valid(self) -> ListSink:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("pytest")
            recorder.end_run(RunOutcome.SUCCESS)
        return sink

    def test_an_empty_trace_is_rejected(self) -> None:
        with pytest.raises(ContractViolationError, match="no events"):
            assert_adapter_contract([])

    def test_a_missing_run_start_is_rejected(self) -> None:
        events = self._valid().events[1:]

        with pytest.raises(ContractViolationError, match="run_start"):
            assert_adapter_contract(events)

    def test_events_after_run_end_are_rejected(self) -> None:
        sink = self._valid()
        recorder = RunRecorder("run_x", sink)
        recorder._sequence = 9
        recorder.tool_call("late")

        with pytest.raises(ContractViolationError):
            assert_adapter_contract(sink.events)

    def test_a_mixed_run_is_rejected(self) -> None:
        sink = self._valid()
        other = ListSink()
        RunRecorder("run_y", other).start_run(task="t", runtime="test")
        combined = [*sink.events, *other.events]

        with pytest.raises(ContractViolationError, match="mixes runs"):
            assert_adapter_contract(combined, run_id="run_x")

    def test_a_naive_timestamp_is_rejected(self) -> None:
        sink = ListSink()
        naive = datetime(2026, 7, 30, 9, 0)  # noqa: DTZ001 - deliberately wrong
        with RunRecorder("run_x", sink, clock=lambda: naive.replace(tzinfo=UTC)) as recorder:
            recorder.start_run(task="t", runtime="test")
        broken = sink.events[0].model_copy(update={"timestamp": naive})

        with pytest.raises(ContractViolationError, match="naive timestamp"):
            assert_adapter_contract([broken])


class TestShellAdapterOnRealCommands:
    def test_it_records_a_real_exit_code(self) -> None:
        sink = ListSink()

        outcomes = record_steps([fails(3)], run_id="run_x", task="t", sink=sink)

        assert outcomes[0].exit_code == 3
        assert outcomes[0].failed

    def test_it_continues_past_a_failure(self) -> None:
        """Otherwise every recorded trace has its onset and symptom in the same place."""
        sink = ListSink()

        outcomes = record_steps([fails(), ok(), ok()], run_id="run_x", task="t", sink=sink)

        assert len(outcomes) == 3

    def test_it_can_be_told_to_stop(self) -> None:
        sink = ListSink()

        outcomes = record_steps(
            [fails(), ok()], run_id="run_x", task="t", sink=sink, stop_on_failure=True
        )

        assert len(outcomes) == 1

    def test_it_names_the_step_after_the_program(self) -> None:
        sink = ListSink()

        record_steps([ok()], run_id="run_x", task="t", sink=sink)

        tools = [e.tool.name for e in sink.events if isinstance(e, ToolCallEvent)]
        assert tools == [Path(PY).stem]

    def test_the_recorded_trace_satisfies_the_contract(self) -> None:
        sink = ListSink()

        record_steps([ok(), fails()], run_id="run_x", task="t", sink=sink)

        assert_adapter_contract(sink.events)

    def test_the_run_outcome_reflects_reality(self) -> None:
        sink = ListSink()

        record_steps([ok()], run_id="run_x", task="t", sink=sink)

        closing = sink.events[-1]
        assert isinstance(closing, RunEndEvent)
        assert closing.run.outcome is RunOutcome.SUCCESS

    def test_it_records_no_per_step_readout_as_state(self) -> None:
        """Regression: recording the exit code as state made every pipeline look broken.

        The value necessarily changes as steps succeed and fail, so the flapping
        detector fired on it and nominated the first step of every run as the onset —
        including steps that had plainly succeeded.
        """
        sink = ListSink()

        record_steps([ok(), fails(), ok(), fails()], run_id="run_x", task="t", sink=sink)

        assert all(event.state_delta == {} for event in sink.events)

    def test_a_real_pipeline_blames_the_first_failure_not_the_first_step(self) -> None:
        sink = ListSink()

        record_steps([ok(), fails(2), ok(), fails(1)], run_id="run_x", task="t", sink=sink)
        context = AnalysisContext.from_events("run_x", sink.events)
        bundle = diagnose(context)

        assert bundle.primary is not None
        assert bundle.primary.onset_node_id == "run_x_evt_0002"


class TestStateKeyGuidance:
    def test_a_per_step_readout_is_reported(self) -> None:
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            for index in range(5):
                recorder.tool_call(f"step_{index}", state={"last_exit_code": index % 2})

        warnings = warn_about_state_keys(sink.events)

        assert any("last_exit_code" in warning for warning in warnings)

    def test_a_genuine_belief_is_not_reported(self) -> None:
        """A fact only some steps touch is exactly what state_delta is for."""
        sink = ListSink()
        with RunRecorder("run_x", sink) as recorder:
            recorder.start_run(task="t", runtime="test")
            recorder.tool_call("migrate", state={"migrated": True})
            recorder.tool_call("build")
            recorder.tool_call("test")
            recorder.tool_call("deploy")

        assert warn_about_state_keys(sink.events) == ()


class TestRealRunEndToEnd:
    def test_a_real_failing_pipeline_is_diagnosed(self, tmp_path: Path) -> None:
        """The first trace in this repository that nobody wrote by hand."""
        with Collector.open(tmp_path / "store") as collector:
            record_steps(
                [ok("setup"), fails(2), ok("cleanup"), fails(1)],
                run_id="run_real",
                task="run the pipeline",
                sink=collector,
            )
            events = collector.events("run_real")

        context = AnalysisContext.from_events("run_real", events)
        bundle = diagnose(context)

        assert context.integrity.is_intact
        assert bundle.candidates
        assert bundle.primary is not None
        # The second step is where it first went wrong; the last is only where it showed.
        assert bundle.primary.onset_node_id == "run_real_evt_0002"


class TestRecordCommand:
    def test_it_records_and_points_at_the_diagnosis(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "record",
                "--store",
                str(tmp_path / "store"),
                "--run-id",
                "run_cli",
                "--task",
                "build",
                "-s",
                ok(),
                "-s",
                fails(),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "1 failed" in result.output
        assert "runopsy diagnose run_cli" in result.output

    def test_the_recorded_run_can_be_diagnosed(self, tmp_path: Path) -> None:
        store = str(tmp_path / "store")
        runner.invoke(app, ["record", "--store", store, "--run-id", "run_cli", "-s", fails()])

        result = runner.invoke(app, ["diagnose", "run_cli", "--store", store])

        assert result.exit_code == 0, result.output
        assert "Observed failure" in result.output

    def test_it_asks_for_a_step(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["record", "--store", str(tmp_path / "store")])

        assert result.exit_code == 2
        assert "--step" in result.output
