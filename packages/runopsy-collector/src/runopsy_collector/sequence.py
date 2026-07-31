"""Handing out step numbers that stay unique when several processes record at once.

A sequence number is a step's position in its run, and an adapter builds the event id
straight out of it — ``{run_id}_evt_{sequence:04d}``. That makes allocation an identity
decision rather than a cosmetic one: two events given the same number are the same event
as far as every downstream layer can tell, so the second is deduplicated away and a step
of history disappears without anything reporting a loss.

Which is what happened. The number used to come from ``SELECT MAX(sequence)`` on the
index, a read and a write with no lock between them. A hook-based adapter is a fresh
process per event, and an agent delegating to parallel subagents fires several of them at
one run within milliseconds. Measured here: thirty-two concurrent steps, thirty survived,
and the integrity report described the result as a duplicate sequence rather than as two
steps of a trace that no longer exist.

Reserving through an exclusive file lock closes the window. The lock is held by the
operating system, so a process that dies mid-reservation releases it rather than wedging
every later hook — the one property an advisory lock file of our own could not offer.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO

COUNTER_NAME = ".sequence"
LOCK_NAME = ".sequence.lock"


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for the duration of the block.

    Both implementations block until the lock is free and both are released by the
    kernel when the file closes, including on an abrupt exit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[bytes] = path.open("ab+")
    try:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only

    def _lock(handle: IO[bytes]) -> None:
        import msvcrt

        handle.seek(0)
        # LK_LOCK retries about ten times over ten seconds before raising, which is the
        # right shape here: contention is measured in milliseconds, so a wait this long
        # means something is genuinely wrong rather than merely busy.
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        import msvcrt

        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - exercised on POSIX only

    def _lock(handle: IO[bytes]) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: IO[bytes]) -> None:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SequenceAllocator:
    """The next free step number for one run, safe to call from several processes."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.counter = run_dir / COUNTER_NAME
        self.lock = run_dir / LOCK_NAME

    def reserve(self, count: int = 1) -> int:
        """Take ``count`` consecutive numbers and return the first.

        Reserved rather than merely read: the counter advances before the caller writes
        anything, so no second process can be handed the same number while this one is
        still serializing its event. A caller that dies in between leaves a gap, which
        the integrity report names honestly as a missing sequence — a gap is a visible
        absence, where a collision was an invisible one.
        """
        if count < 1:
            msg = f"count must be positive, got {count}"
            raise ValueError(msg)
        with _exclusive(self.lock):
            start = self._read_or_seed()
            self.counter.write_text(str(start + count), encoding="utf-8")
            return start

    def _read_or_seed(self) -> int:
        """The current value, recovered from the journal when there is no counter.

        The counter is derived data, like the index: deleting it, or opening a store
        recorded before it existed, must not restart numbering at zero and overwrite the
        run's history. The journal is what it is derived from, so that is where a missing
        counter is rebuilt from.
        """
        try:
            return int(self.counter.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return self._highest_in_journal() + 1

    def _highest_in_journal(self) -> int:
        """The largest sequence already on disk, or -1 for an unrecorded run.

        Parsed with a plain scan rather than by validating each event: this runs on the
        recording path, where a full model parse of the whole run would be paid on every
        hook, and all that is needed is one integer per line.
        """
        journal = self.run_dir / "events.jsonl"
        if not journal.exists():
            return -1

        import orjson

        highest = -1
        with journal.open("rb") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = orjson.loads(line).get("sequence")
                except orjson.JSONDecodeError:
                    # A truncated final line from an interrupted write. Skipping it is
                    # right here — this is the allocation path, and refusing to hand out
                    # a number would stop the run being recorded at all. The integrity
                    # check is where corruption is meant to be reported.
                    continue
                if isinstance(value, int) and value > highest:
                    highest = value
        return highest


def allocator_for(run_dir: Path) -> SequenceAllocator:
    """Convenience for callers holding a path rather than a store."""
    return SequenceAllocator(run_dir)


__all__ = ["COUNTER_NAME", "LOCK_NAME", "SequenceAllocator", "allocator_for"]
