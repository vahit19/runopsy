"""Labelling real runs into a corpus.

The design document calls the labelled failure corpus the project's defensible asset,
and until now nothing connected a recorded run to it: twenty synthetic cases lived in
Python, real runs lived in a store, and there was no path between them.

What these tests protect is not the file format. It is the three properties that make a
corpus worth having at all — that a label is a named human claim, that a case is
portable enough to be argued with, and that contributing a failure never means
contributing your source code.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_bench import (
    CORPUS_VERSION,
    LabelError,
    LabelledRun,
    carries_payload_text,
    from_json,
    label_run,
    load_corpus,
    to_json,
)
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core.schema import (
    CallStatus,
    Event,
    FailureCategory,
    RunOutcome,
    ToolCallEvent,
    ToolPayload,
)

runner = CliRunner()
RUN = "run_labelled"


def call(sequence: int, *, name: str = "pytest", exit_code: int = 0) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(
            name=name,
            arguments_hash="sha256:" + f"{sequence:064d}",
            exit_code=exit_code,
            status=CallStatus.ERROR if exit_code else CallStatus.OK,
        ),
    )


def trace() -> list[Event]:
    return [
        run_start(RUN, task="fix the build"),
        call(1, name="write_config", exit_code=1),
        call(2, name="build"),
        call(3, name="pytest", exit_code=1),
        run_end(4, RUN, outcome=RunOutcome.FAILURE),
    ]


def labelled(**overrides: Any) -> LabelledRun:
    arguments: dict[str, Any] = {
        "name": "case_one",
        "category": FailureCategory.TOOL_EXECUTION,
        "description": "the config write failed and the build carried on",
        "onset_step": 1,
        "affected_steps": {3},
        "labelled_by": "Vahit Feryad",
    }
    arguments.update(overrides)
    return label_run(trace(), **arguments)


class TestALabelIsAHumanClaim:
    def test_it_records_who_made_it(self) -> None:
        case = labelled()

        assert case.labelled_by == "Vahit Feryad"
        assert case.labelled_at

    def test_an_unattributed_label_is_refused(self) -> None:
        """The same reason `human_verified` needs a verifier: nobody stands behind it."""
        with pytest.raises(LabelError, match="labelled_by"):
            labelled(labelled_by="   ")

    def test_the_onset_is_whatever_the_person_said(self) -> None:
        """Never read from the engine — a corpus scored against the engine's own
        opinion would only ever confirm what the engine already believes."""
        case = labelled(onset_step=2)

        assert case.onset_step == 2

    def test_a_healthy_run_can_be_labelled_too(self) -> None:
        """Cases where nothing went wrong are what keep the false-positive rate honest."""
        case = labelled(onset_step=None, affected_steps=set())

        assert case.onset_step is None
        assert case.as_case().is_healthy


class TestALabelThatCannotBeTrustedIsRefused:
    def test_an_onset_outside_the_run(self) -> None:
        with pytest.raises(LabelError, match="not in this run"):
            labelled(onset_step=99)

    def test_affected_steps_outside_the_run(self) -> None:
        with pytest.raises(LabelError, match="affected steps not in this run"):
            labelled(affected_steps={99})

    def test_an_affected_step_before_the_onset(self) -> None:
        """The impact layer's invariant, enforced on human input too: nothing may
        affect the past."""
        with pytest.raises(LabelError, match="after the onset"):
            labelled(onset_step=3, affected_steps={1})

    def test_an_empty_run(self) -> None:
        with pytest.raises(LabelError, match="at least one event"):
            label_run(
                [],
                name="x",
                category=FailureCategory.TOOL_EXECUTION,
                description="",
                onset_step=None,
                labelled_by="someone",
            )


class TestCasesTravelWithoutContent:
    def test_a_case_round_trips(self) -> None:
        case = labelled()

        restored = from_json(to_json(case))

        assert restored.onset_step == case.onset_step
        assert restored.labelled_by == case.labelled_by
        assert [e.event_id for e in restored.events] == [e.event_id for e in case.events]

    def test_it_is_readable_json_rather_than_a_pickle(self) -> None:
        """A case has to survive a schema change and a review on a pull request."""
        import json

        document = json.loads(to_json(labelled()))

        assert document["corpus_version"] == CORPUS_VERSION
        assert document["labelled_by"]

    def test_a_newer_format_is_refused_rather_than_misread(self) -> None:
        text = to_json(labelled()).replace(
            f'"corpus_version": {CORPUS_VERSION}', '"corpus_version": 999'
        )

        with pytest.raises(LabelError, match="corpus version"):
            from_json(text)

    def test_a_normal_trace_carries_no_payload_text(self) -> None:
        assert carries_payload_text(labelled()) is False

    def test_content_that_slipped_in_is_detected(self) -> None:
        """Belt and braces before a case leaves the machine: the cost of being wrong
        here is somebody's repository."""
        events = trace()
        events[0] = run_start(RUN, task="x" * 900)

        case = label_run(
            events,
            name="leaky",
            category=FailureCategory.TOOL_EXECUTION,
            description="",
            onset_step=1,
            labelled_by="someone",
        )

        assert carries_payload_text(case) is True

    def test_a_directory_loads_in_a_stable_order(self, tmp_path: Path) -> None:
        """So a benchmark over a corpus is reproducible and a report diff means something."""
        for name in ("b_case", "a_case", "c_case"):
            (tmp_path / f"{name}.json").write_text(to_json(labelled(name=name)), encoding="utf-8")

        assert [case.name for case in load_corpus(tmp_path)] == ["a_case", "b_case", "c_case"]

    def test_a_missing_directory_is_empty_rather_than_an_error(self, tmp_path: Path) -> None:
        assert load_corpus(tmp_path / "absent") == ()


class TestTheCommand:
    @pytest.fixture
    def store(self, tmp_path: Path) -> Path:
        root = tmp_path / "store"
        with Collector.open(root) as collector:
            collector.record_all(trace())
        return root

    def test_it_writes_a_case_a_reviewer_could_read(self, store: Path, tmp_path: Path) -> None:
        destination = tmp_path / "case.json"

        result = runner.invoke(
            app,
            [
                "label",
                RUN,
                "--store",
                str(store),
                "--onset",
                "1",
                "--by",
                "Vahit Feryad",
                "--category",
                "tool_execution",
                "--affected",
                "3",
                "-o",
                str(destination),
            ],
        )

        assert result.exit_code == 0, result.output
        case = from_json(destination.read_text(encoding="utf-8"))
        assert case.onset_step == 1
        assert case.affected_steps == frozenset({3})

    def test_it_insists_on_a_labeller(self, store: Path, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["label", RUN, "--store", str(store), "--onset", "1", "-o", str(tmp_path / "x")]
        )

        assert result.exit_code == 2
        assert "--by" in result.output

    def test_it_insists_on_a_verdict(self, store: Path, tmp_path: Path) -> None:
        result = runner.invoke(app, ["label", RUN, "--store", str(store), "--by", "someone"])

        assert result.exit_code == 2
        assert "--onset" in result.output

    def test_onset_and_healthy_contradict(self, store: Path) -> None:
        result = runner.invoke(
            app,
            ["label", RUN, "--store", str(store), "--onset", "1", "--healthy", "--by", "x"],
        )

        assert result.exit_code == 2

    def test_an_unknown_category_lists_the_real_ones(self, store: Path) -> None:
        result = runner.invoke(
            app,
            [
                "label",
                RUN,
                "--store",
                str(store),
                "--onset",
                "1",
                "--by",
                "x",
                "--category",
                "vibes",
            ],
        )

        assert result.exit_code == 2
        assert "tool_execution" in result.output

    def test_scoring_an_empty_corpus_says_how_to_fill_it(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["bench", "--corpus", str(tmp_path)])

        assert result.exit_code == 2
        assert "runopsy label" in result.output

    def test_a_labelled_corpus_scores(self, store: Path, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "case.json").write_text(to_json(labelled()), encoding="utf-8")

        result = runner.invoke(app, ["bench", "--corpus", str(corpus)])

        assert result.exit_code == 0, result.output
        assert "labelled real run" in result.output
        assert "Vahit Feryad" in result.output
