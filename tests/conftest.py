"""Shared fixtures and event builders for the collector tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runopsy_collector import Collector
from runopsy_core.schema import (
    Event,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    SecurityMetadata,
    ToolCallEvent,
    ToolPayload,
)

START = datetime(2026, 7, 30, 9, 45, tzinfo=UTC)


@pytest.fixture
def collector(tmp_path: Path) -> Iterator[Collector]:
    with Collector.open(tmp_path / "store") as opened:
        yield opened


def run_start(
    run_id: str = "run_0042",
    *,
    task: str = "fix the failing test",
    at: datetime | None = None,
) -> Event:
    return RunStartEvent(
        event_id=f"{run_id}_evt_start",
        run_id=run_id,
        sequence=0,
        timestamp=at or START,
        run=RunPayload(task=task, runtime="hermes", provider="openrouter", repo="payments"),
    )


def tool_call(
    sequence: int,
    run_id: str = "run_0042",
    *,
    name: str = "terminal",
    exit_code: int = 0,
    contains_secret: bool = False,
) -> Event:
    return ToolCallEvent(
        event_id=f"{run_id}_evt_{sequence}",
        run_id=run_id,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
        security=SecurityMetadata(contains_secret=contains_secret),
    )


def run_end(
    sequence: int, run_id: str = "run_0042", *, outcome: RunOutcome = RunOutcome.FAILURE
) -> Event:
    return RunEndEvent(
        event_id=f"{run_id}_evt_end",
        run_id=run_id,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        run=RunPayload(outcome=outcome, summary="integration test exit code 1"),
    )
