"""Retention tests.

This is the only code in Runopsy that destroys evidence, so the tests are weighted
towards what it refuses to delete rather than what it deletes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_adapter import RunRecorder
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    Event,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    ToolCallEvent,
    ToolPayload,
)

runner = CliRunner()
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def run_events(run_id: str, *, started: datetime, command: str = "make") -> list[Event]:
    return [
        RunStartEvent(
            event_id=f"{run_id}_0",
            run_id=run_id,
            sequence=0,
            timestamp=started,
            run=RunPayload(task=f"task for {run_id}", runtime="test"),
        ),
        ToolCallEvent(
            event_id=f"{run_id}_1",
            run_id=run_id,
            sequence=1,
            timestamp=started + timedelta(seconds=1),
            tool=ToolPayload(name="make", arguments_hash=hash_text(command)),
        ),
        RunEndEvent(
            event_id=f"{run_id}_2",
            run_id=run_id,
            sequence=2,
            timestamp=started + timedelta(seconds=2),
            run=RunPayload(outcome=RunOutcome.SUCCESS),
        ),
    ]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """One run from 30 days ago, one from today, and a shared vault payload."""
    root = tmp_path / "store"
    with Collector.open(root) as collector:
        collector.record_all(run_events("old", started=NOW - timedelta(days=30)))
        collector.record_all(run_events("recent", started=NOW - timedelta(hours=1)))
        collector.vault.put("make")
    return root


class TestPlanning:
    def test_an_old_run_expires(self, store: Path) -> None:
        with Collector.open(store) as collector:
            plan = collector.plan_prune(7, now=NOW)

        assert [run.run_id for run in plan.expiring] == ["old"]

    def test_a_recent_run_is_kept(self, store: Path) -> None:
        with Collector.open(store) as collector:
            plan = collector.plan_prune(7, now=NOW)

        assert [run.run_id for run in plan.kept] == ["recent"]

    def test_a_wide_window_expires_nothing(self, store: Path) -> None:
        with Collector.open(store) as collector:
            plan = collector.plan_prune(365, now=NOW)

        assert plan.is_empty

    def test_a_run_without_a_start_is_never_expired(self, tmp_path: Path) -> None:
        """An unknown age is not an old age; deleting on a missing field loses data."""
        root = tmp_path / "undated"
        with Collector.open(root) as collector:
            recorder = RunRecorder("orphan", collector)
            recorder._sequence = 5
            recorder.tool_call("build")
            plan = collector.plan_prune(1, now=NOW)

        assert [run.run_id for run in plan.undated] == ["orphan"]
        assert plan.is_empty

    def test_the_plan_describes_what_would_go(self, store: Path) -> None:
        with Collector.open(store) as collector:
            plan = collector.plan_prune(7, now=NOW)

        assert "1 run(s)" in plan.describe()
        assert plan.expiring_events == 3


class TestApplying:
    def test_planning_alone_deletes_nothing(self, store: Path) -> None:
        with Collector.open(store) as collector:
            collector.plan_prune(7, now=NOW)

        with Collector.open(store) as collector:
            assert len(collector.events("old")) == 3

    def test_applying_removes_the_run_everywhere(self, store: Path) -> None:
        with Collector.open(store) as collector:
            result = collector.prune(collector.plan_prune(7, now=NOW))

        assert result.removed_runs == ("old",)
        assert result.removed_events == 3
        with Collector.open(store) as collector:
            assert collector.events("old") == ()
            assert collector.store.run("old") is None
        assert not (store / "runs" / "old").exists()

    def test_the_surviving_run_is_untouched(self, store: Path) -> None:
        with Collector.open(store) as collector:
            collector.prune(collector.plan_prune(7, now=NOW))

        with Collector.open(store) as collector:
            assert len(collector.events("recent")) == 3
            assert collector.integrity("recent").is_intact

    def test_a_payload_still_referenced_is_kept(self, store: Path) -> None:
        """Payloads are content-addressed; two runs ran the same command.

        Deleting the entry with the older run would silently break replay for the
        newer one.
        """
        digest = hash_text("make")

        with Collector.open(store) as collector:
            result = collector.prune(collector.plan_prune(7, now=NOW))
            assert collector.vault.get(digest) is not None

        assert result.vault_entries_removed == 0

    def test_an_orphaned_payload_is_removed(self, tmp_path: Path) -> None:
        root = tmp_path / "solo"
        with Collector.open(root) as collector:
            collector.record_all(
                run_events("only", started=NOW - timedelta(days=30), command="unique-command")
            )
            collector.vault.put("unique-command")
            digest = hash_text("unique-command")

            result = collector.prune(collector.plan_prune(7, now=NOW))

            assert collector.vault.get(digest) is None
        assert result.vault_entries_removed == 1

    def test_pruning_nothing_is_a_no_op(self, store: Path) -> None:
        with Collector.open(store) as collector:
            result = collector.prune(collector.plan_prune(365, now=NOW))

        assert result.removed_anything is False


class TestPruneCommand:
    def test_it_reports_without_deleting_by_default(self, store: Path) -> None:
        result = runner.invoke(app, ["prune", "--store", str(store), "--retain-days", "7"])

        assert result.exit_code == 0, result.output
        assert "Would remove" in result.output
        with Collector.open(store) as collector:
            assert len(collector.events("old")) == 3

    def test_it_names_the_runs_that_would_go(self, store: Path) -> None:
        result = runner.invoke(app, ["prune", "--store", str(store), "--retain-days", "7"])

        assert "old" in result.output

    def test_declining_the_prompt_deletes_nothing(self, store: Path) -> None:
        result = runner.invoke(
            app,
            ["prune", "--store", str(store), "--retain-days", "7", "--apply"],
            input="n\n",
        )

        assert result.exit_code == 1
        with Collector.open(store) as collector:
            assert len(collector.events("old")) == 3

    def test_confirming_deletes(self, store: Path) -> None:
        result = runner.invoke(
            app,
            ["prune", "--store", str(store), "--retain-days", "7", "--apply", "--yes"],
        )

        assert result.exit_code == 0, result.output
        assert "Removed 1 run(s)" in result.output
        with Collector.open(store) as collector:
            assert collector.events("old") == ()

    def test_retention_is_off_by_default(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting on an upgrade would remove somebody's evidence unasked."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["prune", "--store", str(store)])

        assert result.exit_code == 0
        assert "Retention is off" in result.output
        with Collector.open(store) as collector:
            assert len(collector.events("old")) == 3
