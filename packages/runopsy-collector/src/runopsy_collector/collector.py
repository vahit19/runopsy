"""The ingest entry point.

``Collector`` is what a runtime adapter talks to. It writes the authoritative journal
first and updates the queryable index second, so a crash between the two loses an index
row rather than a step of history — and ``rebuild`` puts the index back.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from runopsy_collector.journal import EventJournal, serialize
from runopsy_collector.paths import StorePaths
from runopsy_collector.retention import PrunePlan, PruneResult, apply_prune, plan_prune
from runopsy_collector.store import EventStore, RunSummary
from runopsy_collector.vault import PayloadVault
from runopsy_core import IntegrityReport, check_integrity
from runopsy_core.schema import DiagnosisBundle, Event

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_name(identifier: str) -> str:
    """A filename for an id that arrived over a socket.

    A diagnosis id is built from a run id, and a run id comes from a runtime we do not
    control. Anything that could climb out of the directory is replaced rather than
    trusted.
    """
    return _UNSAFE.sub("_", identifier)[:120]


class Collector:
    """Append-only ingest backed by per-run journals and a DuckDB index."""

    def __init__(self, paths: StorePaths) -> None:
        self.paths = paths
        paths.ensure()
        self.store = EventStore(paths.database)
        self.vault = PayloadVault(paths.vault_dir)

    @classmethod
    def open(cls, root: str | Path | None = None) -> Self:
        """Open the store for ``root``, or the resolved default location.

        Takes a string as readily as a ``Path``: this is the entry point a library user
        meets first, and it used to fail on a string several frames deep with a message
        about ``expanduser``.
        """
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

        **The journal is written before the index, and an index failure is survivable.**
        This ordering is the whole reason the invariant "the journal is authoritative,
        the index is rebuildable" can be relied on, and it was not always this way. The
        dedup check used to run first, which made it a DuckDB *read* standing between an
        event and the only durable copy of it. DuckDB permits one writing process, so
        when an agent delegates work to parallel subagents — each firing its own
        ``runopsy hook`` — that read raises on a locked database and the event was lost
        before anything wrote it down. Measured on this machine: thirty-two concurrent
        hooks, twelve events gone, and nothing louder than a line on stderr.

        Now the journal append happens first and unconditionally. If the index cannot be
        reached the event is still on disk, ``runopsy doctor`` reports the drift, and
        ``rebuild`` restores the index from the journals exactly as the invariant
        promises.
        """
        # Dedup when the index is readable. It is an optimisation, not the record of
        # truth, so an unreachable index must not decide whether history is kept.
        with contextlib.suppress(Exception):
            if self.store.has_event(event.event_id):
                return False

        self.journal(event.run_id).append(event)

        try:
            self.store.insert(event, serialize(event))
        except Exception:
            # Lost the index write to a concurrent writer. The event is durable; say so
            # rather than pretending it was indexed, and leave `rebuild` to catch up.
            logger.warning(
                "indexed nothing for %s: the store is busy. The event is in the journal; "
                "run `runopsy doctor` and rebuild if this persists.",
                event.event_id,
            )
        return True

    def record_all(self, events: Iterable[Event]) -> int:
        """Record several events in one pass, returning how many were new.

        Deliberately not a loop over :meth:`record`. Doing it one at a time costs a file
        open and four database round trips per event, which measured at roughly 20
        events per second — ten minutes to ingest a run an agent produces in an hour.
        Batching the existence check, the journal append and the insert makes the same
        work three orders of magnitude faster, and ingest stops being the reason nobody
        uses the tool on a real trace.
        """
        materialized = tuple(events)
        if not materialized:
            return 0

        known = self.store.event_ids_in_runs([event.run_id for event in materialized])
        fresh: list[Event] = []
        seen: set[str] = set()
        for event in materialized:
            # Deduplicate within the batch too: an adapter that retries mid-batch would
            # otherwise violate the primary key rather than being quietly ignored.
            if event.event_id in known or event.event_id in seen:
                continue
            seen.add(event.event_id)
            fresh.append(event)

        if not fresh:
            return 0

        by_run: dict[str, list[Event]] = {}
        for event in fresh:
            by_run.setdefault(event.run_id, []).append(event)
        for run_id, run_events in by_run.items():
            # One file open per run rather than per event.
            self.journal(run_id).append_all(run_events)

        self.store.insert_many([(event, serialize(event)) for event in fresh])
        return len(fresh)

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

    def save_diagnosis(self, bundle: DiagnosisBundle) -> Path:
        """Keep a diagnosis under its own id, as the design's storage split requires.

        Diagnosis is a pure function of the trace, so this is a cache rather than a
        record of something unrepeatable — but it is what lets a bundle be handed to
        somebody by id, and what ``GET /v1/diagnoses/{id}`` serves. The id already
        carries the trace fingerprint, so a re-analysed run writes a different file
        instead of silently overwriting a diagnosis of different events.
        """
        path = self.paths.diagnoses_dir / f"{_safe_name(bundle.diagnosis_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        return path

    def diagnosis(self, diagnosis_id: str) -> DiagnosisBundle | None:
        """Fetch a stored diagnosis, or ``None`` when it was never saved."""
        path = self.paths.diagnoses_dir / f"{_safe_name(diagnosis_id)}.json"
        if not path.is_file():
            return None
        try:
            return DiagnosisBundle.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def plan_prune(self, retain_days: int, *, now: datetime | None = None) -> PrunePlan:
        """Work out which runs are past the retention window. Removes nothing."""
        return plan_prune(self.store, retain_days, now=now)

    def prune(self, plan: PrunePlan) -> PruneResult:
        """Carry out a plan produced by :meth:`plan_prune`.

        Takes the plan rather than a day count so that what a user was shown and what
        gets deleted are provably the same set.
        """
        return apply_prune(self.paths, self.store, plan)

    def rebuild(self) -> int:
        """Rebuild the index from every journal on disk, returning events indexed.

        This is the repair path for a corrupted or deleted database, and the migration
        path when the index schema changes: the journals are sufficient to reconstruct
        everything the store knows.
        """
        self.store.reset()
        indexed = 0
        for run_id in self.paths.known_run_ids():
            # Batched for the same reason ingest is: rebuilding a large store one row
            # at a time would take longer than recording it did.
            seen: set[str] = set()
            unique: list[Event] = []
            for event in self.journal(run_id).read():
                # A journal can legitimately hold a duplicate — an adapter appended the
                # same event twice — and the index has a primary key. Dropping it here
                # keeps the rebuild working while the integrity check still reports it.
                if event.event_id in seen:
                    continue
                seen.add(event.event_id)
                unique.append(event)
            indexed += self.store.insert_many([(event, serialize(event)) for event in unique])
        return indexed
