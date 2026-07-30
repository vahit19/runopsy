"""The ingest entry point.

``Collector`` is what a runtime adapter talks to. It writes the authoritative journal
first and updates the queryable index second, so a crash between the two loses an index
row rather than a step of history — and ``rebuild`` puts the index back.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Self

from runopsy_collector.journal import EventJournal, serialize
from runopsy_collector.paths import StorePaths
from runopsy_collector.store import EventStore, RunSummary
from runopsy_core import IntegrityReport, check_integrity
from runopsy_core.schema import Event


class Collector:
    """Append-only ingest backed by per-run journals and a DuckDB index."""

    def __init__(self, paths: StorePaths) -> None:
        self.paths = paths
        paths.ensure()
        self.store = EventStore(paths.database)

    @classmethod
    def open(cls, root: Path | None = None) -> Self:
        """Open the store for ``root``, or the resolved default location."""
        return cls(StorePaths.resolve(root))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    def journal(self, run_id: str) -> EventJournal:
        """The append-only log for one run."""
        return EventJournal(self.paths.journal(run_id))

    def record(self, event: Event) -> bool:
        """Record one event, returning whether it was new.

        Ingest is idempotent on ``event_id`` because adapters retry. Counting a retried
        write as a second step would inflate exactly the repetition that the loop and
        retry-storm detectors look for, manufacturing a failure signal out of a
        bookkeeping artifact.
        """
        if self.store.has_event(event.event_id):
            return False
        payload = serialize(event)
        self.journal(event.run_id).append(event)
        self.store.insert(event, payload)
        return True

    def record_all(self, events: Iterable[Event]) -> int:
        """Record several events, returning how many were new."""
        return sum(1 for event in events if self.record(event))

    def events(self, run_id: str) -> tuple[Event, ...]:
        """Indexed events for a run, in execution order."""
        return self.store.events(run_id)

    def runs(self) -> tuple[RunSummary, ...]:
        """All known runs, most recently started first."""
        return self.store.runs()

    def latest_run_id(self) -> str | None:
        """The run that bare ``latest`` refers to on the command line."""
        return self.store.latest_run_id()

    def integrity(self, run_id: str) -> IntegrityReport:
        """Check a run's journal for gaps, duplicates and reordering.

        Read from the journal rather than the index, because the index deduplicates by
        ``event_id`` and would therefore hide the very corruption this reports.
        """
        return check_integrity(run_id, self.journal(run_id).read())

    def rebuild(self) -> int:
        """Rebuild the index from every journal on disk, returning events indexed.

        This is the repair path for a corrupted or deleted database, and the migration
        path when the index schema changes: the journals are sufficient to reconstruct
        everything the store knows.
        """
        self.store.reset()
        indexed = 0
        for run_id in self.paths.known_run_ids():
            for event in self.journal(run_id).read():
                if self.store.insert(event, serialize(event)):
                    indexed += 1
        return indexed
