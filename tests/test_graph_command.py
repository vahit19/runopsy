"""`runopsy graph` and `runopsy adapter hermes status`.

Both exist in section 14 of the design document and had never been built. They are
grouped here because they answer the same kind of question — *what is actually going
on* — one about a recorded run, one about whether recording is happening at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_adapter.hermes import RECORDED_EVENTS, adapter_status, hooks_config_block
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core.schema import CallStatus, Event, RunOutcome, ToolCallEvent, ToolPayload

runner = CliRunner()
RUN = "run_graph"


def call(sequence: int, *, name: str, exit_code: int = 0) -> ToolCallEvent:
    from datetime import timedelta

    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(
            name=name,
            arguments_hash="sha256:" + f"{sequence:064d}",
            exit_code=exit_code,
            status=CallStatus.ERROR if exit_code else CallStatus.OK,
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    events: list[Event] = [
        run_start(RUN, task="ship it"),
        call(1, name="write_config", exit_code=1),
        call(2, name="compile"),
        call(3, name="pytest", exit_code=1),
        run_end(4, RUN, outcome=RunOutcome.FAILURE),
    ]
    root = tmp_path / "store"
    with Collector.open(root) as collector:
        collector.record_all(events)
    return root


class TestTheTerminalGraph:
    def test_every_step_appears_in_order(self, store: Path) -> None:
        result = runner.invoke(app, ["graph", RUN, "--store", str(store)])

        assert result.exit_code == 0, result.output
        positions = [result.output.index(name) for name in ("write_config", "compile", "pytest")]
        assert positions == sorted(positions)

    def test_the_onset_and_the_failure_are_distinguishable(self, store: Path) -> None:
        result = runner.invoke(app, ["graph", RUN, "--store", str(store)])

        assert "suspected onset" in result.output
        assert "observed failure" in result.output

    def test_propagation_is_labelled_as_inference(self, store: Path) -> None:
        """The one part of this picture that is not recorded fact."""
        result = runner.invoke(app, ["graph", RUN, "--store", str(store)])

        assert "May reach" in result.output
        assert "inferred, not observed" in result.output

    def test_it_is_pure_ascii(self, store: Path) -> None:
        """A Windows console on a legacy code page raises on box-drawing characters.

        This view crashed with UnicodeEncodeError the first time it was run for real.
        """
        result = runner.invoke(app, ["graph", RUN, "--store", str(store)])

        result.output.encode("cp1252")


class TestTheDotExport:
    def test_it_emits_a_digraph(self, store: Path) -> None:
        result = runner.invoke(app, ["graph", RUN, "--store", str(store), "--format", "dot"])

        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("digraph runopsy {")
        assert result.output.strip().endswith("}")

    def test_inferred_edges_are_dashed_and_carry_their_confidence(self, store: Path) -> None:
        result = runner.invoke(app, ["graph", RUN, "--store", str(store), "--format", "dot"])

        assert "style=dashed" in result.output
        assert "may reach" in result.output

    def test_a_windows_path_label_does_not_break_the_file(self, tmp_path: Path) -> None:
        r"""A raw backslash turns \U into a Graphviz escape and the file will not parse."""
        root = tmp_path / "winstore"
        with Collector.open(root) as collector:
            collector.record_all(
                [
                    run_start(RUN, task=r"C:\Users\someone\AppData\project"),
                    call(1, name="build", exit_code=1),
                    run_end(2, RUN, outcome=RunOutcome.FAILURE),
                ]
            )

        result = runner.invoke(app, ["graph", RUN, "--store", str(root), "--format", "dot"])

        assert r"\\Users" in result.output
        assert r"\Users" not in result.output.replace(r"\\Users", "")

    def test_an_unknown_format_is_refused(self, store: Path) -> None:
        result = runner.invoke(app, ["graph", RUN, "--store", str(store), "--format", "svg"])

        assert result.exit_code == 2


class TestAdapterStatus:
    """The failures this reports are all silent ones.

    A malformed hook block makes Hermes discard its whole config and run with defaults;
    a hook registered for a plugin-only event never fires. Both look like a normal
    session that recorded nothing, which is how the first integration attempt was lost.
    """

    def write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_correct_config_reports_every_event_wired(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, hooks_config_block("runopsy hook"))

        status = adapter_status(path)

        assert status.is_wired
        assert set(status.configured) == set(RECORDED_EVENTS)
        assert not status.never_fires

    def test_a_missing_config_is_reported_rather_than_guessed_at(self, tmp_path: Path) -> None:
        status = adapter_status(tmp_path / "absent.yaml")

        assert status.config_path is None
        assert not status.is_wired

    def test_unparseable_yaml_is_named_as_such(self, tmp_path: Path) -> None:
        """The exact mistake that cost the first live session: a half-quoted command."""
        path = self.write(
            tmp_path,
            'hooks:\n  post_tool_call:\n    - command: "C:/x y/runopsy" hook post_tool_call\n',
        )

        status = adapter_status(path)

        assert status.parse_error
        assert not status.is_wired

    def test_a_plugin_only_event_is_flagged_as_never_firing(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            "hooks:\n  post_llm_call:\n    - command: runopsy hook post_llm_call\n",
        )

        status = adapter_status(path)

        assert "post_llm_call" in status.never_fires

    def test_hooks_belonging_to_another_tool_are_not_counted(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            "hooks:\n  post_tool_call:\n    - command: some-other-tool record\n",
        )

        status = adapter_status(path)

        assert status.configured == ()
        assert "post_tool_call" in status.missing

    def test_the_generated_block_is_what_the_checker_accepts(self) -> None:
        """The two must agree, or the tool contradicts its own instructions."""
        parsed = yaml.safe_load(hooks_config_block("runopsy hook"))

        assert set(parsed["hooks"]) == set(RECORDED_EVENTS)

    def test_the_command_reports_without_a_hermes_install(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["adapter", "hermes", "status", "--store", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Hermes config" in result.output

    def test_an_unknown_action_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["adapter", "hermes", "explode", "--store", str(tmp_path)])

        assert result.exit_code == 2
