"""DuckDB index over recorded events.

This is derived data. Every row here can be reproduced from the JSONL journals, which
is why the schema optimizes for querying rather than durability: a single local file,
no server, and SQL over runs, steps and state changes.
"""

from __future__ import annotations

import csv
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

import duckdb
import orjson
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from runopsy_core.schema import (
    SCHEMA_VERSION,
    Event,
    RunEndEvent,
    RunOutcome,
    RunStartEvent,
)

_events: TypeAdapter[Event] = TypeAdapter(Event)

STORE_VERSION: Final = "1"

BULK_LOAD_THRESHOLD: Final = 64
"""Batch size above which staging a CSV beats inserting row by row."""


def _row(event: Event, payload: bytes) -> tuple[Any, ...]:
    """One event as the columns of the events table, in order."""
    return (
        event.event_id,
        event.run_id,
        event.agent_id,
        event.parent_id,
        event.kind.value,
        event.sequence,
        _to_storage(event.timestamp),
        event.schema_version,
        event.security.redacted,
        event.security.contains_secret,
        payload.decode("utf-8").strip(),
    )


def _digests_of(event: Event) -> set[str]:
    """Payload digests an event refers to.

    Walks the kind-specific payload rather than naming fields, so a schema addition that
    introduces another hash cannot silently escape retention accounting.
    """
    found: set[str] = set()
    for value in event.model_dump(mode="json").values():
        if isinstance(value, dict):
            found.update(
                item
                for item in value.values()
                if isinstance(item, str) and item.startswith("sha256:")
            )
    return found


def _to_storage(moment: datetime) -> datetime:
    """Normalize to naive UTC for the index.

    Timestamps are stored without a zone so the index needs no timezone database at
    runtime, keeping the install to a single wheel. Nothing is lost: the event's
    original offset is preserved verbatim in the journal, which is authoritative, and
    ordering — the only thing the index uses timestamps for — is unaffected by it.
    """
    return moment.astimezone(UTC).replace(tzinfo=None)


def _from_storage(moment: datetime | None) -> datetime | None:
    """Reattach UTC to a value read back from the index."""
    return None if moment is None else moment.replace(tzinfo=UTC)


_DDL: Final = """
CREATE TABLE IF NOT EXISTS store_meta (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      VARCHAR PRIMARY KEY,
    task        VARCHAR,
    repo        VARCHAR,
    runtime     VARCHAR,
    provider    VARCHAR,
    model       VARCHAR,
    outcome     VARCHAR NOT NULL DEFAULT 'unknown',
    started_at  TIMESTAMP,
    ended_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    event_id        VARCHAR PRIMARY KEY,
    run_id          VARCHAR NOT NULL,
    agent_id        VARCHAR NOT NULL,
    parent_id       VARCHAR,
    kind            VARCHAR NOT NULL,
    sequence        BIGINT  NOT NULL,
    timestamp       TIMESTAMP NOT NULL,  -- naive UTC; see _to_storage
    schema_version  VARCHAR NOT NULL,
    redacted        BOOLEAN NOT NULL,
    contains_secret BOOLEAN NOT NULL,
    payload         VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS events_by_run ON events (run_id, sequence);
"""


class RunSummary(BaseModel):
    """One row of ``runopsy runs``."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    task: str = ""
    repo: str | None = None
    runtime: str = "unknown"
    provider: str | None = None
    model: str | None = None
    outcome: RunOutcome = RunOutcome.UNKNOWN
    started_at: datetime | None = None
    ended_at: datetime | None = None
    event_count: int = Field(default=0, ge=0)
    secret_event_count: int = Field(default=0, ge=0)

    @property
    def is_finished(self) -> bool:
        """Whether a ``run_end`` was recorded.

        An unfinished run is not the same as a failed one: the process may have been
        killed before it could report, and saying "failed" for a crash would be a
        conclusion the trace does not support.
        """
        return self.ended_at is not None


CONNECT_TIMEOUT = 5.0
"""Seconds to keep trying for the index before giving up on it.

DuckDB admits one writing process. An agent that delegates to parallel subagents fires
several ``runopsy hook`` processes at the same store within milliseconds of each other,
and without patience most of them met a locked file and dropped their event: measured at
twelve losses in thirty-two concurrent hooks.

Contention here is short — a single insert — so waiting briefly converts almost all of
it into a successful write. Five seconds is far longer than any real contention and far
shorter than a person waiting on a hook would tolerate; the journal covers whatever
still fails.
"""


def _is_newer(found: str, understood: str) -> bool:
    """Whether ``found`` is a later version than this build writes.

    Compared component by component as integers, so 0.10 is later than 0.9 rather than
    earlier — the comparison a string would get wrong exactly once, at the version where
    getting it wrong means refusing to open a store that is perfectly readable.
    """

    def parts(value: str) -> list[int]:
        return [int(piece) if piece.isdigit() else 0 for piece in value.split(".")]

    return parts(found) > parts(understood)


def _connect_with_patience(database: Path, timeout: float) -> duckdb.DuckDBPyConnection:
    """Open the index, retrying while another process holds it."""
    deadline = time.monotonic() + timeout
    delay = 0.02
    while True:
        try:
            return duckdb.connect(str(database))
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            # Back off so a crowd of hooks does not synchronise into a thundering herd,
            # capped so the last attempt still lands inside the deadline.
            delay = min(delay * 2, 0.4)


@dataclass(frozen=True)
class StoreVersions:
    """The versions a store was written with, and how they compare to this build."""

    store_version: str
    schema_version: str
    newly_created: bool

    @property
    def matches_this_build(self) -> bool:
        return self.store_version == STORE_VERSION and self.schema_version == SCHEMA_VERSION

    def describe(self) -> str:
        if self.matches_this_build:
            return f"schema {self.schema_version} (current)"
        return (
            f"schema {self.schema_version}, store {self.store_version} — "
            f"this build writes schema {SCHEMA_VERSION}, store {STORE_VERSION}"
        )


class StoreFromTheFutureError(RuntimeError):
    """A store written by a newer Runopsy than this one.

    Refused rather than opened, and this is the one version mismatch worth refusing.
    Reading it would be tolerable; *writing* to it is not, because this build would
    serialize events without fields it has never heard of and the journal would end up
    holding two incompatible shapes with nothing recording which is which. Telling
    somebody to upgrade costs them a minute. Silently degrading their history costs them
    the history.
    """

    def __init__(self, found: str, understood: str) -> None:
        super().__init__(
            f"this store was written with schema {found}; this Runopsy understands "
            f"{understood}. Upgrade with `pip install --upgrade runopsy`, or point "
            f"--store at a different directory."
        )
        self.found = found
        self.understood = understood


class EventStore:
    """Queryable index over recorded events."""

    def __init__(self, database: Path, *, connect_timeout: float = CONNECT_TIMEOUT) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self._seen_runs: set[str] = set()
        self._connection = _connect_with_patience(database, connect_timeout)
        self._connection.execute(_DDL)
        self.written_by = self._stamp_or_check()

    def _stamp_or_check(self) -> StoreVersions:
        """Record the versions on a new store; on an existing one, leave them alone.

        This used to be an upsert, which meant opening a store written by an older build
        silently restamped it with today's numbers. That is worse than not checking at
        all: the one piece of evidence that the history was recorded under a different
        schema was destroyed by the act of looking at it, and nothing downstream could
        ever tell.

        So the stamp is written once, at creation. An older store keeps saying it is
        older — `runopsy doctor` reports that, and each event carries its own version
        besides. A *newer* store is refused, because this build would write events into
        it in a shape it does not know how to describe.
        """
        rows = self._connection.execute(
            "SELECT key, value FROM store_meta WHERE key IN ('store_version', 'schema_version')"
        ).fetchall()
        recorded = {str(key): str(value) for key, value in rows}

        if not recorded:
            self._connection.execute(
                "INSERT INTO store_meta (key, value) VALUES (?, ?), (?, ?)",
                ["store_version", STORE_VERSION, "schema_version", SCHEMA_VERSION],
            )
            return StoreVersions(STORE_VERSION, SCHEMA_VERSION, newly_created=True)

        found_schema = recorded.get("schema_version", SCHEMA_VERSION)
        found_store = recorded.get("store_version", STORE_VERSION)
        if _is_newer(found_schema, SCHEMA_VERSION) or _is_newer(found_store, STORE_VERSION):
            raise StoreFromTheFutureError(found_schema, SCHEMA_VERSION)
        return StoreVersions(found_store, found_schema, newly_created=False)

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
        self._connection.close()

    def has_event(self, event_id: str) -> bool:
        """Whether this event was already indexed."""
        row = self._connection.execute(
            "SELECT 1 FROM events WHERE event_id = ?", [event_id]
        ).fetchone()
        return row is not None

    def insert(self, event: Event, payload: bytes) -> bool:
        """Index one event, returning whether it was new.

        ``payload`` is the exact journal bytes rather than a re-serialization, so the
        stored copy and the authoritative log can never drift apart.
        """
        if self.has_event(event.event_id):
            return False
        self._connection.execute(
            "INSERT INTO events (event_id, run_id, agent_id, parent_id, kind, sequence, "
            "timestamp, schema_version, redacted, contains_secret, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                event.event_id,
                event.run_id,
                event.agent_id,
                event.parent_id,
                event.kind.value,
                event.sequence,
                _to_storage(event.timestamp),
                event.schema_version,
                event.security.redacted,
                event.security.contains_secret,
                payload.decode("utf-8"),
            ],
        )
        self._update_run(event)
        return True

    def event_ids_in_runs(self, run_ids: Sequence[str]) -> set[str]:
        """Every indexed event id belonging to these runs.

        Queried by run rather than by a list of ids: binding thousands of parameters to
        an ``IN`` clause measured at seconds, while this uses the run index and returns
        a set bounded by the size of the run being added to.

        The run ids bind as a single list parameter rather than as a generated row of
        placeholders, so the statement is a fixed string. That keeps the query plan
        cacheable and leaves no way for a run id to reach the SQL text at all.
        """
        unique = sorted(set(run_ids))
        if not unique:
            return set()
        rows = self._connection.execute(
            "SELECT event_id FROM events WHERE run_id IN (SELECT unnest(?))",
            [unique],
        ).fetchall()
        return {str(row[0]) for row in rows}

    def insert_many(self, batch: Sequence[tuple[Event, bytes]]) -> int:
        """Index a batch of already-deduplicated events.

        Above a small threshold this writes a temporary CSV and lets DuckDB ``COPY`` it
        in. That looks indirect, and it is a hundredfold faster: DuckDB is a columnar
        analytical engine whose row-at-a-time insert path costs milliseconds per row,
        while its bulk loader is the thing it is actually built around. Measured on
        5,000 events, row-at-a-time took 34 seconds and this takes 0.16.

        The caller is responsible for having filtered ids that already exist; checking
        again here would reintroduce the per-event query this exists to avoid.
        """
        if not batch:
            return 0

        if len(batch) < BULK_LOAD_THRESHOLD:
            self._connection.executemany(
                "INSERT INTO events (event_id, run_id, agent_id, parent_id, kind, sequence, "
                "timestamp, schema_version, redacted, contains_secret, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [list(_row(event, payload)) for event, payload in batch],
            )
        else:
            self._bulk_load(batch)

        for event, _ in batch:
            self._update_run(event)
        return len(batch)

    def _bulk_load(self, batch: Sequence[tuple[Event, bytes]]) -> None:
        """Stage the batch as CSV and let DuckDB read it natively."""
        with tempfile.TemporaryDirectory(prefix="runopsy-ingest-") as directory:
            staged = Path(directory) / "batch.csv"
            with staged.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                for event, payload in batch:
                    row = list(_row(event, payload))
                    # The timestamp goes in as text because CSV has no types; DuckDB
                    # parses it back into the TIMESTAMP column on the way in.
                    row[6] = row[6].isoformat(sep=" ")
                    writer.writerow(row)
            self._connection.execute(
                f"COPY events FROM '{staged.as_posix()}' (FORMAT CSV, HEADER false, NULLSTR '')"
            )

    def _update_run(self, event: Event) -> None:
        """Keep the run row in step with lifecycle events.

        Only lifecycle events touch the runs table; everything else needs at most a
        placeholder row, and issuing that per step was a third of the ingest cost.

        Columns are written only by the event that owns them, so a ``run_end`` cannot
        blank out the task recorded at ``run_start`` if events arrive out of order.
        """
        if isinstance(event, RunStartEvent):
            self._connection.execute(
                "INSERT INTO runs (run_id, task, repo, runtime, provider, model, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id) DO UPDATE SET task = excluded.task, "
                "repo = excluded.repo, runtime = excluded.runtime, "
                "provider = excluded.provider, model = excluded.model, "
                "started_at = excluded.started_at",
                [
                    event.run_id,
                    event.run.task,
                    event.run.repo,
                    event.run.runtime,
                    event.run.provider,
                    event.run.model,
                    _to_storage(event.timestamp),
                ],
            )
        elif isinstance(event, RunEndEvent):
            self._connection.execute(
                "INSERT INTO runs (run_id, outcome, ended_at) VALUES (?, ?, ?) "
                "ON CONFLICT (run_id) DO UPDATE SET outcome = excluded.outcome, "
                "ended_at = excluded.ended_at",
                [event.run_id, event.run.outcome.value, _to_storage(event.timestamp)],
            )
        elif not self._seen_runs.__contains__(event.run_id):
            self._connection.execute(
                "INSERT INTO runs (run_id) VALUES (?) ON CONFLICT (run_id) DO NOTHING",
                [event.run_id],
            )
            self._seen_runs.add(event.run_id)

    def events(self, run_id: str) -> tuple[Event, ...]:
        """Every indexed event for a run, in execution order."""
        rows = self._connection.execute(
            "SELECT payload FROM events WHERE run_id = ? ORDER BY sequence, event_id",
            [run_id],
        ).fetchall()
        return tuple(_events.validate_python(orjson.loads(row[0])) for row in rows)

    def run(self, run_id: str) -> RunSummary | None:
        """Summary for one run, or ``None`` when it was never recorded."""
        return next((summary for summary in self.runs() if summary.run_id == run_id), None)

    def runs(self) -> tuple[RunSummary, ...]:
        """All runs, most recently started first."""
        rows = self._connection.execute(
            "SELECT r.run_id, r.task, r.repo, r.runtime, r.provider, r.model, r.outcome, "
            "r.started_at, r.ended_at, "
            "COUNT(e.event_id) AS event_count, "
            "COALESCE(SUM(CASE WHEN e.contains_secret THEN 1 ELSE 0 END), 0) AS secret_count "
            "FROM runs r LEFT JOIN events e ON e.run_id = r.run_id "
            "GROUP BY ALL "
            "ORDER BY r.started_at DESC NULLS LAST, r.run_id"
        ).fetchall()
        return tuple(
            RunSummary(
                run_id=row[0],
                task=row[1] or "",
                repo=row[2],
                runtime=row[3] or "unknown",
                provider=row[4],
                model=row[5],
                outcome=RunOutcome(row[6]),
                started_at=_from_storage(row[7]),
                ended_at=_from_storage(row[8]),
                event_count=row[9],
                secret_event_count=row[10],
            )
            for row in rows
        )

    # Deliberately no `next_sequence` here. It used to live in this class as
    # `SELECT MAX(sequence) + 1`, which is a read with nothing holding the value until
    # the caller writes. Adapters run a fresh process per event, so parallel subagents
    # raced it: two events took one number, an adapter turned that number into one event
    # id, and dedup removed the second. Allocation now belongs to
    # `runopsy_collector.sequence.SequenceAllocator`, which reserves under a file lock
    # and keeps working when the index is locked — which is when the race happens.

    def latest_run_id(self) -> str | None:
        """The run ``runopsy diagnose latest`` refers to."""
        runs = self.runs()
        return runs[0].run_id if runs else None

    def query(self, sql: str, parameters: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Escape hatch for ad-hoc SQL against the index."""
        return self._connection.execute(sql, parameters or []).fetchall()

    def payload_digests(
        self, *, only_runs: set[str] | None = None, exclude_runs: set[str] | None = None
    ) -> set[str]:
        """Every payload digest referenced by the selected runs.

        Read out of the stored event JSON rather than kept in a side table, because the
        journal is authoritative and a second copy of this mapping could drift from it.
        """
        rows = self._connection.execute(
            "SELECT run_id, payload FROM events WHERE payload LIKE '%sha256:%'"
        ).fetchall()

        digests: set[str] = set()
        for run_id, payload in rows:
            if only_runs is not None and run_id not in only_runs:
                continue
            if exclude_runs is not None and run_id in exclude_runs:
                continue
            try:
                event = _events.validate_python(orjson.loads(payload))
            except (orjson.JSONDecodeError, ValueError):
                continue
            digests.update(_digests_of(event))
        return digests

    def delete_run(self, run_id: str) -> int:
        """Remove one run from the index, returning how many events went with it."""
        row = self._connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", [run_id]
        ).fetchone()
        count = int(row[0]) if row else 0
        self._connection.execute("DELETE FROM events WHERE run_id = ?", [run_id])
        self._connection.execute("DELETE FROM runs WHERE run_id = ?", [run_id])
        self._seen_runs.discard(run_id)
        return count

    def reset(self) -> None:
        """Drop all indexed rows, leaving the journals untouched."""
        self._connection.execute("DELETE FROM events")
        self._connection.execute("DELETE FROM runs")
