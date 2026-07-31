"""Turning a real run into a labelled case.

The design document calls the labelled failure corpus the project's defensible asset —
not the visualisation, not the graph layout, both of which anyone can rebuild in a
weekend. What cannot be copied is a body of real agent failures where a person has said
*this is the step where it actually went wrong*, because that only accrues by being
used.

Nothing connected the two halves. Twenty synthetic cases lived in Python and real runs
lived in a store, and there was no path from "I had a failure this morning" to "the
benchmark now contains it". This module is that path.

Three properties matter more than convenience here:

**A label is a human claim, and is stored as one.** The onset is whatever the person
says it is, with their name attached. It is never inferred from what the engine already
found — a corpus scored against the engine's own opinion measures nothing.

**Cases are portable and readable.** JSON on disk, not pickles: a case has to survive a
schema change, a review on a pull request, and a person disagreeing with the label.

**Content never travels.** A case carries the same hashes the trace carries and no
payload text, so contributing a failure does not mean contributing your source code.
That is what makes an opt-in corpus something a company could actually agree to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from runopsy_bench.cases import SyntheticCase
from runopsy_core.schema import Event, FailureCategory

CORPUS_VERSION = 1
"""Bumped when the on-disk shape changes in a way a reader must notice."""

_EVENTS = TypeAdapter(list[Event])


@dataclass(frozen=True)
class LabelledRun:
    """A real run, plus what a person said about where it went wrong."""

    name: str
    category: FailureCategory
    description: str
    events: tuple[Event, ...]
    onset_step: int | None
    """The step the labeller says it started going wrong. ``None`` for a healthy run."""

    affected_steps: frozenset[int] = frozenset()
    labelled_by: str = ""
    labelled_at: str = ""
    runtime: str = ""
    notes: str = ""
    deterministically_detectable: bool = True

    def as_case(self) -> SyntheticCase:
        """The same thing in the shape the scorer already understands."""
        return SyntheticCase(
            name=self.name,
            category=self.category,
            description=self.description,
            events=self.events,
            onset_step=self.onset_step,
            deterministically_detectable=self.deterministically_detectable,
            affected_steps=self.affected_steps,
        )


class LabelError(ValueError):
    """A label that cannot be trusted, with the reason a person can act on."""


def label_run(
    events: list[Event] | tuple[Event, ...],
    *,
    name: str,
    category: FailureCategory,
    description: str,
    onset_step: int | None,
    affected_steps: frozenset[int] | set[int] | None = None,
    labelled_by: str,
    runtime: str = "",
    notes: str = "",
    detectable: bool = True,
    now: datetime | None = None,
) -> LabelledRun:
    """Attach a human label to a recorded run.

    ``labelled_by`` is required and not defaulted. A case with no named labeller is a
    claim nobody stands behind, and the whole value of the corpus is that somebody does
    — the same reason ``human_verified`` needs a verifier before the engine will say it.
    """
    ordered = tuple(sorted(events, key=lambda event: event.sequence))
    if not ordered:
        msg = "a case needs at least one event"
        raise LabelError(msg)
    if not labelled_by.strip():
        msg = "labelled_by is required: an unattributed label is not evidence"
        raise LabelError(msg)

    steps = {event.sequence for event in ordered}
    if onset_step is not None and onset_step not in steps:
        msg = f"step {onset_step} is not in this run (it has {min(steps)}..{max(steps)})"
        raise LabelError(msg)

    affected = frozenset(affected_steps or ())
    unknown = affected - steps
    if unknown:
        msg = f"affected steps not in this run: {sorted(unknown)}"
        raise LabelError(msg)
    if onset_step is not None and any(step <= onset_step for step in affected):
        # The same invariant the impact layer holds: nothing may affect the past.
        msg = "affected steps must come after the onset"
        raise LabelError(msg)

    stamped = (now or datetime.now(UTC)).replace(microsecond=0)
    return LabelledRun(
        name=name,
        category=category,
        description=description,
        events=ordered,
        onset_step=onset_step,
        affected_steps=affected,
        labelled_by=labelled_by.strip(),
        labelled_at=stamped.isoformat(),
        runtime=runtime,
        notes=notes,
        deterministically_detectable=detectable,
    )


def to_json(case: LabelledRun) -> str:
    """Serialise a case for the corpus directory."""
    document: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "name": case.name,
        "category": case.category.value,
        "description": case.description,
        "onset_step": case.onset_step,
        "affected_steps": sorted(case.affected_steps),
        "deterministically_detectable": case.deterministically_detectable,
        "labelled_by": case.labelled_by,
        "labelled_at": case.labelled_at,
        "runtime": case.runtime,
        "notes": case.notes,
        "events": [json.loads(event.model_dump_json()) for event in case.events],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def from_json(text: str) -> LabelledRun:
    """Read a case back, refusing one written by a newer format."""
    document = json.loads(text)
    version = int(document.get("corpus_version", 0))
    if version > CORPUS_VERSION:
        msg = f"case needs corpus version {version}; this build understands {CORPUS_VERSION}"
        raise LabelError(msg)

    return LabelledRun(
        name=str(document["name"]),
        category=FailureCategory(document["category"]),
        description=str(document.get("description", "")),
        events=tuple(_EVENTS.validate_python(document["events"])),
        onset_step=document.get("onset_step"),
        affected_steps=frozenset(document.get("affected_steps") or ()),
        labelled_by=str(document.get("labelled_by", "")),
        labelled_at=str(document.get("labelled_at", "")),
        runtime=str(document.get("runtime", "")),
        notes=str(document.get("notes", "")),
        deterministically_detectable=bool(document.get("deterministically_detectable", True)),
    )


def load_corpus(directory: Path) -> tuple[LabelledRun, ...]:
    """Every labelled case in a directory, in a stable order.

    Sorted by filename so a benchmark run over a corpus is reproducible, and so a
    report diff shows a real change rather than a reshuffle.
    """
    if not directory.is_dir():
        return ()
    cases = []
    for path in sorted(directory.glob("*.json")):
        cases.append(from_json(path.read_text(encoding="utf-8")))
    return tuple(cases)


def carries_payload_text(case: LabelledRun) -> bool:
    """Whether anything in this case looks like content rather than a digest.

    Contributing a failure must not mean contributing your source code. The trace
    schema already stores hashes, so this is a belt-and-braces check before a case
    leaves the machine — cheap, and the cost of being wrong is somebody's repository.
    """
    for event in case.events:
        for value in json.loads(event.model_dump_json()).values():
            if _looks_like_content(value):
                return True
    return False


def _looks_like_content(value: object) -> bool:
    if isinstance(value, dict):
        return any(_looks_like_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_looks_like_content(item) for item in value)
    if not isinstance(value, str):
        return False
    # A digest is fine; long free text is not. Tool names, models and keys are short.
    return not value.startswith("sha256:") and len(value) > 512
