"""Performance measurement, from section 18 of the design document.

The quality gate there asks for 10K and 100K event ingest, graph build and query
benchmarks. The reason is not throughput bragging: a long agent run produces tens of
thousands of events, and a diagnosis engine that takes minutes on one is a diagnosis
engine nobody waits for. The number that matters is whether analysis stays usable at the
size real runs reach.

Everything here is measured on generated traces and reported with the sizes attached, so
a regression shows up as a changed number rather than as a vague feeling that things got
slower. Timings are wall clock on one machine and will differ on yours; the shape of the
curve is the durable part, not the absolute milliseconds.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from runopsy_core import AnalysisContext, build_graph, diagnose
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
    ToolCallEvent,
    ToolPayload,
)

DEFAULT_SIZES: tuple[int, ...] = (1_000, 10_000, 100_000)
START = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Timing:
    """One measured stage."""

    stage: str
    events: int
    seconds: float

    @property
    def per_second(self) -> float:
        return self.events / self.seconds if self.seconds else float("inf")

    @property
    def milliseconds(self) -> float:
        return self.seconds * 1000


@dataclass
class PerformanceReport:
    """Timings for one trace size."""

    events: int
    timings: list[Timing] = field(default_factory=list)

    def stage(self, name: str) -> Timing | None:
        return next((timing for timing in self.timings if timing.stage == name), None)


@contextmanager
def _measure(report: PerformanceReport, stage: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        report.timings.append(
            Timing(stage=stage, events=report.events, seconds=time.perf_counter() - started)
        )


def synthetic_trace(count: int, run_id: str = "perf") -> tuple[Event, ...]:
    """A trace of ``count`` events with a realistic mix and a real failure in it.

    Roughly one model call for every three tool calls, occasional state changes, and a
    failure a fifth of the way in — enough for the detectors and the ranker to do real
    work rather than short-circuit on an empty result.
    """
    events: list[Event] = [
        RunStartEvent(
            event_id=f"{run_id}_evt_0",
            run_id=run_id,
            sequence=0,
            timestamp=START,
            run=RunPayload(task="performance trace", runtime="synthetic"),
        )
    ]
    onset = max(1, count // 5)

    for index in range(1, count - 1):
        moment = START + timedelta(milliseconds=index * 20)
        if index % 4 == 0:
            events.append(
                LlmCallEvent(
                    event_id=f"{run_id}_evt_{index}",
                    run_id=run_id,
                    sequence=index,
                    timestamp=moment,
                    llm=LlmPayload(model="local:qwen", latency_ms=90),
                )
            )
            continue

        failed = index in {onset, count - 2}
        events.append(
            ToolCallEvent(
                event_id=f"{run_id}_evt_{index}",
                run_id=run_id,
                sequence=index,
                timestamp=moment,
                tool=ToolPayload(
                    name="pytest" if failed else "edit_file",
                    exit_code=1 if failed else 0,
                    status=CallStatus.ERROR if failed else CallStatus.OK,
                    duration_ms=40,
                ),
                state_delta=(
                    {"ready": StateChange(after=index % 2 == 0)} if index % 50 == 0 else {}
                ),
            )
        )

    events.append(
        RunEndEvent(
            event_id=f"{run_id}_evt_{count - 1}",
            run_id=run_id,
            sequence=count - 1,
            timestamp=START + timedelta(milliseconds=count * 20),
            run=RunPayload(outcome=RunOutcome.FAILURE),
        )
    )
    return tuple(events)


def measure(count: int, *, include_store: bool = True) -> PerformanceReport:
    """Time trace generation, ingest, graph build, diagnosis and query at one size."""
    report = PerformanceReport(events=count)

    with _measure(report, "build trace"):
        events = synthetic_trace(count)

    if include_store:
        # Imported here so the timing functions stay usable without the collector,
        # and so importing this module does not pull in DuckDB.
        from runopsy_collector import Collector

        with TemporaryDirectory(prefix="runopsy-perf-") as directory:
            with Collector.open(Path(directory)) as collector, _measure(report, "ingest"):
                collector.record_all(events)
            with Collector.open(Path(directory)) as collector:
                with _measure(report, "read back"):
                    collector.events("perf")
                with _measure(report, "integrity check"):
                    collector.integrity("perf")

    with _measure(report, "build graph"):
        build_graph("perf", events)

    with _measure(report, "detect and rank"):
        diagnose(AnalysisContext.from_events("perf", events))

    return report


def run_performance_suite(
    sizes: tuple[int, ...] = DEFAULT_SIZES, *, include_store: bool = True
) -> tuple[PerformanceReport, ...]:
    """Measure every requested trace size, smallest first."""
    return tuple(measure(size, include_store=include_store) for size in sorted(sizes))


def scaling_factor(reports: tuple[PerformanceReport, ...], stage: str) -> float | None:
    """How the stage grows between the smallest and largest size measured.

    A value near the size ratio means linear. Well above it means the stage will stop
    being usable before real traces stop growing, which is the thing worth catching.
    """
    usable = [report for report in reports if report.stage(stage) is not None]
    if len(usable) < 2:
        return None
    first, last = usable[0], usable[-1]
    first_timing, last_timing = first.stage(stage), last.stage(stage)
    if first_timing is None or last_timing is None or first_timing.seconds <= 0:
        return None
    return last_timing.seconds / first_timing.seconds


STAGES: tuple[str, ...] = (
    "build trace",
    "ingest",
    "read back",
    "integrity check",
    "build graph",
    "detect and rank",
)


def _timing_lookup(report: PerformanceReport) -> Callable[[str], Timing | None]:
    return report.stage
