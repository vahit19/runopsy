"""Trace integrity checks.

A diagnosis is only as trustworthy as the trace under it. If events were dropped by a
crashed adapter, duplicated by a retried write, or reordered on ingest, the engine can
still produce a fluent and completely wrong causal story. These checks make that
condition visible so the CLI can say "this trace has a gap at step 12" instead of
silently blaming whatever step happens to sit next to the hole.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from runopsy_core.schema.events import BaseEvent


class IntegrityReport(BaseModel):
    """Result of checking one run's event stream."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    event_count: int = Field(ge=0)
    missing_sequences: tuple[int, ...] = ()
    duplicate_sequences: tuple[int, ...] = ()
    out_of_order: bool = False
    foreign_run_ids: tuple[str, ...] = ()

    @property
    def is_intact(self) -> bool:
        """Whether the stream is complete, unique, ordered and single-run."""
        return not (
            self.missing_sequences
            or self.duplicate_sequences
            or self.out_of_order
            or self.foreign_run_ids
        )

    def describe(self) -> str:
        """A short human-readable verdict for CLI output."""
        if self.is_intact:
            return f"{self.event_count} events, intact"
        problems: list[str] = []
        if self.missing_sequences:
            problems.append(f"missing {len(self.missing_sequences)}")
        if self.duplicate_sequences:
            problems.append(f"duplicated {len(self.duplicate_sequences)}")
        if self.out_of_order:
            problems.append("out of order")
        if self.foreign_run_ids:
            problems.append(f"{len(self.foreign_run_ids)} foreign run ids")
        return f"{self.event_count} events, {', '.join(problems)}"


def check_integrity(run_id: str, events: Iterable[BaseEvent]) -> IntegrityReport:
    """Check that ``events`` form a complete, ordered stream for ``run_id``.

    Sequences are expected to be contiguous from the lowest observed value, rather than
    forced to start at zero, so a deliberately trimmed window can still be checked.
    """
    sequences: list[int] = []
    seen: set[int] = set()
    duplicates: set[int] = set()
    foreign: list[str] = []
    ordered = True
    previous: int | None = None

    for event in events:
        if event.run_id != run_id:
            foreign.append(event.run_id)
            continue
        sequences.append(event.sequence)
        if event.sequence in seen:
            duplicates.add(event.sequence)
        seen.add(event.sequence)
        if previous is not None and event.sequence < previous:
            ordered = False
        previous = event.sequence

    missing: tuple[int, ...] = ()
    if sequences:
        expected = range(min(sequences), max(sequences) + 1)
        missing = tuple(value for value in expected if value not in seen)

    return IntegrityReport(
        run_id=run_id,
        event_count=len(sequences),
        missing_sequences=missing,
        duplicate_sequences=tuple(sorted(duplicates)),
        out_of_order=not ordered,
        foreign_run_ids=tuple(dict.fromkeys(foreign)),
    )
