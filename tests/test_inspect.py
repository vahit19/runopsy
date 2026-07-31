"""Reading Inspect AI eval logs into Runopsy traces.

Built against Inspect's own typed models rather than a hand-written fixture of what its
JSON looks like, for the same reason the Hermes adapter was built against the wire
protocol in its source: a fixture agrees with whatever the person writing it believed,
and keeps agreeing after the real format moves.

That discipline paid immediately. Two assumptions in the first draft were wrong —
`started_at` is a string rather than a datetime, and `EvalSpec` requires a `config` —
and both were caught by constructing a real log instead of a plausible one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import (
    CallStatus,
    Event,
    LlmCallEvent,
    RunEndEvent,
    RunOutcome,
    ToolCallEvent,
)

inspect_log = pytest.importorskip("inspect_ai.log", reason="inspect-ai is not installed")

from inspect_ai.event import ModelEvent, ToolEvent  # noqa: E402
from inspect_ai.log import (  # noqa: E402
    EvalConfig,
    EvalLog,
    EvalSample,
    EvalSampleScore,
    EvalSpec,
)
from inspect_ai.model import ModelOutput, ModelUsage  # noqa: E402
from inspect_ai.tool._tool_call import ToolCallError  # noqa: E402
from runopsy_inspect import log_to_runs, run_id_for, sample_to_events  # noqa: E402

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


def only_sample(log: EvalLog) -> EvalSample:
    """`EvalLog.samples` is optional in Inspect's model; every log here has one."""
    assert log.samples
    return log.samples[0]


def tool_event(
    function: str, arguments: dict[str, object], result: str, *, error: str | None = None, at: int
) -> ToolEvent:
    return ToolEvent(
        timestamp=NOW.replace(second=at),
        id=f"call-{at}",
        function=function,
        arguments=arguments,
        result=result,
        error=ToolCallError(type="unknown", message=error) if error else None,
    )


def model_event(*, at: int, input_tokens: int = 120, output_tokens: int = 30) -> ModelEvent:
    return ModelEvent(
        timestamp=NOW.replace(second=at),
        model="openai/gpt-4o-mini",
        input=[],
        tools=[],
        tool_choice="auto",
        config={},
        output=ModelOutput(
            model="openai/gpt-4o-mini",
            choices=[],
            completion="trying something",
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        ),
    )


def build_log(
    *, events: list[object] | None = None, score: str | None = "I", sample_id: str = "s1"
) -> EvalLog:
    sample = EvalSample(
        id=sample_id,
        epoch=1,
        input="fix the failing test",
        target="",
        messages=[],
        started_at=NOW.isoformat(),
        completed_at=NOW.replace(minute=5).isoformat(),
        scores={"accuracy": EvalSampleScore(value=score)} if score is not None else {},
        events=events
        if events is not None
        else [
            tool_event("bash", {"cmd": "pytest"}, "2 failed", error="exit 1", at=2),
            model_event(at=3),
            tool_event("edit", {"path": "a.py"}, "ok", at=4),
        ],
    )
    log = EvalLog(
        version=2,
        status="success",
        eval=EvalSpec(
            eval_id="ev1",
            run_id="r1",
            created=NOW.isoformat(),
            task="demo",
            dataset={},
            model="openai/gpt-4o-mini",
            config=EvalConfig(),
        ),
    )
    log.samples = [sample]
    return log


class TestTheShapeOfAConvertedRun:
    def test_a_sample_becomes_one_run_bracketed_by_start_and_end(self) -> None:
        events = sample_to_events(build_log(), only_sample(build_log()))

        assert events[0].kind.value == "run_start"
        assert events[-1].kind.value == "run_end"

    def test_each_sample_is_its_own_run(self) -> None:
        """A single trace over every epoch would let propagation reach between attempts.

        That is the "nothing may affect the past" invariant, violated sideways.
        """
        log = build_log()
        log.samples = [
            only_sample(log),
            EvalSample(**{**only_sample(log).model_dump(), "id": "s2"}),
        ]

        assert len(log_to_runs(log)) == 2

    def test_the_run_id_is_stable_across_reads(self) -> None:
        """Re-importing the same log must not create a second copy of the run."""
        log = build_log()

        first = run_id_for(log, only_sample(log))
        second = run_id_for(build_log(), only_sample(build_log()))

        assert first == second

    def test_the_run_id_is_filesystem_safe(self) -> None:
        log = build_log(sample_id="../../etc/passwd")

        assert "/" not in run_id_for(log, only_sample(log))


class TestWhatInspectStatesIsWhatIsRecorded:
    def convert(self, **kwargs: object) -> list[Event]:
        log = build_log(**kwargs)  # type: ignore[arg-type]
        return sample_to_events(log, only_sample(log))

    def final(self, **kwargs: object) -> RunEndEvent:
        last = self.convert(**kwargs)[-1]
        assert isinstance(last, RunEndEvent)
        return last

    def test_a_tool_error_becomes_a_failed_step(self) -> None:
        tools = [e for e in self.convert() if isinstance(e, ToolCallEvent)]

        failed = next(e for e in tools if e.tool.name == "bash")
        assert failed.tool.exit_code == 1
        assert failed.tool.status is CallStatus.ERROR

    def test_a_tool_without_an_error_is_recorded_as_successful(self) -> None:
        """Never inferred from the look of the output — that would manufacture findings."""
        events = self.convert(
            events=[tool_event("bash", {"cmd": "x"}, "FAIL FAIL everything broke", at=2)]
        )

        tool = next(e for e in events if isinstance(e, ToolCallEvent))
        assert tool.tool.status is CallStatus.OK

    def test_token_usage_survives_the_conversion(self) -> None:
        """Inspect reports usage, so the budget layer works here even though Hermes
        cannot supply it through shell hooks."""
        call = next(e for e in self.convert() if isinstance(e, LlmCallEvent))

        assert call.llm.tokens.input_tokens == 120
        assert call.llm.tokens.output_tokens == 30

    @pytest.mark.parametrize(
        ("score", "outcome"),
        [("C", RunOutcome.SUCCESS), ("I", RunOutcome.FAILURE), (None, RunOutcome.UNKNOWN)],
    )
    def test_the_score_decides_the_outcome(self, score: str | None, outcome: RunOutcome) -> None:
        assert self.final(score=score).run.outcome is outcome

    def test_an_unrecognised_score_is_left_unknown_rather_than_guessed(self) -> None:
        """A sample wrongly marked failing would put a fabricated failure in the corpus."""
        assert self.final(score="partial-credit-0.4").run.outcome is RunOutcome.UNKNOWN

    def test_bookkeeping_events_do_not_become_steps(self) -> None:
        """Spans and store writes describe how Inspect ran the sample, not what the
        agent did; inventing steps from them would inflate the loop detectors."""
        from inspect_ai.event import StoreEvent

        events = self.convert(
            events=[
                StoreEvent(timestamp=NOW, changes=[]),
                tool_event("bash", {"cmd": "x"}, "ok", at=2),
            ]
        )

        assert len([e for e in events if isinstance(e, ToolCallEvent)]) == 1


class TestItSpansMoreThanOneUpstreamVersion:
    """Fields inspect-ai added later must not be required by this adapter.

    Found the hard way. The adapter was written against 0.3.251 and read
    `sample.started_at` directly; a security constraint then forced the supported
    version down to 0.3.145, where that attribute does not exist — and every
    conversion raised. A field appearing in a later release should widen what this can
    read, never narrow it.
    """

    def test_a_sample_without_timestamps_still_converts(self) -> None:
        log = build_log()
        sample = only_sample(log)
        object.__setattr__(sample, "__dict__", {**sample.__dict__})
        sample.__dict__.pop("started_at", None)
        sample.__dict__.pop("completed_at", None)

        events = sample_to_events(log, sample)

        assert events[0].kind.value == "run_start"
        assert events[-1].kind.value == "run_end"

    def test_timestamps_are_ordered_even_when_only_one_is_known(self) -> None:
        """A run that ends before it starts would break every downstream ordering."""
        log = build_log()
        sample = only_sample(log)
        sample.__dict__.pop("completed_at", None)

        events = sample_to_events(log, sample)

        assert events[-1].timestamp >= events[0].timestamp


class TestTheEngineCanReadTheResult:
    def test_a_converted_trace_diagnoses_normally(self) -> None:
        log = build_log()
        run_id = run_id_for(log, only_sample(log))

        events = sample_to_events(log, only_sample(log))
        bundle = diagnose(AnalysisContext.from_events(run_id, events))

        assert bundle.primary is not None
        assert "bash" in bundle.primary.summary

    def test_a_clean_sample_produces_no_finding(self) -> None:
        """The zero-false-positive invariant has to hold on imported traces too."""
        log = build_log(
            score="C",
            events=[
                tool_event("bash", {"cmd": "pytest"}, "all passed", at=2),
                tool_event("edit", {"path": "a.py"}, "ok", at=3),
            ],
        )
        run_id = run_id_for(log, only_sample(log))

        bundle = diagnose(
            AnalysisContext.from_events(run_id, sample_to_events(log, only_sample(log)))
        )

        assert bundle.candidates == ()
