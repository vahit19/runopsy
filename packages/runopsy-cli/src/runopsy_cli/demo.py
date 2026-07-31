"""A worked example that ships inside the package.

The repository has `examples/coding_failure/seed.py`, and the README told people to run
it — which works if you cloned the repository and does nothing at all if you installed
from PyPI, because `examples/` is in no wheel. So the one thing a first-time user wants,
*show me what this does*, had no answer on a fresh install.

This is that answer. It builds the same trace in memory, so `runopsy demo` works
seconds after `pip install runopsy`, with no repository, no agent, no key and no
configuration.

The trace is the scenario from the design document, and it is chosen because it is the
whole product in fourteen steps: the run visibly fails at step 14, and it actually broke
at step 9 where a config was written for the wrong environment. Read from the end — the
way a log is read — you go and look at the test. That is the mistake this tool exists to
stop somebody making.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

RUN_ID = "demo_run"
ONSET_STEP = 9
SYMPTOM_STEP = 14

# A fixed clock: the demo trace is a constant, so diagnosing it twice gives the same
# answer and the numbers in the documentation stay true.
START = datetime(2026, 7, 30, 9, 45, tzinfo=UTC)


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
            output_hash=hash_text(f"{name}:{sequence}"),
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
    """An agent asked to fix a failing test, which breaks the environment instead."""
    return [
        RunStartEvent(
            event_id=f"{RUN_ID}_evt_00",
            run_id=RUN_ID,
            sequence=0,
            timestamp=_at(0),
            run=RunPayload(
                task="fix the failing integration test in the payments service",
                repo="payments",
                runtime="demo",
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
        # Here. The write fails, the agent carries on, and the environment is now wrong.
        _tool(
            ONSET_STEP,
            "write_config",
            exit_code=1,
            arguments="config/test.yaml env=production",
            state={"config.env": "production"},
        ),
        _tool(10, "edit_file", arguments="src/payments/client.py"),
        _tool(11, "restart_service", arguments="payments"),
        _think(12),
        _tool(13, "curl", arguments="http://localhost:8080/health"),
        # ...and only now does anything look wrong, five steps later.
        _tool(SYMPTOM_STEP, "pytest", exit_code=1, arguments="tests/test_checkout.py"),
        RunEndEvent(
            event_id=f"{RUN_ID}_evt_15",
            run_id=RUN_ID,
            sequence=15,
            timestamp=_at(15),
            run=RunPayload(outcome=RunOutcome.FAILURE, summary="integration test still failing"),
        ),
    ]
