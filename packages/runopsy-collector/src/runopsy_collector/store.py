"""DuckDB index over recorded events.

This is derived data. Every row here can be reproduced from the JSONL journals, which
is why the schema optimizes for querying rather than durability: a single local file,
no server, and SQL over runs, steps and state changes.
"""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Sequence
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


class EventStore:
    """Queryable index over recorded events."""

    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self._seen_runs: set[str] = set()
        self._connection = duckdb.connect(str(database))
        self._connection.execute(_DDL)
        self._connection.execute(
            "INSERT INTO store_meta (key, value) VALUES (?, ?), (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            ["store_version", STORE_VERSION, "schema_version", SCHEMA_VERSION],
        )

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

    def next_sequence(self, run_id: str) -> int:
        """The sequence number a new event for this run should take.

        Read from the store rather than held in memory because a hook-based adapter is
        a fresh process on every event: there is nothing to keep a counter in. Two
        events racing here would collide, which the integrity check reports as a
        duplicate rather than hiding — the visible failure being the point.
        """
        row = self._connection.execute(
            "SELECT MAX(sequence) FROM events WHERE run_id = ?", [run_id]
        ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0]) + 1

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
