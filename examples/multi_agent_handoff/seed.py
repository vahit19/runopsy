"""A handoff that dropped half the brief.

A coordinator splits a migration between two subagents. The first finishes and reports
back properly. The second returns with no summary at all — Hermes records the child
stopping, but the child said nothing about what it did or did not do. The coordinator
treats silence as success, writes the migration note, and the deploy check fails four
steps later on a table nobody migrated.

Read from the end, the culprit is the deploy check. The run broke at the handoff.

This is the failure class the design document calls *handoff*, and it is the one that
tool-level tracing misses entirely: nothing errored. A subagent exited, a tool returned
zero, and the information simply was not there.

    uv run python examples/multi_agent_handoff/seed.py
    uv run runopsy diagnose --store .runopsy-handoff
    uv run runopsy evidence --step 6 --store .runopsy-handoff
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runopsy_collector import Collector
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    Event,
    HandoffEvent,
    HandoffPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    StateChange,
    ToolCallEvent,
    ToolPayload,
)

RUN_ID = "run_handoff_0007"
START = datetime(2026, 7, 31, 11, 0, tzinfo=UTC)
STORE = Path(".runopsy-handoff")


def _at(sequence: int) -> datetime:
    return START + timedelta(seconds=sequence * 11)


def _tool(
    sequence: int,
    name: str,
    *,
    exit_code: int = 0,
    arguments: str = "",
    state: dict[str, object] | None = None,
) -> Event:
    return ToolCallEvent(
        event_id=f"{RUN_ID}_evt_{sequence:02d}",
        run_id=RUN_ID,
        sequence=sequence,
        timestamp=_at(sequence),
        tool=ToolPayload(
            name=name,
            exit_code=exit_code,
            status=CallStatus.ERROR if exit_code else CallStatus.OK,
            arguments_hash=hash_text(arguments or name),
            output_hash=hash_text(f"{name}:{sequence}:out"),
            duration_ms=500 + sequence * 40,
        ),
        state_delta={key: StateChange(after=value) for key, value in (state or {}).items()},
    )


def _handoff(sequence: int, child: str, *, summary: str | None) -> Event:
    """A subagent stopping. Without a summary the parent has been told nothing."""
    return HandoffEvent(
        event_id=f"{RUN_ID}_evt_{sequence:02d}",
        run_id=RUN_ID,
        sequence=sequence,
        timestamp=_at(sequence),
        handoff=HandoffPayload(
            from_agent_id="coordinator",
            to_agent_id=child,
            context_hash=hash_text(summary) if summary else None,
            missing_fields=() if summary else ("child_summary",),
        ),
    )


def trace() -> list[Event]:
    """The run, as the Hermes adapter would have recorded it."""
    return [
        RunStartEvent(
            event_id=f"{RUN_ID}_evt_00",
            run_id=RUN_ID,
            sequence=0,
            timestamp=_at(0),
            run=RunPayload(
                task="split the schema migration across two workers and verify it",
                repo="billing",
                runtime="hermes",
                provider="openrouter",
                model="openai/gpt-4o-mini",
            ),
        ),
        _tool(1, "read_file", arguments="migrations/plan.md"),
        _tool(2, "delegate", arguments="worker-a: migrate invoices"),
        _tool(3, "delegate", arguments="worker-b: migrate subscriptions"),
        # Worker A reports back properly.
        _handoff(4, "worker-a", summary="migrated invoices; 3 tables, all verified"),
        _tool(5, "read_file", arguments="migrations/status.json"),
        # The run breaks here. Worker B stops having said nothing at all, and every
        # tool call around it succeeded — there is no exit code to blame.
        _handoff(6, "worker-b", summary=None),
        _tool(
            7,
            "write_file",
            arguments="migrations/NOTES.md: all workers reported complete",
            state={"migration.state": "believed-complete"},
        ),
        _tool(8, "commit", arguments="chore: record migration completion"),
        _tool(9, "deploy_check", arguments="--env staging"),
        _tool(10, "deploy_check", exit_code=1, arguments="--env staging"),
        RunEndEvent(
            event_id=f"{RUN_ID}_evt_11",
            run_id=RUN_ID,
            sequence=11,
            timestamp=_at(11),
            run=RunPayload(outcome=RunOutcome.FAILURE, summary="staging check failed"),
        ),
    ]


def main() -> int:
    with Collector.open(STORE) as collector:
        recorded = collector.record_all(trace())

    print(f"Recorded {recorded} events into {STORE}/ as {RUN_ID}.")
    print()
    print("The visible failure is the deploy check at step 10.")
    print("The run broke at step 6, where a subagent returned nothing.")
    print()
    print(f"  runopsy diagnose --store {STORE}")
    print(f"  runopsy evidence --step 6 --store {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
