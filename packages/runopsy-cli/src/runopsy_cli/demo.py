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


_TEXTS: list[str] = []
"""Every command and output the demo hashes, so the vault can hold the originals.

The demo used to hash text it never stored, which made `runopsy evidence` on the demo
run say "not kept locally" — the product's central claim, that a finding can be traced
back to the thing that happened, going unillustrated at the exact moment a new user looks
for it. The texts are collected as the trace is built so the two cannot drift apart.
"""


def _tool(
    sequence: int,
    name: str,
    *,
    exit_code: int = 0,
    arguments: str = "",
    output: str = "",
    state: dict[str, object] | None = None,
) -> Event:
    command = arguments or name
    result = output or f"{name}: ok"
    _TEXTS.extend((command, result))
    return ToolCallEvent(
        event_id=f"{RUN_ID}_evt_{sequence:02d}",
        run_id=RUN_ID,
        sequence=sequence,
        timestamp=_at(sequence),
        tool=ToolPayload(
            name=name,
            exit_code=exit_code,
            status=CallStatus.ERROR if exit_code else CallStatus.OK,
            arguments_hash=hash_text(command),
            output_hash=hash_text(result),
            duration_ms=400 + sequence * 30,
        ),
        state_delta={key: StateChange(after=value) for key, value in (state or {}).items()},
    )


def payload_texts() -> tuple[str, ...]:
    """The originals behind the hashes, for the vault.

    Built by calling :func:`trace`, because the texts are registered as the events are
    constructed — asking for them without building the trace would return an empty vault
    and silently reproduce the defect this exists to fix.
    """
    _TEXTS.clear()
    trace()
    return tuple(dict.fromkeys(_TEXTS))


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
        _tool(
            2,
            "read_file",
            arguments="cat tests/test_checkout.py",
            output="def test_checkout_charges_once():\n    assert charge(order).status == 'paid'",
        ),
        _tool(
            3,
            "read_file",
            arguments="cat src/payments/client.py",
            output="ENDPOINT = os.environ['PAYMENTS_ENDPOINT']\ndef charge(order): ...",
        ),
        _think(4),
        _tool(
            5,
            "grep",
            arguments="grep -rn endpoint config/",
            output=(
                "config/test.yaml:3:  endpoint: http://localhost:8080\n"
                "config/prod.yaml:3:  endpoint: https://api.example.com"
            ),
        ),
        _tool(
            6,
            "read_file",
            arguments="cat config/test.yaml",
            output="env: test\nendpoint: http://localhost:8080\ntimeout: 5",
        ),
        _think(7),
        _tool(
            8,
            "checkpoint",
            arguments="checkpoint before config edit",
            output="saved checkpoint ck_8 (working tree clean)",
        ),
        # Here. The write fails, the agent carries on, and the environment is now wrong.
        _tool(
            ONSET_STEP,
            "write_config",
            exit_code=1,
            arguments="write_config config/test.yaml env=production",
            output=(
                "error: refusing to set env=production in a test config\n"
                "wrote endpoint: https://api.example.com before failing\n"
                "exit status 1"
            ),
            state={"config.env": "production"},
        ),
        _tool(
            10,
            "edit_file",
            arguments="edit src/payments/client.py",
            output="1 insertion(+), 1 deletion(-)",
        ),
        _tool(
            11,
            "restart_service",
            arguments="systemctl restart payments",
            output="payments restarted (pid 4412)",
        ),
        _think(12),
        _tool(
            13,
            "curl",
            arguments="curl -s http://localhost:8080/health",
            output='{"status": "ok"}',
        ),
        # ...and only now does anything look wrong, five steps later.
        _tool(
            SYMPTOM_STEP,
            "pytest",
            exit_code=1,
            arguments="pytest tests/test_checkout.py",
            output=(
                "E   ConnectionError: HTTPSConnectionPool(host='api.example.com', port=443)\n"
                "1 failed in 3.14s"
            ),
        ),
        RunEndEvent(
            event_id=f"{RUN_ID}_evt_15",
            run_id=RUN_ID,
            sequence=15,
            timestamp=_at(15),
            run=RunPayload(outcome=RunOutcome.FAILURE, summary="integration test still failing"),
        ),
    ]
