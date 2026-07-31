"""Recording under concurrency, which is the normal case and was never tested.

An agent that delegates work to parallel subagents fires several `runopsy hook`
processes at one store within milliseconds of each other. DuckDB admits a single writing
process, so this was measured losing twelve of thirty-two events — silently, because the
hook's first duty is not to break the run it observes, so it swallowed the error and
exited zero. A recorder that drops a third of history under ordinary parallelism is not
a recorder.

Three things were wrong and all three are pinned here.

The dedup check ran *before* the journal append, putting a database read between an
event and the only durable copy of it. Opening the collector connects to the index, so a
locked database failed before any code could write anything at all — durability depended
on the derived data being available, which inverts the invariant the whole design rests
on. And step numbers came from ``SELECT MAX(sequence) + 1`` with no lock holding the
answer, so two subagents in one session took the same number; since an adapter builds the
event id out of that number, the second event was deduplicated away as a repeat of the
first.

The last one only shows up when the parallel work shares a session id, which is what
Hermes subagents actually do — the first test here gives each its own, and passes either
way.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

import pytest

from runopsy_collector import Collector, SequenceAllocator, StorePaths
from runopsy_collector.journal import EventJournal
from runopsy_collector.sequence import COUNTER_NAME


def payload(session: str, index: int) -> str:
    return json.dumps(
        {
            "hook_event_name": "post_tool_call",
            "session_id": session,
            "tool_name": "terminal",
            "args": f"cmd-{index}",
            "extra": {
                "status": "ok",
                "result": json.dumps({"output": f"out{index}", "exit_code": 0}),
            },
        }
    )


def fire_hook(store: Path, session: str, index: int) -> tuple[int, str]:
    """One hook, as Hermes invokes it: a fresh process per event."""
    result = subprocess.run(
        [sys.executable, "-m", "runopsy_cli", "hook", "post_tool_call", "--store", str(store)],
        input=payload(session, index),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    return result.returncode, result.stderr.strip()


class TestParallelSubagentsDoNotLoseHistory:
    def test_every_concurrent_event_reaches_the_journal(self, tmp_path: Path) -> None:
        """Thirty-two hooks at once, which is four subagents doing eight steps each.

        This is the test that would have caught the original defect: it passed at 20/32
        before the ordering was fixed and 29/32 after, and only reaches 32/32 once
        writing the journal stopped requiring the index at all.
        """
        store = tmp_path / "store"
        work = [(f"sess_{s}", i) for s in range(4) for i in range(8)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda a: fire_hook(store, *a), work))

        recorded = sum(
            len(path.read_text(encoding="utf-8").strip().splitlines())
            for path in (store / "runs").glob("*/events.jsonl")
        )

        assert recorded == len(work), f"lost {len(work) - recorded} of {len(work)} events"
        assert all(code == 0 for code, _ in results), "a hook failed the run it was observing"

    def test_a_hook_never_fails_the_run_it_observes(self, tmp_path: Path) -> None:
        """Whatever happens to the store, the agent must keep going."""
        store = tmp_path / "store"

        code, _ = fire_hook(store, "sess", 0)

        assert code == 0


class TestDurabilityDoesNotDependOnTheIndex:
    """The invariant, tested rather than asserted: the journal is authoritative."""

    def test_the_journal_is_written_even_when_the_index_cannot_be_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from runopsy_cli.main import _record_to_journal_only

        store = tmp_path / "store"
        _record_to_journal_only(json.loads(payload("sess_x", 1)), "sess_x", store)

        events = list(EventJournal(StorePaths.resolve(store).journal("sess_x")).read())
        assert len(events) == 1
        assert events[0].run_id == "sess_x"

    def test_the_fallback_numbers_events_from_the_journal(self, tmp_path: Path) -> None:
        """With no database to ask, the sequence has to come from the file itself."""
        store = tmp_path / "store"

        for index in range(3):
            _payload = json.loads(payload("sess_y", index))
            from runopsy_cli.main import _record_to_journal_only

            _record_to_journal_only(_payload, "sess_y", store)

        events = list(EventJournal(StorePaths.resolve(store).journal("sess_y")).read())
        assert [event.sequence for event in events] == [0, 1, 2]

    def test_an_index_written_behind_catches_up_without_being_asked(self, tmp_path: Path) -> None:
        """What makes losing an index row survivable rather than merely regrettable.

        Reading repairs the drift rather than reporting it. Requiring an explicit
        ``rebuild`` would mean a user who records a run and diagnoses it immediately
        gets whichever steps won the race, and a trace missing three of thirty-two steps
        says nothing about itself — it just moves the onset.
        """
        store = tmp_path / "store"
        for index in range(4):
            from runopsy_cli.main import _record_to_journal_only

            _record_to_journal_only(json.loads(payload("sess_z", index)), "sess_z", store)

        with Collector.open(store) as collector:
            # Nothing was ever indexed, yet all four are here: reading reconciled them.
            assert len(collector.events("sess_z")) == 4
            assert collector.reconcile("sess_z") == 0  # and asking again costs nothing

    def test_a_journal_only_run_is_still_listed_and_still_the_latest(self, tmp_path: Path) -> None:
        """Otherwise the run is on disk, absent from every listing, and unreachable."""
        from runopsy_cli.main import _record_to_journal_only

        store = tmp_path / "store"
        _record_to_journal_only(json.loads(payload("sess_only", 0)), "sess_only", store)

        with Collector.open(store) as collector:
            assert [summary.run_id for summary in collector.runs()] == ["sess_only"]
            assert collector.latest_run_id() == "sess_only"

    def test_reconciling_survives_an_unusable_index(self, tmp_path: Path) -> None:
        """Repair is best-effort: failing to fix the index must not refuse the answer."""
        from runopsy_cli.main import _record_to_journal_only

        store = tmp_path / "store"
        _record_to_journal_only(json.loads(payload("sess_broken", 0)), "sess_broken", store)

        with Collector.open(store) as collector:
            collector.store.close()
            assert collector.reconcile("sess_broken") == 0


class TestStepNumbersStayUniqueAcrossProcesses:
    """A sequence is an identity, not a label.

    An adapter builds the event id out of it — ``{run_id}_evt_{sequence:04d}`` — so two
    events handed one number are one event to every layer downstream, and the second is
    deduplicated out of the trace. Measured before this was fixed: thirty-two concurrent
    steps in one session, thirty survived, and the integrity report called it a duplicate
    sequence rather than two steps that no longer exist.
    """

    def test_parallel_subagents_sharing_a_session_keep_every_step(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        session = "shared_session"

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda i: fire_hook(store, session, i), range(32)))

        events = list(EventJournal(StorePaths.resolve(store).journal(session)).read())
        sequences = [event.sequence for event in events]

        assert len(events) == 32
        assert len({event.event_id for event in events}) == 32, "two steps shared one id"
        assert sorted(sequences) == list(range(32))

        with Collector.open(store) as collector:
            assert len(collector.events(session)) == 32
            report = collector.integrity(session)
            assert report.duplicate_sequences == ()
            assert report.missing_sequences == ()

    def test_reserving_hands_out_each_number_once(self, tmp_path: Path) -> None:
        allocator = SequenceAllocator(tmp_path / "run")

        assert [allocator.reserve() for _ in range(5)] == [0, 1, 2, 3, 4]

    def test_reserving_a_block_advances_by_the_whole_block(self, tmp_path: Path) -> None:
        allocator = SequenceAllocator(tmp_path / "run")

        assert allocator.reserve(4) == 0
        assert allocator.reserve() == 4

    def test_a_deleted_counter_resumes_from_the_journal(self, tmp_path: Path) -> None:
        """The counter is derived data. Losing it must not restart numbering at zero."""
        store = tmp_path / "store"
        for index in range(3):
            fire_hook(store, "sess_seed", index)

        run_dir = StorePaths.resolve(store).run_dir("sess_seed")
        (run_dir / COUNTER_NAME).unlink()

        assert SequenceAllocator(run_dir).reserve() == 3

    def test_an_unrecorded_run_starts_at_zero(self, tmp_path: Path) -> None:
        assert SequenceAllocator(tmp_path / "never-used").reserve() == 0

    def test_reserving_nothing_is_a_mistake_worth_raising(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="positive"):
            SequenceAllocator(tmp_path / "run").reserve(0)


class TestTheStoreIsPatientAboutContention:
    def test_a_string_path_is_accepted_like_a_path(self, tmp_path: Path) -> None:
        """The first thing a library user writes, and it used to raise about expanduser."""
        with Collector.open(str(tmp_path / "store")) as collector:
            assert collector.paths.root.name == "store"

    def test_connecting_retries_before_giving_up(self) -> None:
        from runopsy_collector.store import CONNECT_TIMEOUT

        assert CONNECT_TIMEOUT >= 1.0
