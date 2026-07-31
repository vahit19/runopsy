"""The shipped examples, run and checked.

Examples are the first thing a new reader executes and the first thing to rot, because
nothing else imports them. Each one here is asserted to produce the onset its own
docstring claims — a demo that quietly stops demonstrating its point is worse than no
demo, since the reader concludes the tool does not work.

The three cover deliberately different failure shapes:

- ``coding_failure`` — a failed write, visible five steps before the symptom
- ``multi_agent_handoff`` — a subagent that returned nothing; every exit code zero
- ``research_failure`` — a claim outrunning its evidence; the run reports success
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from runopsy_core import AnalysisContext, diagnose
from runopsy_core.schema import DiagnosisBundle, Event, ToolCallEvent

ROOT = Path(__file__).resolve().parents[1]

EXAMPLES = [
    pytest.param("coding_failure", 9, id="coding_failure"),
    pytest.param("multi_agent_handoff", 6, id="multi_agent_handoff"),
    pytest.param("research_failure", 4, id="research_failure"),
]


def load(name: str) -> Any:
    """Import an example's seed module without installing it."""
    path = ROOT / "examples" / name / "seed.py"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def analysed(name: str) -> tuple[list[Event], DiagnosisBundle]:
    events = load(name).trace()
    return events, diagnose(AnalysisContext.from_events(events[0].run_id, events))


@pytest.mark.parametrize(("name", "onset_step"), EXAMPLES)
class TestEveryExampleStillMakesItsPoint:
    def test_the_trace_is_well_formed(self, name: str, onset_step: int) -> None:
        from runopsy_adapter import assert_adapter_contract

        events, _ = analysed(name)

        assert_adapter_contract(events)

    def test_it_finds_the_onset_the_docstring_promises(self, name: str, onset_step: int) -> None:
        events, bundle = analysed(name)

        assert bundle.primary is not None, f"{name} produced no finding"
        node = next(e for e in events if e.event_id == bundle.primary.onset_node_id)
        assert node.sequence == onset_step

    def test_the_onset_is_earlier_than_the_visible_symptom(
        self, name: str, onset_step: int
    ) -> None:
        """The entire premise: reading from the end sends you to the wrong step."""
        events, bundle = analysed(name)
        if bundle.observed_failure_node_id is None:
            pytest.skip(f"{name} has no visibly failing step, which is its own point")

        symptom = next(e for e in events if e.event_id == bundle.observed_failure_node_id)
        assert onset_step < symptom.sequence

    def test_nothing_is_claimed_as_a_proven_cause(self, name: str, onset_step: int) -> None:
        from runopsy_cli.language import asserts_causation

        _, bundle = analysed(name)

        assert not asserts_causation(bundle.primary.summary if bundle.primary else "")
        assert bundle.primary is not None
        assert bundle.primary.confidence <= 0.75


class TestTheExamplesCoverDifferentGround:
    def test_all_three_from_the_design_document_exist(self) -> None:
        for param in EXAMPLES:
            name = param.values[0]
            assert (ROOT / "examples" / str(name) / "seed.py").is_file()

    def test_the_handoff_example_fails_with_every_exit_code_zero(self) -> None:
        """Its whole value: tool-level tracing sees nothing wrong here."""
        events, _ = analysed("multi_agent_handoff")

        failures = [
            event for event in events if isinstance(event, ToolCallEvent) and event.tool.exit_code
        ]
        assert [event.sequence for event in failures] == [10]

    def test_the_research_example_reports_success(self) -> None:
        """A run can be wrong and finish cleanly; that is the hardest case to surface."""
        from runopsy_core.schema import RunEndEvent, RunOutcome

        events, _ = analysed("research_failure")

        end = next(event for event in events if isinstance(event, RunEndEvent))
        assert end.run.outcome is RunOutcome.SUCCESS
