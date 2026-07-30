"""Collector tests.

The central claim under test is that the JSONL journal is authoritative and the DuckDB
index is disposable. If that holds, a crashed run stays diagnosable and a corrupted
database is a one-command repair.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import START, run_end, run_start, tool_call
from runopsy_collector import Collector, EventJournal, JournalCorruptionError, StorePaths
from runopsy_core.schema import RunOutcome, ToolCallEvent


class TestRecording:
    def test_recorded_events_come_back_in_execution_order(self, collector: Collector) -> None:
        collector.record(run_start())
        collector.record(tool_call(2))
        collector.record(tool_call(1))

        assert [e.sequence for e in collector.events("run_0042")] == [0, 1, 2]

    def test_a_retried_write_is_not_counted_twice(self, collector: Collector) -> None:
        """Adapters retry; counting the retry would fabricate a loop signal."""
        event = tool_call(1)

        assert collector.record(event) is True
        assert collector.record(event) is False
        assert len(collector.events("run_0042")) == 1

    def test_record_all_reports_only_the_new_events(self, collector: Collector) -> None:
        collector.record(tool_call(1))

        newly = collector.record_all([tool_call(1), tool_call(2), tool_call(3)])

        assert newly == 2

    def test_the_journal_holds_exactly_one_line_per_event(self, collector: Collector) -> None:
        collector.record_all([run_start(), tool_call(1), tool_call(1)])

        assert collector.journal("run_0042").count() == 2

    def test_events_are_byte_identical_across_writes(self, tmp_path: Path) -> None:
        """Deterministic bytes are what make caching a diagnosis by trace hash sound."""
        first = tmp_path / "a.jsonl"
        second = tmp_path / "b.jsonl"
        EventJournal(first).append(tool_call(1))
        EventJournal(second).append(tool_call(1))

        assert first.read_bytes() == second.read_bytes()


class TestRunSummaries:
    def test_a_run_summary_is_assembled_from_its_lifecycle_events(
        self, collector: Collector
    ) -> None:
        collector.record_all([run_start(), tool_call(1), run_end(2)])

        summary = collector.store.run("run_0042")

        assert summary is not None
        assert summary.task == "fix the failing test"
        assert summary.runtime == "hermes"
        assert summary.outcome is RunOutcome.FAILURE
        assert summary.event_count == 3
        assert summary.is_finished is True

    def test_a_run_end_arriving_first_does_not_erase_the_task(self, collector: Collector) -> None:
        """Out-of-order lifecycle events must not blank fields another event owns."""
        collector.record(run_end(2))
        collector.record(run_start())

        summary = collector.store.run("run_0042")

        assert summary is not None
        assert summary.task == "fix the failing test"
        assert summary.outcome is RunOutcome.FAILURE

    def test_an_unfinished_run_is_not_reported_as_failed(self, collector: Collector) -> None:
        """A killed process is not evidence that the task failed."""
        collector.record_all([run_start(), tool_call(1)])

        summary = collector.store.run("run_0042")

        assert summary is not None
        assert summary.is_finished is False
        assert summary.outcome is RunOutcome.UNKNOWN

    def test_secret_bearing_events_are_counted_for_the_export_gate(
        self, collector: Collector
    ) -> None:
        collector.record_all([run_start(), tool_call(1), tool_call(2, contains_secret=True)])

        summary = collector.store.run("run_0042")

        assert summary is not None
        assert summary.secret_event_count == 1

    def test_latest_run_id_picks_the_most_recently_started(self, collector: Collector) -> None:
        collector.record(run_start("run_0001"))
        collector.record(run_start("run_0002", at=START + timedelta(hours=1)))

        assert collector.latest_run_id() == "run_0002"

    def test_a_run_with_no_start_event_does_not_shadow_a_started_one(
        self, collector: Collector
    ) -> None:
        """A run known only from mid-stream events has no start time and cannot be 'latest'."""
        collector.record(run_start("run_0001"))
        collector.record(tool_call(5, "run_orphan"))

        assert collector.latest_run_id() == "run_0001"

    def test_runs_are_isolated_from_one_another(self, collector: Collector) -> None:
        collector.record_all([run_start("run_a"), tool_call(1, "run_a")])
        collector.record_all([run_start("run_b")])

        assert len(collector.events("run_a")) == 2
        assert len(collector.events("run_b")) == 1


class TestRebuild:
    def test_the_index_is_reconstructible_from_the_journals_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        with Collector.open(root) as first:
            first.record_all([run_start(), tool_call(1), run_end(2)])
        database = StorePaths.resolve(root).database

        database.unlink()

        with Collector.open(root) as rebuilt:
            indexed = rebuilt.rebuild()
            assert indexed == 3
            assert len(rebuilt.events("run_0042")) == 3
            summary = rebuilt.store.run("run_0042")
            assert summary is not None
            assert summary.outcome is RunOutcome.FAILURE

    def test_rebuilding_twice_does_not_duplicate_history(self, collector: Collector) -> None:
        collector.record_all([run_start(), tool_call(1)])

        collector.rebuild()
        collector.rebuild()

        assert len(collector.events("run_0042")) == 2

    def test_reindexing_preserves_payloads_exactly(self, collector: Collector) -> None:
        collector.record(tool_call(1, name="pytest", exit_code=1))

        collector.rebuild()
        restored = collector.events("run_0042")[0]

        assert isinstance(restored, ToolCallEvent)
        assert restored.tool.name == "pytest"
        assert restored.tool.exit_code == 1


class TestIntegrity:
    def test_a_complete_run_reports_intact(self, collector: Collector) -> None:
        collector.record_all([run_start(), tool_call(1), tool_call(2)])

        assert collector.integrity("run_0042").is_intact is True

    def test_a_gap_left_by_a_dropped_event_is_reported(self, collector: Collector) -> None:
        collector.record_all([run_start(), tool_call(1), tool_call(3)])

        assert collector.integrity("run_0042").missing_sequences == (2,)

    def test_integrity_reads_the_journal_so_dedup_cannot_hide_corruption(
        self, collector: Collector
    ) -> None:
        """A duplicate appended directly to the log must still be visible."""
        collector.record(tool_call(1))
        collector.journal("run_0042").append(tool_call(1))

        report = collector.integrity("run_0042")

        assert report.duplicate_sequences == (1,)
        assert len(collector.events("run_0042")) == 1


class TestFailureModes:
    def test_a_truncated_journal_line_is_raised_not_skipped(self, collector: Collector) -> None:
        """A silently dropped line becomes an invisible hole in the causal chain."""
        collector.record(tool_call(1))
        path = collector.paths.journal("run_0042")
        with path.open("ab") as handle:
            handle.write(b'{"event_id": "truncated"\n')

        with pytest.raises(JournalCorruptionError):
            list(collector.journal("run_0042").read())

    def test_a_missing_journal_reads_as_empty_history(self, collector: Collector) -> None:
        assert list(collector.journal("never_recorded").read()) == []

    def test_run_ids_cannot_escape_the_store_directory(self, collector: Collector) -> None:
        with pytest.raises(ValueError, match="unsafe run id"):
            collector.paths.journal("../../.ssh/authorized_keys")


class TestStoreLocation:
    def test_an_explicit_root_wins(self, tmp_path: Path) -> None:
        paths = StorePaths.resolve(tmp_path / "explicit")

        assert paths.root == (tmp_path / "explicit").resolve()

    def test_the_environment_is_used_when_no_root_is_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNOPSY_HOME", str(tmp_path / "from_env"))

        assert StorePaths.resolve().root == (tmp_path / "from_env").resolve()

    def test_the_default_is_project_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Traces belong to the repository being worked on, not to one global pile."""
        monkeypatch.delenv("RUNOPSY_HOME", raising=False)
        monkeypatch.chdir(tmp_path)

        assert StorePaths.resolve().root == (tmp_path / ".runopsy").resolve()
