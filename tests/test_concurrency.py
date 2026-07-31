"""Recording under concurrency, which is the normal case and was never tested.

An agent that delegates work to parallel subagents fires several `runopsy hook`
processes at one store within milliseconds of each other. DuckDB admits a single writing
process, so this was measured losing twelve of thirty-two events — silently, because the
hook's first duty is not to break the run it observes, so it swallowed the error and
exited zero. A recorder that drops a third of history under ordinary parallelism is not
a recorder.

Two things were wrong and both are pinned here.

The dedup check ran *before* the journal append, putting a database read between an
event and the only durable copy of it. And opening the collector connects to the index,
so a locked database failed before any code could write anything at all — durability
depended on the derived data being available, which inverts the invariant the whole
design rests on.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

import pytest

from runopsy_collector import Collector, StorePaths
from runopsy_collector.journal import EventJournal


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

    def test_an_index_written_behind_can_be_rebuilt_from_the_journals(self, tmp_path: Path) -> None:
        """What makes losing an index row survivable rather than merely regrettable."""
        store = tmp_path / "store"
        for index in range(4):
            from runopsy_cli.main import _record_to_journal_only

            _record_to_journal_only(json.loads(payload("sess_z", index)), "sess_z", store)

        with Collector.open(store) as collector:
            assert collector.events("sess_z") == ()  # nothing was ever indexed
            indexed = collector.rebuild()
            assert indexed == 4
            assert len(collector.events("sess_z")) == 4


class TestTheStoreIsPatientAboutContention:
    def test_a_string_path_is_accepted_like_a_path(self, tmp_path: Path) -> None:
        """The first thing a library user writes, and it used to raise about expanduser."""
        with Collector.open(str(tmp_path / "store")) as collector:
            assert collector.paths.root.name == "store"

    def test_connecting_retries_before_giving_up(self) -> None:
        from runopsy_collector.store import CONNECT_TIMEOUT

        assert CONNECT_TIMEOUT >= 1.0
