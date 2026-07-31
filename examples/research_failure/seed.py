"""A research run that answered confidently from something it never checked.

An agent is asked which database version the team is on. Its first search returns
nothing useful, so at step 4 it recalls a note from memory — written eleven months ago —
and at step 6 asserts a version from it without ever fetching a page to confirm. The
claim is recorded as unsupported. Nothing errors. The run "succeeds", the report is
written, and the migration built on that number fails in a later run nobody has yet
connected to this one.

This is the failure the design document calls *validation*, and it is the shape that
makes agent failures hard: every step returned zero. There is no exception, no non-zero
exit, no timeout. What went wrong is that a claim outran its evidence, and a stale
memory was treated as a source.

Two detectors have something to say here — the stale recall at step 4 and the
unsupported claim at step 6 — which is the point: the visible symptom is the claim, and
the onset is the recall that fed it.

    uv run python examples/research_failure/seed.py
    uv run runopsy diagnose --store .runopsy-research
    uv run runopsy evidence --step 4 --store .runopsy-research
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runopsy_collector import Collector
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    ClaimEvent,
    ClaimPayload,
    Event,
    MemoryOperation,
    MemoryOpEvent,
    MemoryPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    SupportStatus,
    ToolCallEvent,
    ToolPayload,
)

RUN_ID = "run_research_0013"
START = datetime(2026, 7, 31, 14, 20, tzinfo=UTC)
STORE = Path(".runopsy-research")

ELEVEN_MONTHS = 60 * 60 * 24 * 330.0


def _at(sequence: int) -> datetime:
    return START + timedelta(seconds=sequence * 9)


def _tool(sequence: int, name: str, *, arguments: str, output: str) -> Event:
    """Every tool call here succeeds. That is the whole difficulty."""
    return ToolCallEvent(
        event_id=f"{RUN_ID}_evt_{sequence:02d}",
        run_id=RUN_ID,
        sequence=sequence,
        timestamp=_at(sequence),
        tool=ToolPayload(
            name=name,
            exit_code=0,
            status=CallStatus.OK,
            arguments_hash=hash_text(arguments),
            output_hash=hash_text(output),
            duration_ms=600 + sequence * 25,
        ),
    )


def trace() -> list[Event]:
    """The run, as a runtime adapter would have recorded it."""
    return [
        RunStartEvent(
            event_id=f"{RUN_ID}_evt_00",
            run_id=RUN_ID,
            sequence=0,
            timestamp=_at(0),
            run=RunPayload(
                task="confirm which PostgreSQL version production runs, for the migration plan",
                repo="platform",
                runtime="hermes",
                provider="openrouter",
                model="openai/gpt-4o-mini",
            ),
        ),
        _tool(1, "web_search", arguments="postgres version production", output="no relevant hits"),
        _tool(2, "read_file", arguments="docs/infra.md", output="see the runbook"),
        _tool(3, "web_search", arguments="platform runbook postgres", output="404 — page moved"),
        # The run breaks here: a note written eleven months ago is recalled and,
        # from this point on, treated as if it were a checked source.
        MemoryOpEvent(
            event_id=f"{RUN_ID}_evt_04",
            run_id=RUN_ID,
            sequence=4,
            timestamp=_at(4),
            memory=MemoryPayload(
                operation=MemoryOperation.READ,
                key="infra.postgres.version",
                source="agent notes",
                value_hash=hash_text("postgres 13.4"),
                age_seconds=ELEVEN_MONTHS,
            ),
        ),
        _tool(5, "write_file", arguments="notes/migration-scratch.md", output="written"),
        # The visible symptom: an assertion with nothing behind it.
        ClaimEvent(
            event_id=f"{RUN_ID}_evt_06",
            run_id=RUN_ID,
            sequence=6,
            timestamp=_at(6),
            claim=ClaimPayload(
                claim_id="claim_pg_version",
                text_hash=hash_text("production runs PostgreSQL 13.4"),
                support_status=SupportStatus.UNSUPPORTED,
            ),
        ),
        _tool(7, "write_file", arguments="docs/migration-plan.md", output="written"),
        _tool(8, "commit", arguments="docs: add migration plan", output="committed"),
        RunEndEvent(
            event_id=f"{RUN_ID}_evt_09",
            run_id=RUN_ID,
            sequence=9,
            timestamp=_at(9),
            run=RunPayload(
                outcome=RunOutcome.SUCCESS,
                summary="migration plan written",
            ),
        ),
    ]


def main() -> int:
    with Collector.open(STORE) as collector:
        recorded = collector.record_all(trace())

    print(f"Recorded {recorded} events into {STORE}/ as {RUN_ID}.")
    print()
    print("Every tool call succeeded and the run reports success.")
    print("What went wrong is that a claim outran its evidence, from a stale recall.")
    print()
    print(f"  runopsy diagnose --store {STORE}")
    print(f"  runopsy evidence --step 4 --store {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
