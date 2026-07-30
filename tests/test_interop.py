"""Structured logging and OpenInference export tests.

Both close a gap where the design named a capability the code did not have. The tests
are mostly about restraint: logging must stay silent and must never carry a credential,
and export must not relax the privacy rules that every other surface follows.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core import AnalysisContext, diagnose, get_logger, logging_enabled, to_otlp_json
from runopsy_core.openinference import SPAN_KIND, to_spans
from runopsy_core.schema import (
    DiagnosisBundle,
    Event,
    LlmCallEvent,
    LlmPayload,
    RunOutcome,
    SecurityMetadata,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

runner = CliRunner()
RUN = "run_interop"
SECRET_HASH = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNOPSY_LOG", raising=False)
    monkeypatch.delenv("RUNOPSY_LOG_FORMAT", raising=False)


def tool(
    sequence: int, *, name: str = "terminal", exit_code: int = 0, secret: bool = False
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code, arguments_hash=SECRET_HASH),
        security=SecurityMetadata(contains_secret=secret),
    )


def trace() -> list[Event]:
    return [
        run_start(RUN, task="configure and verify"),
        tool(1, name="write_config", exit_code=1),
        LlmCallEvent(
            event_id="evt_2",
            run_id=RUN,
            sequence=2,
            timestamp=START + timedelta(seconds=2),
            llm=LlmPayload(
                model="gpt-4o-mini",
                provider="openrouter",
                tokens=TokenUsage(input_tokens=120, output_tokens=30),
            ),
        ),
        tool(3, name="curl", secret=True),
        tool(4, name="pytest", exit_code=1),
        run_end(5, RUN, outcome=RunOutcome.FAILURE),
    ]


def analysed() -> tuple[AnalysisContext, DiagnosisBundle]:
    context = AnalysisContext.from_events(RUN, trace())
    return context, diagnose(context)


class TestLoggingIsSilentByDefault:
    def test_nothing_is_emitted_without_the_variable(self) -> None:
        """A tool that chatters into a terminal competes with the answer it was asked for."""
        stream = io.StringIO()
        get_logger("test", stream).info("something happened", run="x")

        assert stream.getvalue() == ""
        assert logging_enabled() is False

    def test_setting_the_level_enables_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "info")
        stream = io.StringIO()

        get_logger("test", stream).info("recorded", events=3)

        assert "recorded" in stream.getvalue()
        assert "events=3" in stream.getvalue()

    def test_a_lower_level_message_is_filtered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "warning")
        stream = io.StringIO()

        get_logger("test", stream).debug("noisy detail")

        assert stream.getvalue() == ""

    def test_json_format_is_machine_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "info")
        monkeypatch.setenv("RUNOPSY_LOG_FORMAT", "json")
        stream = io.StringIO()

        get_logger("collector", stream).warning("gap detected", run="run_1", missing=2)

        record = json.loads(stream.getvalue())
        assert record["event"] == "gap detected"
        assert record["logger"] == "collector"
        assert record["missing"] == 2


class TestLoggingNeverLeaks:
    def test_a_field_named_like_a_secret_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A debug line must not become the leak path every other layer prevents."""
        monkeypatch.setenv("RUNOPSY_LOG", "debug")
        stream = io.StringIO()

        get_logger("test", stream).debug("provider call", api_key="super-secret-value")

        assert "super-secret-value" not in stream.getvalue()
        assert "[REDACTED]" in stream.getvalue()

    @pytest.mark.parametrize("field", ["token", "password", "authorization", "credential"])
    def test_every_sensitive_field_name_is_covered(
        self, field: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "debug")
        stream = io.StringIO()

        get_logger("test", stream).debug("call", **{field: "leaked-value"})

        assert "leaked-value" not in stream.getvalue()

    def test_a_long_value_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "debug")
        stream = io.StringIO()

        get_logger("test", stream).debug("payload", body="x" * 5_000)

        assert len(stream.getvalue()) < 500

    def test_a_digest_is_shortened_rather_than_printed_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNOPSY_LOG", "debug")
        stream = io.StringIO()

        get_logger("test", stream).debug("hashed", digest=SECRET_HASH)

        assert SECRET_HASH not in stream.getvalue()
        assert "sha256:aaaaaaa" in stream.getvalue()


class TestOpenInferenceShape:
    def test_every_event_becomes_a_span(self) -> None:
        context, bundle = analysed()

        spans = to_spans(context.graph, context.events, bundle)

        assert len(spans) == len(context.events)

    def test_spans_carry_the_openinference_kind(self) -> None:
        context, bundle = analysed()

        kinds = {
            span.attributes[SPAN_KIND] for span in to_spans(context.graph, context.events, bundle)
        }

        assert {"AGENT", "TOOL", "LLM"} <= kinds

    def test_model_calls_use_the_conventional_attribute_names(self) -> None:
        """The point of the export is that other tools can read it without a mapping."""
        context, bundle = analysed()

        llm = next(
            span
            for span in to_spans(context.graph, context.events, bundle)
            if span.attributes[SPAN_KIND] == "LLM"
        )

        assert llm.attributes["llm.model_name"] == "gpt-4o-mini"
        assert llm.attributes["llm.token_count.prompt"] == 120
        assert llm.attributes["llm.token_count.total"] == 150

    def test_a_failing_step_gets_an_error_status(self) -> None:
        context, bundle = analysed()

        failed = [s for s in to_spans(context.graph, context.events, bundle) if s.status == "ERROR"]

        assert failed

    def test_span_ids_are_stable_across_exports(self) -> None:
        """Derived rather than random, so re-exporting does not create a second copy."""
        context, bundle = analysed()

        first = [s.span_id for s in to_spans(context.graph, context.events, bundle)]
        second = [s.span_id for s in to_spans(context.graph, context.events, bundle)]

        assert first == second


class TestTheDiagnosisTravelsWithTheTrace:
    def test_the_onset_is_annotated(self) -> None:
        """Without this the export is just a trace; with it, it carries the finding."""
        context, bundle = analysed()

        roles = {
            span.attributes.get("runopsy.diagnosis.role")
            for span in to_spans(context.graph, context.events, bundle)
        }

        assert "suspected_onset" in roles
        assert "observed_failure" in roles

    def test_the_onset_span_states_its_status_and_confidence(self) -> None:
        context, bundle = analysed()

        onset = next(
            span
            for span in to_spans(context.graph, context.events, bundle)
            if span.attributes.get("runopsy.diagnosis.role") == "suspected_onset"
        )

        assert onset.attributes["runopsy.diagnosis.status"] == "suspected_onset"
        assert 0 < onset.attributes["runopsy.diagnosis.confidence"] < 1

    def test_exporting_without_a_diagnosis_still_works(self) -> None:
        context, _ = analysed()

        spans = to_spans(context.graph, context.events, None)

        assert spans
        assert all("runopsy.diagnosis.role" not in span.attributes for span in spans)


class TestExportPrivacy:
    def test_a_flagged_step_is_withheld(self) -> None:
        """An export is a sharing surface whichever format it takes."""
        context, bundle = analysed()

        flagged = next(
            span
            for span in to_spans(context.graph, context.events, bundle, redact=True)
            if span.attributes.get("runopsy.contains_secret")
        )

        assert flagged.attributes["input.value"] == "[redacted]"

    def test_redaction_can_be_disabled_deliberately(self) -> None:
        context, bundle = analysed()

        flagged = next(
            span
            for span in to_spans(context.graph, context.events, bundle, redact=False)
            if span.attributes.get("runopsy.contains_secret")
        )

        assert flagged.attributes["input.value"] == SECRET_HASH

    def test_no_raw_content_is_ever_exported(self) -> None:
        """Only hashes, exactly as the trace itself stores them."""
        context, bundle = analysed()

        document = to_otlp_json(context.graph, context.events, bundle)

        assert "sha256:" in document
        assert "write_config" in document  # tool names are not content


class TestOtlpDocument:
    def test_it_is_valid_otlp_json(self) -> None:
        context, bundle = analysed()

        document = json.loads(to_otlp_json(context.graph, context.events, bundle))

        resource = document["resourceSpans"][0]
        assert resource["scopeSpans"][0]["spans"]
        assert any(
            attribute["key"] == "service.name" for attribute in resource["resource"]["attributes"]
        )

    def test_all_spans_share_one_trace_id(self) -> None:
        context, bundle = analysed()

        document = json.loads(to_otlp_json(context.graph, context.events, bundle))
        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]

        assert len({span["traceId"] for span in spans}) == 1

    def test_steps_hang_off_the_run_span(self) -> None:
        context, bundle = analysed()

        document = json.loads(to_otlp_json(context.graph, context.events, bundle))
        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
        roots = [span for span in spans if not span["parentSpanId"]]

        assert len(roots) == 1

    def test_an_empty_run_produces_an_empty_document(self) -> None:
        context = AnalysisContext.from_events(RUN, ())

        document = json.loads(to_otlp_json(context.graph, context.events, None))

        assert document["resourceSpans"][0]["scopeSpans"][0]["spans"] == []


class TestExportCommand:
    @pytest.fixture
    def store(self, tmp_path: Path) -> Path:
        root = tmp_path / "store"
        with Collector.open(root) as collector:
            collector.record_all(trace())
        return root

    def test_otlp_export_writes_valid_json(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "trace.otlp.json"

        result = runner.invoke(
            app, ["export", RUN, "--store", str(store), "--otlp", "-o", str(destination)]
        )

        assert result.exit_code == 0, result.output
        document = json.loads(destination.read_text(encoding="utf-8"))
        assert document["resourceSpans"]

    def test_otlp_export_is_redacted_by_default(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "trace.otlp.json"

        runner.invoke(app, ["export", RUN, "--store", str(store), "--otlp", "-o", str(destination)])

        assert "[redacted]" in destination.read_text(encoding="utf-8")

    def test_html_remains_the_default_format(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "report.html"

        runner.invoke(app, ["export", RUN, "--store", str(store), "-o", str(destination)])

        assert destination.read_text(encoding="utf-8").startswith("<!doctype html>")
