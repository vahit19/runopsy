"""Recording what the working tree did, which is a coding agent's actual output.

Until this existed the trace held what the agent *ran* and what came back, and nothing
about the files it changed. That gap has a name in this repository: the very first real
Hermes session fired the loop detector at HIGH severity on a run that succeeded, because
the agent re-ran one verification command after every edit and an argument hash cannot
see a file.

These tests use real repositories and a real ``git`` rather than a stubbed one. The
parser reads a specific output format from a specific tool, and a fake that agrees with
the parser proves only that the two were written by the same person.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runopsy_adapter.recorder import ListSink
from runopsy_adapter.repo import RepositoryWatch, read_repository
from runopsy_adapter.shell import record_steps
from runopsy_core import AnalysisContext
from runopsy_core.detectors import default_registry
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    Event,
    EventKind,
    StatePayload,
    StateSnapshotEvent,
    ToolCallEvent,
    ToolPayload,
)


def git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A real repository with one commit, which is the normal starting point."""
    root = tmp_path / "project"
    root.mkdir()
    git("init", "-q", ".", cwd=root)
    git("config", "user.email", "test@example.invalid", cwd=root)
    git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "first", cwd=root)
    return root


class TestObservingARepository:
    def test_a_clean_tree_reports_its_commit_and_branch(self, repository: Path) -> None:
        state = read_repository(repository)

        assert state is not None
        assert state.head is not None
        assert len(state.head) == 40
        assert state.branch
        assert not state.dirty

    def test_an_edit_is_reported_with_the_lines_it_moved(self, repository: Path) -> None:
        """A path list cannot say whether a step rewrote a file or merely touched it."""
        (repository / "app.py").write_text("a\nCHANGED\nc\n", encoding="utf-8")

        state = read_repository(repository)

        assert state is not None
        assert state.dirty
        assert "app.py" in state.changed_paths
        assert state.edits["app.py"] == (1, 1)

    def test_a_new_file_is_reported_as_untracked(self, repository: Path) -> None:
        (repository / "extra.py").write_text("x\n", encoding="utf-8")

        state = read_repository(repository)

        assert state is not None
        assert state.untracked_count == 1

    def test_somewhere_without_a_repository_says_nothing(self, tmp_path: Path) -> None:
        """Running an agent outside version control is ordinary, not an error."""
        plain = tmp_path / "plain"
        plain.mkdir()

        assert read_repository(plain) is None


class TestWhatEachLookIsWorthRecording:
    def test_the_first_look_is_always_worth_a_snapshot(self, repository: Path) -> None:
        """Without a baseline, a run that began dirty looks like the agent made the mess."""
        observed = RepositoryWatch().observe(repository)

        assert observed is not None
        assert observed.worth_a_snapshot

    def test_a_commit_is_reported_as_a_state_delta(self, repository: Path) -> None:
        watch = RepositoryWatch()
        watch.observe(repository)

        (repository / "app.py").write_text("a\nB\nc\n", encoding="utf-8")
        git("add", "-A", cwd=repository)
        git("commit", "-qm", "second", cwd=repository)
        observed = watch.observe(repository)

        assert observed is not None
        assert "git.head" in observed.deltas
        assert observed.deltas["git.head"].before != observed.deltas["git.head"].after

    def test_an_unchanged_tree_is_not_worth_recording_twice(self, repository: Path) -> None:
        watch = RepositoryWatch()
        watch.observe(repository)

        assert watch.observe(repository).worth_a_snapshot is False  # type: ignore[union-attr]

    def test_editing_the_same_file_twice_reads_as_two_changes(self, repository: Path) -> None:
        """Status alone cannot see this: the file is simply 'modified' both times.

        It is the difference between "this step changed the code" and "this step only ran
        the tests", which is most of what a reader wants from a coding agent's trace.
        """
        watch = RepositoryWatch()
        watch.observe(repository)

        (repository / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
        first = watch.observe(repository)
        (repository / "app.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        second = watch.observe(repository)

        assert first is not None
        assert second is not None
        assert first.worth_a_snapshot
        assert second.worth_a_snapshot

    def test_the_cursor_survives_a_new_process(self, repository: Path, tmp_path: Path) -> None:
        """A hook-based adapter is a fresh process per event; memory is not available."""
        run_dir = tmp_path / "run"
        RepositoryWatch(run_dir).observe(repository)

        again = RepositoryWatch(run_dir).observe(repository)

        assert again is not None
        assert again.worth_a_snapshot is False


class TestRecordingAPipelineInARepository:
    def test_the_run_carries_a_baseline_and_what_each_step_changed(self, repository: Path) -> None:
        sink = ListSink()

        record_steps(
            ["python -c \"open('app.py','w').write('a\\nZ\\nc\\n')\"", "python -c pass"],
            run_id="run_repo",
            task="edit then check",
            sink=sink,
            cwd=repository,
        )

        snapshots = [event for event in sink.events if event.kind is EventKind.STATE_SNAPSHOT]
        assert len(snapshots) >= 2, "expected a baseline and the edit"
        assert snapshots[0].state.values["git.dirty"] is False
        assert any(state.state.values.get("git.dirty") for state in snapshots[1:])

    def test_capture_can_be_turned_off(self, repository: Path) -> None:
        sink = ListSink()

        record_steps(
            ["python -c pass"],
            run_id="run_off",
            task="t",
            sink=sink,
            cwd=repository,
            capture_git=False,
        )

        assert not [event for event in sink.events if event.kind is EventKind.STATE_SNAPSHOT]

    def test_recording_does_not_put_the_store_in_the_agents_commit(self, repository: Path) -> None:
        """Runopsy must not change the outcome of the run it is observing.

        Measured before the store excluded itself: an agent's own ``git add -A`` swept
        the store in and the commit then failed with exit 128, because DuckDB held the
        index open and git could not read it. The run was altered by being watched.
        """
        from runopsy_collector import Collector

        with Collector.open(repository / ".runopsy") as collector:
            record_steps(
                [
                    "python -c \"open('app.py','a').write('extra')\"",
                    "git add -A",
                    "git commit -qm from-the-agent",
                ],
                run_id="run_commit",
                task="commit everything",
                sink=collector,
                cwd=repository,
            )
            events = collector.events("run_commit")

        commits = [
            event
            for event in events
            if isinstance(event, ToolCallEvent) and event.tool.name == "git"
        ]
        assert commits, "the git steps were not recorded"
        assert all(event.tool.exit_code == 0 for event in commits), "recording broke the run"

        tracked = subprocess.run(
            ["git", "ls-files"], cwd=repository, capture_output=True, text=True, check=True
        )
        assert ".runopsy" not in tracked.stdout


class TestEvidenceShowsWhatTheStepChanged:
    """The payoff. Capturing the working tree is worth nothing if nobody is shown it."""

    def test_the_step_that_edited_a_file_says_which_file_and_how_much(
        self, repository: Path
    ) -> None:
        from typer.testing import CliRunner

        from runopsy_cli.main import app
        from runopsy_collector import Collector

        store = repository / ".runopsy"
        with Collector.open(store) as collector:
            record_steps(
                ["python -c \"open('app.py','w').write('a\\nZ\\nc\\nd\\n')\""],
                run_id="run_ev",
                task="edit a file",
                sink=collector,
                cwd=repository,
            )

        # Step 1 is the baseline snapshot; the command the agent ran is the step after it.
        result = CliRunner().invoke(
            app, ["evidence", "run_ev", "--step", "2", "--store", str(store)]
        )

        assert result.exit_code == 0, result.output
        assert "repository" in result.output
        assert "app.py" in result.output


def _loops(events: Sequence[Event]) -> list[str]:
    """The loop signals a trace produces, which is the one thing these cases are about."""
    signals = default_registry().run(AnalysisContext.from_events("run_loop", events))
    return [signal.summary for signal in signals if signal.detector == "behavioral:tool_loop"]


def _repeated_call(sequence: int, *, output: str) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"run_loop_evt_{sequence:04d}",
        run_id="run_loop",
        sequence=sequence,
        timestamp=datetime(2026, 7, 31, tzinfo=UTC) + timedelta(seconds=sequence),
        tool=ToolPayload(
            name="pytest",
            arguments_hash=hash_text("same-arguments"),
            output_hash=hash_text(output),
            exit_code=0,
            status=CallStatus.OK,
        ),
    )


def _tree(sequence: int, *, paths: list[str], added: int) -> StateSnapshotEvent:
    return StateSnapshotEvent(
        event_id=f"run_loop_evt_{sequence:04d}",
        run_id="run_loop",
        sequence=sequence,
        timestamp=datetime(2026, 7, 31, tzinfo=UTC) + timedelta(seconds=sequence),
        state=StatePayload(
            values={
                "git.head": "abc",
                "git.dirty": True,
                "git.changed_paths": paths,
                "git.edits": {paths[0]: {"added": added, "removed": 0}},
            }
        ),
    )


class TestTheLoopDetectorCanNowSeeTheFiles:
    """The correction the first real session asked for, and could not have.

    A repeated verification command is only a loop if repeating it got nowhere. Output
    was the sole evidence available, and output is silent when the command prints
    nothing — which is exactly when an agent editing files looks most stuck.
    """

    def test_a_repeated_check_is_not_a_loop_while_the_tree_keeps_moving(self) -> None:
        events: list[Event] = [
            _repeated_call(1, output="same"),
            _tree(2, paths=["app.py"], added=1),
            _repeated_call(3, output="same"),
            _tree(4, paths=["app.py"], added=2),
            _repeated_call(5, output="same"),
            _tree(6, paths=["app.py"], added=3),
        ]

        assert not _loops(events)

    def test_a_tree_that_keeps_returning_to_itself_is_still_a_loop(self) -> None:
        """The twenty-five-step session that really was stuck also rewrote a file every
        step. "The repository changed" would therefore silence a true finding; what
        separates them is whether the tree reaches somewhere it has not been."""
        events: list[Event] = []
        for index in range(12):
            events.append(_repeated_call(index * 2 + 1, output="same"))
            events.append(_tree(index * 2 + 2, paths=["app.py"], added=1 + index % 2))

        assert _loops(events)

    def test_a_trace_without_repository_data_behaves_exactly_as_before(self) -> None:
        """Which is what keeps every existing result, and the benchmark, untouched."""
        events = [_repeated_call(index, output="same") for index in range(1, 5)]

        assert _loops(events)


class TestCheckpointsMakeAReplayAboutTheOriginalRun:
    """The gap that made every replay plan warn about itself.

    `build_plan` has always looked for checkpoints and never found one, because nothing
    took them: a checkpoint needs the working tree, and the trace held only commands. So
    every plan carried "file state cannot be restored", and every execution started from
    whatever was on disk *now* — after the failure, after any manual fixing, possibly
    weeks later. A step that behaved differently said nothing about the original.
    """

    def test_a_run_in_a_repository_records_points_it_can_return_to(self, repository: Path) -> None:
        from runopsy_collector import Collector
        from runopsy_core.schema import CheckpointEvent

        store = repository / ".runopsy"
        with Collector.open(store) as collector:
            record_steps(
                ["python -c \"open('app.py','w').write('broken')\""],
                run_id="run_ck",
                task="break the file",
                sink=collector,
                vault=collector.vault,
                cwd=repository,
            )
            events = collector.events("run_ck")

        checkpoints = [event for event in events if isinstance(event, CheckpointEvent)]
        assert checkpoints, "nothing recorded a point the run could be returned to"
        assert all(event.checkpoint.repo_state for event in checkpoints)
        assert any(event.checkpoint.patch_digest for event in checkpoints), (
            "a checkpoint with no patch can be named but not restored"
        )

    def test_the_patch_is_kept_in_runopsys_vault_not_the_users_repository(
        self, repository: Path
    ) -> None:
        """Runopsy was asked to watch this repository, not to write to it."""
        from runopsy_collector import Collector

        store = repository / ".runopsy"
        before = subprocess.run(
            ["git", "rev-list", "--all", "--count"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        with Collector.open(store) as collector:
            record_steps(
                ["python -c \"open('app.py','w').write('broken')\""],
                run_id="run_ck2",
                task="break the file",
                sink=collector,
                vault=collector.vault,
                cwd=repository,
            )

        after = subprocess.run(
            ["git", "rev-list", "--all", "--count"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert before == after, "recording created objects in the user's repository"

    def test_the_replay_restores_the_tree_and_the_failure_goes_away(self, repository: Path) -> None:
        """End to end, and the point of the whole feature.

        A step breaks a file, a later step notices. Skipping the breaking step is only a
        real experiment if the sandbox starts from the tree as it was — otherwise the
        broken file is still there, copied from disk, and the check fails anyway.
        """
        from runopsy_collector import Collector
        from runopsy_core import AnalysisContext
        from runopsy_replay import build_plan, execute_plan

        (repository / "check.py").write_text(
            'import pathlib\nassert pathlib.Path("app.py").read_text().startswith("a")\n',
            encoding="utf-8",
        )
        git("add", "-A", cwd=repository)
        git("commit", "-qm", "add the check", cwd=repository)

        store = repository / ".runopsy"
        with Collector.open(store) as collector:
            record_steps(
                [
                    "python -c \"open('app.py','w').write('BROKEN')\"",
                    "python check.py",
                ],
                run_id="run_break",
                task="break then notice",
                sink=collector,
                vault=collector.vault,
                cwd=repository,
            )
            events = collector.events("run_break")
            context = AnalysisContext.from_events("run_break", events)

            onset = next(
                event.sequence
                for event in events
                if isinstance(event, ToolCallEvent) and event.tool.exit_code == 0
            )
            plan = build_plan(context, from_sequence=onset)
            assert plan.checkpoint_sequence is not None, "no checkpoint to anchor the replay"

            verdict = execute_plan(
                plan,
                context,
                collector.vault,
                collector,
                replay_run_id="run_break_replay",
                cwd=repository,
                skip_onset=True,
                approve_unknown=True,
            )

        assert "restored" in verdict.checkpoint_restored
        assert verdict.supports_onset, (
            f"skipping the onset did not clear the downstream failure: {verdict}"
        )

    def test_a_replay_without_a_checkpoint_says_so_rather_than_pretending(
        self, tmp_path: Path
    ) -> None:
        """Outside a repository there is nothing to restore, and the verdict must admit
        it — a result that looks the same either way is worse than no result."""
        from runopsy_collector import Collector
        from runopsy_core import AnalysisContext
        from runopsy_replay import build_plan, execute_plan

        plain = tmp_path / "plain"
        plain.mkdir()
        store = tmp_path / "store"
        with Collector.open(store) as collector:
            record_steps(
                ["python -c pass", "python -c pass"],
                run_id="run_plain",
                task="no repository here",
                sink=collector,
                vault=collector.vault,
                cwd=plain,
            )
            events = collector.events("run_plain")
            context = AnalysisContext.from_events("run_plain", events)
            verdict = execute_plan(
                build_plan(context, from_sequence=1),
                context,
                collector.vault,
                collector,
                replay_run_id="run_plain_replay",
                cwd=plain,
                approve_unknown=True,
            )

        assert "no checkpoint" in verdict.checkpoint_restored
