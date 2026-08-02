"""Append-only event journal.

One JSONL file per run, written with sorted keys so a given run always serializes to
identical bytes. That determinism is what makes ``cache_by_trace_hash`` possible: an
unchanged run hashes the same, so diagnosis results can be reused instead of paying for
the analysis twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import orjson
from pydantic import TypeAdapter, ValidationError

from runopsy_collector.seal import Seal, SealVerdict
from runopsy_collector.sequence import LOCK_NAME, exclusive
from runopsy_core.schema import Event

_events: TypeAdapter[Event] = TypeAdapter(Event)
_SERIALIZE_OPTIONS = orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE


class JournalCorruptionError(RuntimeError):
    """Raised when a journal line cannot be read as an event.

    Surfaced rather than skipped: silently dropping unreadable lines would produce a
    trace with an invisible hole, and a hole in the trace becomes a wrong answer about
    where a run started failing.
    """

    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"{path}:{line_number}: {reason}")
        self.path = path
        self.line_number = line_number


def serialize(event: Event) -> bytes:
    """Render one event as a deterministic JSON line, newline included."""
    return orjson.dumps(_events.dump_python(event, mode="json"), option=_SERIALIZE_OPTIONS)


class EventJournal:
    """The append-only record of one run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: Event) -> None:
        """Append a single event, creating the run directory on first write."""
        self.append_all([event])

    def append_all(self, events: Iterable[Event]) -> int:
        """Append several events in one open, returning how many were written.

        The file is opened in append mode and flushed on close, so a crash truncates at
        a line boundary at worst; readers stop at the last complete line.

        The write and the seal update happen inside one cross-process lock. They have to:
        a hook is a fresh process per event, and an agent with parallel subagents runs
        several at once, so without the lock two writers could fold their bytes into the
        chain in one order and write them to the file in another. The journal would be
        perfectly correct and the seal would call it modified — the recorder accusing
        itself, which is the worst possible failure for a check whose whole purpose is to
        be believed.
        """
        payload = b"".join(serialize(event) for event in events)
        if not payload:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive(self.path.parent / LOCK_NAME):
            with self.path.open("ab") as handle:
                handle.write(payload)
            Seal(self.path.parent).extend(payload)
        return payload.count(b"\n")

    def verify(self) -> SealVerdict:
        """Whether this journal is byte-for-byte the one that was recorded."""
        return Seal(self.path.parent).verify(self.path)

    def read(self) -> Iterator[Event]:
        """Yield every event in write order.

        A missing journal yields nothing rather than raising: a run that was never
        recorded is an empty history, not an error condition.
        """
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield _events.validate_python(orjson.loads(line))
                except (orjson.JSONDecodeError, ValidationError) as error:
                    raise JournalCorruptionError(self.path, line_number, str(error)) from error

    def count(self) -> int:
        """Number of events on disk, without validating each one."""
        if not self.path.exists():
            return 0
        with self.path.open("rb") as handle:
            return sum(1 for line in handle if line.strip())
