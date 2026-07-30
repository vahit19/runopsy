"""Event schema tests, anchored to the wire format in design document section 7.3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from runopsy_core.schema import (
    CallStatus,
    Event,
    EventKind,
    LlmCallEvent,
    RunEndEvent,
    RunOutcome,
    ToolCallEvent,
)

events: TypeAdapter[Event] = TypeAdapter(Event)

# Reproduced verbatim from design document section 7.3. If a change to the schema makes
# this fail, the on-disk format has changed and the schema version must be bumped.
SPEC_SAMPLE: dict[str, Any] = {
    "schema_version": "0.1",
    "event_id": "evt_0187",
    "run_id": "run_0042",
    "parent_id": "turn_0011",
    "kind": "tool_call",
    "sequence": 17,
    "timestamp": "2026-07-30T09:45:21+03:00",
    "agent_id": "main",
    "tool": {
        "name": "terminal",
        "arguments_hash": "sha256:" + "a" * 64,
        "exit_code": 1,
        "duration_ms": 1824,
    },
    "state_delta": {"tests_passed": {"before": False, "after": True}},
    "security": {"redacted": True, "contains_secret": False},
}


def test_spec_sample_parses_into_a_tool_call_event() -> None:
    event = events.validate_python(SPEC_SAMPLE)

    assert isinstance(event, ToolCallEvent)
    assert event.kind is EventKind.TOOL_CALL
    assert event.sequence == 17
    assert event.tool.name == "terminal"
    assert event.tool.exit_code == 1
    assert event.security.redacted is True


def test_spec_sample_survives_a_round_trip() -> None:
    event = events.validate_python(SPEC_SAMPLE)

    restored = events.validate_python(events.dump_python(event, mode="json"))

    assert restored == event


def test_state_delta_records_the_transition_not_just_the_new_value() -> None:
    event = events.validate_python(SPEC_SAMPLE)

    change = event.state_delta["tests_passed"]

    assert change.before is False
    assert change.after is True


def test_kind_selects_the_matching_payload_type() -> None:
    payload = {
        **SPEC_SAMPLE,
        "kind": "llm_call",
        "llm": {"model": "local:qwen", "latency_ms": 90},
    }

    event = events.validate_python(payload)

    assert isinstance(event, LlmCallEvent)
    assert event.llm.model == "local:qwen"
    assert event.llm.status is CallStatus.OK


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        events.validate_python({**SPEC_SAMPLE, "kind": "telepathy"})


def test_a_newer_producers_extra_fields_are_kept_rather_than_rejected() -> None:
    payload = {**SPEC_SAMPLE, "sampling_rate": 0.5}

    event = events.validate_python(payload)

    assert event.model_extra is not None
    assert event.model_extra["sampling_rate"] == 0.5


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        events.validate_python({**SPEC_SAMPLE, "timestamp": "2026-07-30T09:45:21"})


def test_non_utc_offsets_are_preserved_as_the_same_instant() -> None:
    istanbul = events.validate_python(SPEC_SAMPLE)
    utc = events.validate_python({**SPEC_SAMPLE, "timestamp": "2026-07-30T06:45:21+00:00"})

    assert istanbul.timestamp == utc.timestamp
    assert istanbul.timestamp.utcoffset() == timedelta(hours=3)


def test_events_are_immutable_because_the_trace_is_append_only() -> None:
    event = events.validate_python(SPEC_SAMPLE)
    assert isinstance(event, ToolCallEvent)

    with pytest.raises(ValidationError):
        event.sequence = 99  # type: ignore[misc]


def test_malformed_digests_are_rejected() -> None:
    payload = {**SPEC_SAMPLE, "tool": {**SPEC_SAMPLE["tool"], "arguments_hash": "deadbeef"}}

    with pytest.raises(ValidationError):
        events.validate_python(payload)


def test_negative_sequence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        events.validate_python({**SPEC_SAMPLE, "sequence": -1})


def test_identifiers_reject_path_traversal() -> None:
    with pytest.raises(ValidationError):
        events.validate_python({**SPEC_SAMPLE, "run_id": "../../etc/passwd"})


def test_run_end_carries_a_reported_outcome_rather_than_an_inferred_one() -> None:
    payload = {
        "event_id": "evt_9999",
        "run_id": "run_0042",
        "kind": "run_end",
        "sequence": 40,
        "timestamp": datetime(2026, 7, 30, 9, 50, tzinfo=UTC).isoformat(),
        "run": {"outcome": "failure", "summary": "integration test exit code 1"},
    }

    event = events.validate_python(payload)

    assert isinstance(event, RunEndEvent)
    assert event.run.outcome is RunOutcome.FAILURE


@pytest.mark.parametrize(
    "kind",
    [k.value for k in EventKind],
)
def test_every_declared_kind_is_constructible(kind: str) -> None:
    """Every member of EventKind must have a concrete event class behind it.

    Guards against an enum member being added without a payload, which would let an
    adapter emit an event the engine silently cannot parse.
    """
    minimal_payloads: dict[str, dict[str, object]] = {
        "run_start": {"run": {"runtime": "hermes"}},
        "run_end": {"run": {}},
        "agent_start": {"agent": {"role": "main"}},
        "agent_end": {"agent": {}},
        "turn_start": {"turn": {}},
        "turn_end": {"turn": {}},
        "llm_call": {"llm": {"model": "local:qwen"}},
        "tool_call": {"tool": {"name": "terminal"}},
        "state_snapshot": {"state": {}},
        "memory_op": {"memory": {"operation": "read", "key": "plan"}},
        "claim": {"claim": {"claim_id": "c1", "text_hash": "sha256:" + "b" * 64}},
        "evidence": {"evidence": {"source": "pytest", "excerpt_hash": "sha256:" + "c" * 64}},
        "artifact": {"artifact": {"path": "a.py", "content_hash": "sha256:" + "d" * 64}},
        "checkpoint": {"checkpoint": {"checkpoint_id": "ck1"}},
        "handoff": {"handoff": {"from_agent_id": "main", "to_agent_id": "sub"}},
    }

    event = events.validate_python(
        {
            "event_id": f"evt_{kind}",
            "run_id": "run_0042",
            "kind": kind,
            "sequence": 0,
            "timestamp": datetime(2026, 7, 30, tzinfo=timezone(timedelta(hours=3))),
            **minimal_payloads[kind],
        }
    )

    assert event.kind.value == kind
