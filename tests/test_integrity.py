"""Trace integrity tests, including the property the whole check exists to guarantee."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from runopsy_core import check_integrity
from runopsy_core.schema import ToolCallEvent, ToolPayload

NOW = datetime(2026, 7, 30, 9, 45, tzinfo=UTC)


def event(sequence: int, run_id: str = "run_0042") -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=run_id,
        sequence=sequence,
        timestamp=NOW,
        tool=ToolPayload(name="terminal"),
    )


def test_a_contiguous_stream_is_intact() -> None:
    report = check_integrity("run_0042", [event(i) for i in range(5)])

    assert report.is_intact is True
    assert report.event_count == 5
    assert report.describe() == "5 events, intact"


def test_a_dropped_event_is_reported_as_a_gap() -> None:
    report = check_integrity("run_0042", [event(0), event(1), event(3)])

    assert report.missing_sequences == (2,)
    assert report.is_intact is False


def test_a_replayed_write_is_reported_as_a_duplicate() -> None:
    report = check_integrity("run_0042", [event(0), event(1), event(1)])

    assert report.duplicate_sequences == (1,)


def test_reordered_ingest_is_detected() -> None:
    report = check_integrity("run_0042", [event(0), event(2), event(1)])

    assert report.out_of_order is True


def test_events_from_another_run_are_isolated_not_silently_merged() -> None:
    """Mixing runs would let one run's failure be blamed on another run's step."""
    report = check_integrity("run_0042", [event(0), event(1, run_id="run_9999")])

    assert report.foreign_run_ids == ("run_9999",)
    assert report.event_count == 1


def test_an_empty_stream_reports_nothing_missing() -> None:
    report = check_integrity("run_0042", [])

    assert report.event_count == 0
    assert report.missing_sequences == ()


def test_a_window_not_starting_at_zero_is_still_intact() -> None:
    """Diagnosis often runs on a trimmed window; that is not corruption."""
    report = check_integrity("run_0042", [event(10), event(11), event(12)])

    assert report.is_intact is True


@given(
    start=st.integers(min_value=0, max_value=1000),
    length=st.integers(min_value=1, max_value=60),
)
def test_any_contiguous_ascending_window_is_intact(start: int, length: int) -> None:
    events = [event(i) for i in range(start, start + length)]

    assert check_integrity("run_0042", events).is_intact is True


@given(
    sequences=st.lists(
        st.integers(min_value=0, max_value=200), min_size=1, max_size=40, unique=True
    )
)
def test_sorted_unique_sequences_are_intact_exactly_when_there_are_no_gaps(
    sequences: list[int],
) -> None:
    ordered = sorted(sequences)
    report = check_integrity("run_0042", [event(i) for i in ordered])
    contiguous = ordered == list(range(ordered[0], ordered[0] + len(ordered)))

    assert report.is_intact is contiguous
