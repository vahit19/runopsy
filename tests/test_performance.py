"""Performance tests, from the quality gate in section 18.

These are correctness tests about scale, not micro-benchmarks. A diagnosis engine that
takes ten minutes on a long run is one nobody waits for, and the first time this suite
ran it found exactly that: ingest managed 17 events per second, so a 10,000-event trace
took ten minutes to record.

Thresholds are deliberately loose — CI machines are slow and shared — and exist to catch
a return to row-at-a-time ingest, not to police milliseconds.
"""

from __future__ import annotations

import pathlib
import tempfile
import time

import pytest

from runopsy_bench.performance import (
    STAGES,
    measure,
    run_performance_suite,
    scaling_factor,
    synthetic_trace,
)
from runopsy_collector import Collector
from runopsy_core import AnalysisContext, build_graph, diagnose

MIN_INGEST_EVENTS_PER_SECOND = 300
"""What this guard is actually for: catching a return to row-at-a-time ingest.

That path measured 17 events per second — ten minutes to ingest a run an agent produces
in an hour — and the bulk loader took it to roughly 30,000. Any threshold between those
two catches the regression.

It was 2,000, which sounds prudent and was not. A wall-clock assertion inside a test
suite competes with every other test on the machine, and this one duly failed at 1,710
events/sec during a full run while passing five times in a row on its own. A flaky gate
is worse than a loose one: it teaches people to re-run until green, and then a real
failure gets the same shrug.

300 is seventeen times the pathological case and a hundred times below the measured
rate, so it still fails loudly if the bulk path disappears and cannot fail because
another test was busy. Actual numbers belong in `runopsy bench --perf`, which measures
rather than asserts.
"""

MAX_DIAGNOSE_SECONDS_10K = 20.0


class TestTraceGeneration:
    def test_the_generated_trace_has_the_requested_size(self) -> None:
        assert len(synthetic_trace(500)) == 500

    def test_it_contains_a_real_failure_to_analyse(self) -> None:
        """A trace with nothing wrong would measure the empty path, not the real one."""
        bundle = diagnose(AnalysisContext.from_events("perf", synthetic_trace(500)))

        assert bundle.candidates

    def test_sequences_are_contiguous(self) -> None:
        events = synthetic_trace(300)

        assert sorted(e.sequence for e in events) == list(range(300))


class TestIngestScales:
    @pytest.mark.parametrize("size", [1_000, 10_000])
    def test_ingest_is_fast_enough_to_be_usable(self, size: int) -> None:
        """Regression guard for the row-at-a-time ingest that measured 17 events/sec."""
        events = synthetic_trace(size)

        with (
            tempfile.TemporaryDirectory() as directory,
            Collector.open(pathlib.Path(directory)) as collector,
        ):
            started = time.perf_counter()
            recorded = collector.record_all(events)
            elapsed = time.perf_counter() - started

        assert recorded == size
        rate = size / elapsed
        assert rate > MIN_INGEST_EVENTS_PER_SECOND, f"{rate:,.0f} events/sec is too slow"

    def test_a_large_batch_survives_the_round_trip(self) -> None:
        """The bulk path stages CSV; nothing may be lost or mangled by that detour."""
        events = synthetic_trace(2_000)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with Collector.open(root) as collector:
                collector.record_all(events)
            with Collector.open(root) as collector:
                stored = collector.events("perf")
                report = collector.integrity("perf")

        assert len(stored) == len(events)
        assert report.is_intact
        assert [e.event_id for e in stored] == [e.event_id for e in events]

    def test_payloads_survive_the_csv_staging(self) -> None:
        """Journal bytes carry JSON with commas and quotes; CSV must not corrupt them."""
        events = synthetic_trace(200)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with Collector.open(root) as collector:
                collector.record_all(events)
                stored = collector.events("perf")

        assert stored[1].model_dump() == events[1].model_dump()

    def test_re_recording_a_batch_adds_nothing(self) -> None:
        """Idempotence must survive the batched path, not only the single-event one."""
        events = synthetic_trace(500)

        with (
            tempfile.TemporaryDirectory() as directory,
            Collector.open(pathlib.Path(directory)) as collector,
        ):
            first = collector.record_all(events)
            second = collector.record_all(events)
            stored = len(collector.events("perf"))

        assert first == 500
        assert second == 0
        assert stored == 500

    def test_a_batch_containing_duplicates_is_deduplicated(self) -> None:
        """A retrying adapter must not violate the primary key mid-batch."""
        events = synthetic_trace(100)

        with (
            tempfile.TemporaryDirectory() as directory,
            Collector.open(pathlib.Path(directory)) as collector,
        ):
            recorded = collector.record_all([*events, *events])
            stored = len(collector.events("perf"))

        assert recorded == 100
        assert stored == 100


class TestAnalysisScales:
    def test_graph_build_handles_ten_thousand_events(self) -> None:
        events = synthetic_trace(10_000)

        started = time.perf_counter()
        graph = build_graph("perf", events)
        elapsed = time.perf_counter() - started

        assert len(graph.nodes) > 9_000
        assert elapsed < MAX_DIAGNOSE_SECONDS_10K

    def test_diagnosis_handles_ten_thousand_events(self) -> None:
        context = AnalysisContext.from_events("perf", synthetic_trace(10_000))

        started = time.perf_counter()
        bundle = diagnose(context)
        elapsed = time.perf_counter() - started

        assert bundle.candidates
        assert elapsed < MAX_DIAGNOSE_SECONDS_10K

    def test_analysis_stays_roughly_linear(self) -> None:
        """Ten times the events should not cost far more than ten times the work."""
        reports = run_performance_suite((500, 5_000), include_store=False)

        factor = scaling_factor(reports, "detect and rank")

        assert factor is not None
        assert factor < 40, f"detect and rank grew {factor:.1f}x for 10x the events"


class TestReportShape:
    def test_every_stage_is_timed(self) -> None:
        report = measure(200)

        assert {timing.stage for timing in report.timings} == set(STAGES)

    def test_timings_carry_the_size_they_were_measured_at(self) -> None:
        report = measure(200)

        assert all(timing.events == 200 for timing in report.timings)

    def test_the_store_can_be_excluded(self) -> None:
        report = measure(200, include_store=False)

        assert report.stage("ingest") is None
        assert report.stage("detect and rank") is not None
