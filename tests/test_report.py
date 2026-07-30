"""HTML report tests.

Two properties carry weight beyond appearance: the report must open anywhere with no
network, and it must agree with the terminal about which step is which. A visual view
that disagrees with the text turns a debugging session into an argument about tooling.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_cli.language import asserts_causation
from runopsy_cli.main import app
from runopsy_cli.report import render_report
from runopsy_collector import Collector, RunSummary
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import (
    Event,
    RunOutcome,
    SecurityMetadata,
    StateChange,
    ToolCallEvent,
    ToolPayload,
)

RUN = "run_0042"
runner = CliRunner()

EXTERNAL_REFERENCE = re.compile(r"(src|href)\s*=\s*[\"']?(https?:)?//", re.IGNORECASE)


def tool(
    sequence: int, *, name: str = "terminal", exit_code: int = 0, secret: bool = False
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
        security=SecurityMetadata(contains_secret=secret),
        state_delta={"endpoint": StateChange(after="staging")} if sequence == 9 else {},
    )


def failing_run(*, with_secret: bool = False) -> list[Event]:
    return [
        run_start(RUN, task="fix the failing integration test"),
        tool(9, name="write_config", exit_code=1),
        tool(10, name="edit_file"),
        tool(11, name="curl", secret=with_secret),
        tool(14, name="pytest", exit_code=1),
        run_end(15, RUN, outcome=RunOutcome.FAILURE),
    ]


def build(events: list[Event], *, redact: bool = True, summary: RunSummary | None = None) -> str:
    context = AnalysisContext.from_events(RUN, events)
    bundle = diagnose(context)
    return render_report(bundle, context.graph, summary, redact=redact)


@pytest.fixture
def document() -> str:
    return build(failing_run())


class TestSelfContained:
    def test_it_makes_no_external_requests(self, document: str) -> None:
        """The interesting failures happen on machines that cannot reach a CDN."""
        assert not EXTERNAL_REFERENCE.search(document), "report references an external asset"

    def test_it_is_a_complete_document(self, document: str) -> None:
        assert document.startswith("<!doctype html>")
        assert "</html>" in document

    def test_styles_are_inline(self, document: str) -> None:
        assert "<style>" in document
        assert "stylesheet" not in document

    def test_it_carries_no_script(self, document: str) -> None:
        """Nothing here needs to execute, so nothing here is allowed to."""
        assert "<script" not in document.lower()


class TestIdentityMatchesTheTerminal:
    def test_every_step_is_addressable_by_its_node_id(self, document: str) -> None:
        for node_id in ("evt_9", "evt_10", "evt_14"):
            assert f'id="node-{node_id}"' in document

    def test_step_numbers_match_the_trace_sequence(self, document: str) -> None:
        assert 'data-step="9"' in document
        assert 'data-step="14"' in document

    def test_the_ribbon_and_the_timeline_use_the_same_identities(self, document: str) -> None:
        assert 'id="ribbon-evt_9"' in document
        assert 'id="node-evt_9"' in document

    def test_container_nodes_are_not_shown_as_steps(self, document: str) -> None:
        """A run is not a step; listing it would invite blaming it for the failure."""
        assert f'id="node-{RUN}"' not in document


class TestContent:
    def test_it_separates_the_symptom_from_the_suspicion(self, document: str) -> None:
        assert "Observed failure" in document
        assert "Suspected onset" in document

    def test_it_never_claims_a_cause_it_has_not_validated(self, document: str) -> None:
        assert not asserts_causation(document), "report asserts causation"

    def test_it_says_the_arcs_are_inferred(self, document: str) -> None:
        """Reachability drawn as a line is easily misread as a demonstrated effect."""
        assert "inferred" in document
        assert "not a demonstrated" in document

    def test_it_shows_how_to_confirm_the_finding(self, document: str) -> None:
        assert "runopsy replay" in document

    def test_a_clean_run_says_so(self) -> None:
        events = [
            run_start(RUN, task="tidy up"),
            tool(1),
            run_end(2, RUN, outcome=RunOutcome.SUCCESS),
        ]

        assert "Nothing detectable went wrong" in build(events)

    def test_markup_in_a_task_name_is_escaped(self) -> None:
        """A task string is user-controlled text arriving from a runtime adapter."""
        events = [run_start(RUN, task="<script>alert(1)</script>"), tool(1, exit_code=1)]
        summary = RunSummary(run_id=RUN, task="<script>alert(1)</script>", runtime="hermes")

        document = build(events, summary=summary)

        assert "<script>alert(1)" not in document
        assert "&lt;script&gt;alert(1)" in document

    def test_markup_in_a_tool_name_is_escaped(self) -> None:
        events = [run_start(RUN), tool(1, name="<img onerror=alert(1)>", exit_code=1)]

        document = build(events)

        assert "<img onerror" not in document
        assert "&lt;img" in document


class TestRedaction:
    def test_flagged_values_are_withheld_by_default(self) -> None:
        document = build(failing_run(with_secret=True))

        assert "[redacted]" in document

    def test_the_shape_of_the_run_survives_redaction(self) -> None:
        """A redacted report that hides the timeline is a report nobody can use."""
        document = build(failing_run(with_secret=True))

        assert 'id="node-evt_11"' in document
        assert 'data-step="11"' in document

    def test_redaction_can_be_disabled_deliberately(self) -> None:
        document = build(failing_run(with_secret=True), redact=False)

        assert "[redacted]" not in document


class TestExportCommand:
    @pytest.fixture
    def store(self, tmp_path: Path) -> Path:
        root = tmp_path / "store"
        with Collector.open(root) as collector:
            collector.record_all(failing_run(with_secret=True))
        return root

    def test_it_writes_a_file(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "report.html"

        result = runner.invoke(app, ["export", RUN, "--store", str(store), "-o", str(destination)])

        assert result.exit_code == 0, result.output
        assert destination.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_redaction_is_the_default_not_the_opt_in(self, store: Path, tmp_path: Path) -> None:
        """Export is the sharing path; a leaking default eventually leaks in public."""
        destination = tmp_path / "report.html"

        runner.invoke(app, ["export", RUN, "--store", str(store), "-o", str(destination)])

        assert "[redacted]" in destination.read_text(encoding="utf-8")

    def test_disabling_redaction_warns(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "report.html"

        result = runner.invoke(
            app,
            [
                "export",
                RUN,
                "--store",
                str(store),
                "-o",
                str(destination),
                "--include-sensitive",
            ],
        )

        assert "may contain values" in result.output

    def test_an_unknown_run_fails_with_a_usable_message(self, store: Path) -> None:
        result = runner.invoke(app, ["export", "run_nope", "--store", str(store)])

        assert result.exit_code == 2
        assert "No events recorded" in result.output
