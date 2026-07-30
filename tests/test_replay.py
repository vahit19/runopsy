"""Replay planning tests.

The gate tested here is the only thing standing between a diagnosis and a second
outgoing email. Its failure mode is silent, so the tests are written to catch a
classifier that has quietly become permissive.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core import AnalysisContext
from runopsy_core.schema import (
    CheckpointEvent,
    CheckpointPayload,
    Event,
    ReplayLevel,
    RunOutcome,
    ToolCallEvent,
    ToolPayload,
)
from runopsy_replay import Intervention, SideEffect, StepAction, build_plan, classify

RUN = "run_0042"
runner = CliRunner()


def tool(sequence: int, name: str = "terminal", *, exit_code: int = 0) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
    )


def checkpoint(sequence: int) -> CheckpointEvent:
    return CheckpointEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        checkpoint=CheckpointPayload(checkpoint_id=f"ck_{sequence}"),
    )


def context(*events: Event) -> AnalysisContext:
    return AnalysisContext.from_events(RUN, events)


class TestClassification:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("read_file", SideEffect.READ_ONLY),
            ("pytest", SideEffect.READ_ONLY),
            ("grep", SideEffect.READ_ONLY),
            ("edit_file", SideEffect.LOCAL_WRITE),
            ("git_commit", SideEffect.LOCAL_WRITE),
            ("send_email", SideEffect.EXTERNAL),
            ("deploy_service", SideEffect.EXTERNAL),
            ("charge_card", SideEffect.EXTERNAL),
            ("curl", SideEffect.EXTERNAL),
            ("rm_rf", SideEffect.DESTRUCTIVE),
            ("drop_table", SideEffect.DESTRUCTIVE),
            ("force_push", SideEffect.DESTRUCTIVE),
        ],
    )
    def test_known_tools_are_classified(self, name: str, expected: SideEffect) -> None:
        assert classify(name) == expected

    def test_an_unknown_tool_is_never_assumed_safe(self) -> None:
        """The classifier only sees a name, so it must fail closed."""
        assert classify("frobnicate_widget") is SideEffect.UNKNOWN

    def test_the_more_dangerous_reading_wins(self) -> None:
        """A name matching both a write and a removal must resolve to the removal."""
        assert classify("delete_and_write_config") is SideEffect.DESTRUCTIVE

    def test_separator_style_does_not_change_the_verdict(self) -> None:
        assert classify("send-email") is classify("send.email") is classify("send_email")

    def test_camel_case_classifies_like_snake_case(self) -> None:
        """Adapters name tools in whatever style their ecosystem prefers."""
        assert classify("sendEmail") is SideEffect.EXTERNAL
        assert classify("dropTable") is SideEffect.DESTRUCTIVE


class TestPlanning:
    def test_steps_before_the_replay_point_are_skipped(self) -> None:
        plan = build_plan(context(tool(1), tool(5), tool(9)), 5)

        skipped = [step.sequence for step in plan.steps if step.action is StepAction.SKIP]

        assert skipped == [1]

    def test_read_only_steps_replay_directly(self) -> None:
        plan = build_plan(context(tool(5, "pytest")), 5)

        assert plan.steps[0].action is StepAction.REPLAY

    def test_file_writes_are_confined_to_a_sandbox(self) -> None:
        plan = build_plan(context(tool(5, "edit_file")), 5)

        assert plan.steps[0].action is StepAction.SANDBOX

    def test_external_calls_are_excluded_from_replay(self) -> None:
        plan = build_plan(context(tool(5, "send_email")), 5)

        assert plan.steps[0].action is StepAction.BLOCK
        assert plan.blocked

    def test_destructive_calls_are_excluded_from_replay(self) -> None:
        plan = build_plan(context(tool(5, "rm")), 5)

        assert plan.steps[0].action is StepAction.BLOCK

    def test_unknown_tools_stop_for_a_human(self) -> None:
        plan = build_plan(context(tool(5, "frobnicate")), 5)

        assert plan.steps[0].action is StepAction.APPROVE
        assert plan.requires_human_decision is True

    def test_a_plan_is_always_a_plan(self) -> None:
        """The distinction between proposing and doing lives in the data, not the docs."""
        plan = build_plan(context(tool(5, "pytest")), 5)

        assert plan.is_dry_run is True

    def test_the_original_run_is_named_as_the_parent(self) -> None:
        plan = build_plan(context(tool(5)), 5)

        assert plan.parent_run_id == RUN
        assert plan.level is ReplayLevel.R2_SESSION_FORK


class TestCheckpointAnchoring:
    def test_the_nearest_earlier_checkpoint_is_chosen(self) -> None:
        plan = build_plan(context(checkpoint(2), checkpoint(6), tool(9)), 9)

        assert plan.checkpoint_sequence == 6

    def test_a_later_checkpoint_is_not_used(self) -> None:
        """Restoring from ahead of the replay point would undo the very step under test."""
        plan = build_plan(context(checkpoint(2), checkpoint(12), tool(9)), 9)

        assert plan.checkpoint_sequence == 2

    def test_a_missing_checkpoint_is_stated_not_hidden(self) -> None:
        plan = build_plan(context(tool(9)), 9)

        assert plan.checkpoint_id is None
        assert any("No checkpoint" in warning for warning in plan.warnings)

    def test_a_mismatched_checkpoint_is_warned_about(self) -> None:
        plan = build_plan(context(checkpoint(2), tool(9)), 9)

        assert any("not step 9" in warning for warning in plan.warnings)


class TestScientificHonesty:
    def test_changing_one_variable_is_controlled(self) -> None:
        plan = build_plan(context(tool(5)), 5, intervention=Intervention(model="local:qwen"))

        assert plan.intervention.is_controlled is True
        assert not any("more than one variable" in w.lower() for w in plan.warnings)

    def test_changing_several_variables_at_once_is_flagged(self) -> None:
        """If the outcome changes, a multi-variable replay cannot say which change did it."""
        plan = build_plan(
            context(tool(5)),
            5,
            intervention=Intervention(model="gpt", prompt_note="rewritten", tool_policy="strict"),
        )

        assert plan.intervention.is_controlled is False
        assert any("more than one variable" in w.lower() for w in plan.warnings)

    def test_a_broken_trace_is_declared(self) -> None:
        plan = build_plan(context(tool(1), tool(3), tool(5)), 3)

        assert any("not intact" in warning for warning in plan.warnings)


class TestReplayCommand:
    @pytest.fixture
    def store(self, tmp_path: Path) -> Path:
        root = tmp_path / "store"
        events = [
            run_start(RUN, task="fix the deploy"),
            checkpoint(3),
            tool(5, "write_config", exit_code=1),
            tool(7, "deploy_service"),
            tool(9, "pytest", exit_code=1),
            run_end(10, RUN, outcome=RunOutcome.FAILURE),
        ]
        with Collector.open(root) as collector:
            collector.record_all(events)
        return root

    def test_it_prints_a_plan(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--from-step", "5", "--store", str(store)])

        assert result.exit_code == 0, result.output
        assert "Replay plan" in result.output

    def test_it_states_that_nothing_ran(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--from-step", "5", "--store", str(store)])

        assert "Nothing was executed" in result.output

    def test_it_excludes_the_deploy(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--from-step", "5", "--store", str(store)])

        assert "blocked" in result.output
        assert "excluded" in result.output

    def test_it_uses_the_earlier_checkpoint(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--from-step", "5", "--store", str(store)])

        assert "checkpoint at step 3" in result.output

    def test_omitting_the_step_explains_what_to_do(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--store", str(store)])

        assert result.exit_code == 2
        assert "--from-step" in result.output

    def test_an_unknown_step_fails_with_a_usable_message(self, store: Path) -> None:
        result = runner.invoke(app, ["replay", RUN, "--from-step", "999", "--store", str(store)])

        assert result.exit_code == 2
        assert "no step 999" in result.output.lower()

    def test_asking_to_execute_says_it_is_not_available(self, store: Path) -> None:
        """Better to refuse clearly than to imply a capability that is not there."""
        result = runner.invoke(
            app, ["replay", RUN, "--from-step", "5", "--store", str(store), "--no-dry-run"]
        )

        assert "does not run them" in result.output
