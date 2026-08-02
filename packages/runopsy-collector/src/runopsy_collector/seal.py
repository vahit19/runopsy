"""Evidence that a journal has not been edited since it was recorded.

A diagnosis is an argument about what happened, and the journal is the evidence it rests
on. Until now the strongest thing that could be said about that evidence was that its
step numbers were contiguous — which catches a crashed adapter and catches nothing at all
about a file somebody opened and changed afterwards. A trace with one line quietly
rewritten produces a fluent, confident and completely wrong causal story, and every check
in the system would call it intact.

The seal is a rolling digest: each appended line is folded into the previous value, so
the final digest depends on every byte in the file and on their order. Inserting,
deleting, reordering or editing any line changes it, and no local recomputation can put
it back without also rewriting the seal.

**What this is not.** It is tamper *evidence*, not tamper *proofing*. Anyone who can edit
the journal can also delete the seal file, and a signature that survived that would need a
key this machine has nowhere safe to keep — a local-first tool cannot honestly claim
otherwise. What it does establish is that a trace handed to somebody else, or read back
after a week, is byte-for-byte the one that was recorded; and that accidental damage,
half-finished edits and well-meant "fixes" cannot pass unnoticed.

**Absence is not guilt.** A journal recorded before sealing existed, or imported from
another tool, has no seal and is reported as unsealed rather than as broken. Treating
"unknown" as "tampered" would make the check worthless within a week of shipping it.
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SEAL_NAME = ".seal"
GENESIS = "0" * 64
"""The value the chain starts from, so the first line has something to fold into."""


class SealState(StrEnum):
    """What a seal check concluded."""

    INTACT = "intact"
    BROKEN = "broken"
    UNSEALED = "unsealed"
    EMPTY = "empty"


@dataclass(frozen=True)
class SealVerdict:
    """The result of checking one journal against its seal."""

    state: SealState
    recorded: str | None = None
    computed: str | None = None
    lines: int = 0

    @property
    def is_trustworthy(self) -> bool:
        """Whether the journal is known not to have changed.

        An unsealed journal is not trustworthy in this sense and is not damaged either;
        the two are kept apart deliberately, because collapsing them would make every
        older trace look tampered with.
        """
        return self.state in (SealState.INTACT, SealState.EMPTY)

    def describe(self) -> str:
        match self.state:
            case SealState.INTACT:
                return f"{self.lines} events, unchanged since recording"
            case SealState.BROKEN:
                return (
                    f"{self.lines} events, but the seal does not match — this journal "
                    "has been modified since it was recorded"
                )
            case SealState.UNSEALED:
                return f"{self.lines} events, not sealed (recorded before sealing, or imported)"
            case _:
                return "no events"


def fold(previous: str, line: bytes) -> str:
    """Fold one journal line into the running digest.

    Order matters as much as content: the previous value is part of what is hashed, so
    two events swapped produce a different chain even though the same bytes are present.
    """
    return hashlib.sha256(previous.encode("ascii") + line).hexdigest()


def compute(payload: bytes, *, start: str = GENESIS) -> tuple[str, int]:
    """Fold every line of ``payload`` in order, returning the digest and line count."""
    digest = start
    counted = 0
    for line in payload.splitlines():
        if not line.strip():
            continue
        digest = fold(digest, line)
        counted += 1
    return digest, counted


class Seal:
    """The recorded digest for one run's journal."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / SEAL_NAME

    def read(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def extend(self, payload: bytes) -> str:
        """Fold newly appended bytes into the seal and store the result.

        The caller holds the journal's lock, so this and the append it belongs to are one
        operation as far as any other process is concerned. Without that the seal could
        advance past bytes a competing writer had not yet written, and a correct journal
        would then read as broken — the recorder accusing itself.
        """
        digest, _ = compute(payload, start=self.read() or GENESIS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(digest, encoding="utf-8")
        return digest

    def reset(self) -> None:
        """Forget the seal. Used when a run's journal is removed."""
        with contextlib.suppress(OSError):
            self.path.unlink()

    def verify(self, journal_path: Path) -> SealVerdict:
        """Recompute the chain over the journal on disk and compare."""
        try:
            payload = journal_path.read_bytes()
        except OSError:
            return SealVerdict(SealState.EMPTY)

        computed, lines = compute(payload)
        if lines == 0:
            return SealVerdict(SealState.EMPTY)

        recorded = self.read()
        if recorded is None:
            return SealVerdict(SealState.UNSEALED, computed=computed, lines=lines)
        if recorded == computed:
            return SealVerdict(SealState.INTACT, recorded, computed, lines)
        return SealVerdict(SealState.BROKEN, recorded, computed, lines)


__all__ = ["GENESIS", "SEAL_NAME", "Seal", "SealState", "SealVerdict", "compute", "fold"]
