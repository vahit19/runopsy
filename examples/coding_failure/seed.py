"""Seed the demo trace from section C of the design document.

An agent is asked to fix a failing integration test. It writes a config for the wrong
environment at step 9 — which fails, but the agent carries on — edits files, restarts the
service, and only at step 14 does the test suite fail. Read from the end, the obvious
culprit is the test. The run actually broke five steps earlier.

Run it, then diagnose it:

    uv run python examples/coding_failure/seed.py
    uv run runopsy diagnose --store .runopsy-demo
    uv run runopsy evidence --step 9 --store .runopsy-demo
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
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    StateChange,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

RUN_ID = "run_demo_0042"
START = datetime(2026, 7, 30, 9, 45, tzinfo=UTC)
STORE = Path(".runopsy-demo")


def _at(sequence: int) -> datetime:
    return START + timedelta(seconds=sequence * 7)


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
            duration_ms=400 + sequence * 30,
        ),
        state_delta={key: StateChange(after=value) for key, value in (state or {}).items()},
    )


def _think(sequence: int) -> Event:
    return LlmCallEvent(
        event_id=f"{RUN_ID}_evt_{sequence:02d}",
        run_id=RUN_ID,
        sequence=sequence,
        timestamp=_at(sequence),
        llm=LlmPayload(
            model="local:qwen2.5-coder",
            tokens=TokenUsage(input_tokens=1800, output_tokens=260),
            latency_ms=900,
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
                task="fix the failing integration test in the payments service",
                repo="payments",
                runtime="hermes",
                provider="local",
                model="local:qwen2.5-coder",
            ),
        ),
        _think(1),
        _tool(2, "read_file", arguments="tests/test_checkout.py"),
        _tool(3, "read_file", arguments="src/payments/client.py"),
        _think(4),
        _tool(5, "grep", arguments="endpoint"),
        _tool(6, "read_file", arguments="config/test.yaml"),
        _think(7),
        _tool(8, "checkpoint", arguments="before config edit"),
        # The run breaks here: the write fails and the environment is left wrong.
        _tool(
            9,
            "write_config",
            exit_code=1,
            arguments="config/test.yaml endpoint=https://staging.internal",
            state={"endpoint": "staging", "config_written": False},
        ),
        _think(10),
        _tool(11, "edit_file", arguments="src/payments/client.py timeout"),
        _tool(12, "restart_service", arguments="payments"),
        _tool(13, "edit_file", arguments="tests/test_checkout.py fixture"),
        # ... and only now does anything visibly go wrong.
        _tool(14, "pytest", exit_code=1, arguments="tests/test_checkout.py -x"),
        RunEndEvent(
            event_id=f"{RUN_ID}_evt_15",
            run_id=RUN_ID,
            sequence=15,
            timestamp=_at(15),
            run=RunPayload(
                outcome=RunOutcome.FAILURE,
                summary="integration test still failing after 14 steps",
            ),
        ),
    ]


def main() -> int:
    with Collector.open(STORE) as collector:
        recorded = collector.record_all(trace())
        report = collector.integrity(RUN_ID)

    print(f"Recorded {recorded} events for {RUN_ID} in {STORE}/ ({report.describe()}).")
    print("\nNow diagnose it:")
    print(f"  uv run runopsy diagnose --store {STORE}")
    print(f"  uv run runopsy evidence --step 9 --store {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
