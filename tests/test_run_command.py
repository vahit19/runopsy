"""`runopsy run` — drive an agent, then diagnose what it did.

The design lists this and it was the last command missing. It had been deferred as "a
decision about how to drive a runtime without forking it", which turned out to be an
overcomplication: the mechanism is the runtime's own documented command line, plus
`RUNOPSY_HOME` so the hooks the user already approved write where this command looks.
No Hermes module is imported and no config of theirs is rewritten.

The tests that matter here are about the ways this fails. `record` runs the steps
itself and therefore knows them; `run` starts an agent that decides its own steps and
learns what happened only through hooks — so "the command succeeded and recorded
nothing" is a real state, and it must never be reported as success.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import run_end, run_start
from runopsy_adapter import launch as launcher
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core.schema import Event, RunOutcome

runner = CliRunner()
RUN = "recorded_by_agent"


class TestTheCommandItBuilds:
    def test_it_asks_for_one_task_non_interactively(self) -> None:
        command = launcher.build_command("hermes", "fix the tests")

        assert command[0] == "hermes"
        assert "--cli" in command
        assert command[command.index("-z") + 1] == "fix the tests"

    def test_hooks_are_accepted_because_nobody_can_answer_a_prompt(self) -> None:
        assert "--accept-hooks" in launcher.build_command("hermes", "x")

    def test_that_can_be_withheld(self) -> None:
        """It stays a flag rather than an assumption baked into the launcher."""
        assert "--accept-hooks" not in launcher.build_command("hermes", "x", accept_hooks=False)

    def test_model_and_provider_are_passed_through(self) -> None:
        command = launcher.build_command("hermes", "x", model="m", provider="p")

        assert command[command.index("-m") + 1] == "m"
        assert command[command.index("--provider") + 1] == "p"

    def test_a_prompt_with_shell_metacharacters_stays_one_argument(self) -> None:
        """No shell is involved, so this is an argument rather than something to run."""
        command = launcher.build_command("hermes", "rm -rf / ; echo $(whoami)")

        assert "rm -rf / ; echo $(whoami)" in command


class TestTheStoreIsPassedThroughTheEnvironment:
    def test_runopsy_home_is_set_for_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rather than rewriting the user's hook config, which would outlive the command."""
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr("runopsy_adapter.launch.subprocess.run", fake_run)

        launcher.launch("task", store=tmp_path / "store", executable="hermes")

        assert seen["env"]["RUNOPSY_HOME"] == str((tmp_path / "store").resolve())

    def test_a_missing_runtime_is_an_error_not_a_silent_skip(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            launcher.launch("task", store=tmp_path, executable=None)


class TestWhatTheCommandReports:
    def test_it_explains_how_to_install_a_missing_runtime(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(launcher, "find_executable", lambda name="hermes": None)

        result = runner.invoke(app, ["run", "do a thing", "--store", str(tmp_path)])

        assert result.exit_code == 2
        assert "hermes-agent" in result.output

    def test_recording_nothing_is_a_failure_however_the_agent_exited(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failure this command exists to catch: a session that looks entirely
        normal and produced no trace."""
        monkeypatch.setattr(launcher, "find_executable", lambda name="hermes": "hermes")
        monkeypatch.setattr(
            launcher,
            "launch",
            lambda *a, **k: launcher.LaunchResult("hermes", 0, None, 0),
        )

        result = runner.invoke(app, ["run", "do a thing", "--store", str(tmp_path)])

        assert result.exit_code == 1
        assert "nothing was recorded" in result.output

    def test_a_recorded_run_is_diagnosed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = tmp_path / "store"
        events: list[Event] = [
            run_start(RUN, task="fix the tests"),
            run_end(1, RUN, outcome=RunOutcome.FAILURE),
        ]

        def fake_launch(*args: Any, **kwargs: Any) -> launcher.LaunchResult:
            with Collector.open(store) as collector:
                collector.record_all(events)
            return launcher.LaunchResult("hermes", 0, None, 0)

        monkeypatch.setattr(launcher, "find_executable", lambda name="hermes": "hermes")
        monkeypatch.setattr(launcher, "launch", fake_launch)

        result = runner.invoke(app, ["run", "fix the tests", "--store", str(store)])

        assert result.exit_code == 0, result.output
        assert RUN in result.output

    def test_diagnosis_can_be_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = tmp_path / "store"

        def fake_launch(*args: Any, **kwargs: Any) -> launcher.LaunchResult:
            with Collector.open(store) as collector:
                collector.record_all([run_start(RUN), run_end(1, RUN)])
            return launcher.LaunchResult("hermes", 0, None, 0)

        monkeypatch.setattr(launcher, "find_executable", lambda name="hermes": "hermes")
        monkeypatch.setattr(launcher, "launch", fake_launch)

        result = runner.invoke(app, ["run", "x", "--store", str(store), "--no-diagnose"])

        assert result.exit_code == 0, result.output
        assert "Suspected onset" not in result.output

    def test_an_unknown_runtime_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["run", "x", "--store", str(tmp_path), "--runtime", "autogen"])

        assert result.exit_code == 2


class TestOutputSurvivesANarrowConsole:
    def test_unencodable_output_does_not_crash_the_command(self) -> None:
        """`runopsy run` once recorded 46 events and then died printing an em dash.

        A Windows console on a legacy code page raises rather than substituting, so the
        work was done and the answer lost. Streams are reconfigured to replace instead.
        """
        import io
        import sys

        from runopsy_cli.main import _tolerate_narrow_encodings

        narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        original = sys.stdout
        try:
            sys.stdout = narrow
            _tolerate_narrow_encodings()
            print("step 1 — done ◆")  # em dash and a diamond
        finally:
            sys.stdout = original

        assert narrow.errors == "replace"

    def test_it_survives_a_stream_that_cannot_be_reconfigured(self) -> None:
        """Under pytest's capture, stdout may not support reconfigure at all."""
        import io
        import sys

        from runopsy_cli.main import _tolerate_narrow_encodings

        original = sys.stdout
        try:
            sys.stdout = io.StringIO()
            _tolerate_narrow_encodings()
        finally:
            sys.stdout = original
