"""Regressions taken from the first real agent session Runopsy ever recorded.

Everything else in this suite is built from traces written to exercise the engine. This
file is different: on 31 July 2026 a live Hermes 0.19.0 session, fixing a genuine bug in
a small Python module with the hooks attached, produced a 33-event trace — and the
engine got it wrong in ways twenty synthetic cases had never shown.

The pattern that broke it is the most ordinary thing an agent does. It ran the same
verification command after every edit. The command never changed; the file on disk did,
and an argument hash cannot see a file. The loop detector counted seven identical calls
and fired at HIGH severity, outranking the three consecutive patch failures that were
the actual trouble — on a run that ended in success.

The lesson is not about the threshold. Synthetic traces only ever repeat a call when
something is genuinely stuck, so "identical arguments" and "making no progress" were
never distinguishable in them. They are trivially distinguishable in real work.

A third session then corrected the correction. Given a contradictory specification the
agent could not satisfy, it spent twenty-five steps writing a file and re-running the
same check, whose output alternated between two answers. Requiring outputs to be
identical — the obvious first fix — kept the engine silent on the one thing that had
really gone wrong. What actually separates the two is whether calls keep turning up
results that are *new*: 0.71 and 0.80 distinct outputs per call on the healthy cycles,
0.31 on the stuck one.

Both corrections left the synthetic benchmark at 94.4% top-1 and zero false positives,
which is the point. Real traces did not improve the score; they changed what the score
was failing to measure.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from conftest import START, run_end, run_start
from runopsy_cli import render
from runopsy_collector import RunSummary
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import (
    CallStatus,
    DiagnosisBundle,
    Event,
    RunOutcome,
    ToolCallEvent,
    ToolPayload,
)

RUN = "run_real"
ARGS = "sha256:" + "c" * 64


def call(
    sequence: int,
    *,
    name: str = "terminal",
    arguments: str = ARGS,
    output: str | None = None,
    exit_code: int = 0,
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(
            name=name,
            arguments_hash=arguments,
            output_hash=output,
            exit_code=exit_code,
            status=CallStatus.ERROR if exit_code else CallStatus.OK,
        ),
    )


def diagnosed(events: list[Event]) -> tuple[AnalysisContext, DiagnosisBundle]:
    context = AnalysisContext.from_events(RUN, events)
    return context, diagnose(context)


class TestRepeatingACommandThatMakesProgressIsNotALoop:
    def test_the_edit_verify_cycle_is_not_flagged(self) -> None:
        """The exact shape of the real session: same command, moving output."""
        events: list[Event] = [run_start(RUN, task="fix the bug")]
        events += [call(index, output=f"sha256:{index:064d}") for index in range(1, 8)]
        events.append(run_end(8, RUN, outcome=RunOutcome.SUCCESS))

        _, bundle = diagnosed(events)

        assert not any(
            "identical arguments" in candidate.summary for candidate in bundle.candidates
        )

    def test_alternating_between_two_answers_is_still_stuck(self) -> None:
        """The correction to the correction.

        Requiring outputs to be *identical* was the obvious first fix and it was too
        strict: a third real session spent twenty-five steps writing a file and
        re-running the same check while the check's output alternated between two
        answers. Nothing was identical and nothing was moving, and the engine stayed
        silent on the only thing that had actually gone wrong.
        """
        events: list[Event] = [run_start(RUN, task="fix the bug")]
        events += [
            call(index, output="sha256:" + ("e" if index % 2 else "f") * 64)
            for index in range(1, 17)
        ]
        events.append(run_end(17, RUN, outcome=RunOutcome.FAILURE))

        _, bundle = diagnosed(events)

        assert any("identical arguments" in candidate.summary for candidate in bundle.candidates)

    def test_the_same_answer_every_time_is_still_a_loop(self) -> None:
        """Repetition that learns nothing is what the detector was always for."""
        events: list[Event] = [run_start(RUN, task="fix the bug")]
        events += [call(index, output="sha256:" + "d" * 64) for index in range(1, 8)]
        events.append(run_end(8, RUN, outcome=RunOutcome.SUCCESS))

        _, bundle = diagnosed(events)

        assert any("identical arguments" in candidate.summary for candidate in bundle.candidates)

    def test_repeated_failure_is_a_loop_however_the_output_moves(self) -> None:
        """A retry storm stays a retry storm even when each error message differs."""
        events: list[Event] = [run_start(RUN, task="fix the bug")]
        events += [call(index, output=f"sha256:{index:064d}", exit_code=1) for index in range(1, 8)]
        events.append(run_end(8, RUN, outcome=RunOutcome.FAILURE))

        _, bundle = diagnosed(events)

        assert any("identical arguments" in candidate.summary for candidate in bundle.candidates)

    def test_an_unrecorded_output_falls_back_to_counting_arguments(self) -> None:
        """Silence about a possible loop is worse than a candidate the ranker can weigh."""
        events: list[Event] = [run_start(RUN, task="fix the bug")]
        events += [call(index) for index in range(1, 8)]
        events.append(run_end(8, RUN, outcome=RunOutcome.SUCCESS))

        _, bundle = diagnosed(events)

        assert any("identical arguments" in candidate.summary for candidate in bundle.candidates)


class TestASuccessfulRunIsNotDescribedAsAFailure:
    """The real session ended in success with three failed patches inside it."""

    def events(self, outcome: RunOutcome) -> list[Event]:
        return [
            run_start(RUN, task="fix the bug"),
            call(1, name="patch", arguments="sha256:" + "1" * 64, exit_code=1),
            call(2, name="patch", arguments="sha256:" + "2" * 64, exit_code=1),
            call(3, name="patch", arguments="sha256:" + "3" * 64, output="sha256:" + "9" * 64),
            run_end(4, RUN, outcome=outcome),
        ]

    def summary(self, outcome: RunOutcome) -> RunSummary:
        return RunSummary(
            run_id=RUN,
            task="fix the bug",
            runtime="hermes",
            event_count=5,
            outcome=outcome,
            started_at=START,
            ended_at=START + timedelta(seconds=4),
        )

    def rendered(self, outcome: RunOutcome) -> str:
        from rich.console import Console

        context, bundle = diagnosed(self.events(outcome))
        console = Console(width=100, no_color=True)
        with console.capture() as captured:
            console.print(render.diagnosis(bundle, context.graph, self.summary(outcome)))
        return captured.get()

    def test_a_recovered_step_is_not_called_an_observed_failure(self) -> None:
        text = self.rendered(RunOutcome.SUCCESS)

        assert "Recovered failure" in text
        assert "Observed failure" not in text

    def test_a_failed_run_still_reads_as_one(self) -> None:
        text = self.rendered(RunOutcome.FAILURE)

        assert "Observed failure" in text
        assert "Recovered failure" not in text

    def test_the_failing_steps_are_still_reported_either_way(self) -> None:
        """Recovered is not the same as uninteresting — three failed patches still ran."""
        _, bundle = diagnosed(self.events(RunOutcome.SUCCESS))

        assert bundle.candidates


class TestTheCommandWeOfferActuallyRuns:
    """Diagnosis ends by offering the command that would test its own suspicion.

    That command suggested `--dry-run` for two months. There is no such option — planning
    is what `replay` does unless `--execute` is passed — so the one action offered to
    turn a suspicion into something checkable failed with a usage error. Nothing caught
    it, because every test asserted on the text of the hint rather than running it.
    """

    def bundle_and_store(self, tmp_path: Path) -> tuple[str, Path]:
        from runopsy_collector import Collector

        store = tmp_path / "store"
        events = [
            run_start(RUN, task="fix the bug"),
            call(1, name="patch", arguments="sha256:" + "1" * 64, exit_code=1),
            call(2, name="pytest", arguments="sha256:" + "2" * 64, exit_code=1),
            run_end(3, RUN, outcome=RunOutcome.FAILURE),
        ]
        with Collector.open(store) as collector:
            collector.record_all(events)
        return RUN, store

    def test_the_suggested_replay_command_is_a_valid_invocation(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from runopsy_cli.main import app

        run_id, store = self.bundle_and_store(tmp_path)
        runner = CliRunner()

        diagnosis = runner.invoke(app, ["diagnose", run_id, "--store", str(store)])
        assert diagnosis.exit_code == 0, diagnosis.output

        # The hint is rendered inside a Rich panel, so strip the box drawing around it.
        match = re.search(r"runopsy replay [^|│\n]*", diagnosis.output)
        assert match, diagnosis.output
        suggested = match.group(0).strip()
        arguments = [*suggested.split()[1:], "--store", str(store)]

        replayed = runner.invoke(app, arguments)

        assert replayed.exit_code == 0, f"{suggested!r} failed:\n{replayed.output}"
        assert "No such option" not in replayed.output
