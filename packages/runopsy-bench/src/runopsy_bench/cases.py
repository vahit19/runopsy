"""Synthetic traces with known ground truth.

Each case is a small run built so that the step where it broke is a matter of record
rather than of opinion. They are deliberately short: a fixture whose ground truth needs
an argument is not ground truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    ClaimEvent,
    ClaimPayload,
    Event,
    FailureCategory,
    HandoffEvent,
    HandoffPayload,
    LlmCallEvent,
    LlmPayload,
    MemoryOperation,
    MemoryOpEvent,
    MemoryPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    StateChange,
    SupportStatus,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

START = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
RUN = "run_case"


@dataclass(frozen=True)
class SyntheticCase:
    """One labelled scenario."""

    name: str
    category: FailureCategory
    description: str
    events: tuple[Event, ...]
    onset_step: int | None
    """Sequence number where the run actually started going wrong. None for healthy runs."""

    deterministically_detectable: bool = True
    """False when nothing in the recorded trace is anomalous at the onset.

    These are the cases structural analysis cannot reach by construction. They are kept
    in the suite and reported separately, because a benchmark that only contains
    problems the engine can already solve measures nothing.
    """

    affected_steps: frozenset[int] = field(default_factory=frozenset)

    @property
    def is_healthy(self) -> bool:
        return self.onset_step is None


def _tool(
    step: int,
    name: str = "terminal",
    *,
    exit_code: int = 0,
    status: CallStatus = CallStatus.OK,
    arguments: str | None = None,
    retry_of: int | None = None,
    state: dict[str, object] | None = None,
    blocked_reason: str | None = None,
) -> Event:
    resolved_status = CallStatus.ERROR if exit_code and status is CallStatus.OK else status
    return ToolCallEvent(
        event_id=f"evt_{step:02d}",
        run_id=RUN,
        sequence=step,
        timestamp=START + timedelta(seconds=step),
        tool=ToolPayload(
            name=name,
            exit_code=exit_code,
            status=resolved_status,
            arguments_hash=hash_text(arguments) if arguments is not None else None,
            retry_of=f"evt_{retry_of:02d}" if retry_of is not None else None,
            blocked_reason=blocked_reason,
        ),
        state_delta={key: StateChange(after=value) for key, value in (state or {}).items()},
    )


def _llm(
    step: int,
    *,
    status: CallStatus = CallStatus.OK,
    finish_reason: str | None = None,
    tokens: int = 500,
) -> Event:
    return LlmCallEvent(
        event_id=f"evt_{step:02d}",
        run_id=RUN,
        sequence=step,
        timestamp=START + timedelta(seconds=step),
        llm=LlmPayload(
            model="local:qwen",
            status=status,
            finish_reason=finish_reason,
            tokens=TokenUsage(input_tokens=tokens, output_tokens=tokens // 4),
        ),
    )


def _start(step: int = 0, task: str = "synthetic case") -> Event:
    return RunStartEvent(
        event_id=f"evt_{step:02d}",
        run_id=RUN,
        sequence=step,
        timestamp=START,
        run=RunPayload(task=task, runtime="synthetic"),
    )


def _end(step: int, outcome: RunOutcome = RunOutcome.FAILURE) -> Event:
    return RunEndEvent(
        event_id=f"evt_{step:02d}",
        run_id=RUN,
        sequence=step,
        timestamp=START + timedelta(seconds=step),
        run=RunPayload(outcome=outcome),
    )


def _case(
    name: str,
    category: FailureCategory,
    description: str,
    events: Sequence[Event],
    onset_step: int | None,
    *,
    detectable: bool = True,
    affected: Sequence[int] = (),
) -> SyntheticCase:
    return SyntheticCase(
        name=name,
        category=category,
        description=description,
        events=tuple(events),
        onset_step=onset_step,
        deterministically_detectable=detectable,
        affected_steps=frozenset(affected),
    )


def all_cases() -> tuple[SyntheticCase, ...]:
    """Every labelled case in the suite."""
    return (
        _case(
            "tool_exit_code",
            FailureCategory.TOOL_EXECUTION,
            "a build step fails and the run continues on a broken tree",
            [
                _start(),
                _tool(3, "make"),
                _tool(5, "make", exit_code=2),
                _tool(8, "pytest", exit_code=1),
                _end(9),
            ],
            onset_step=5,
            affected=[8],
        ),
        _case(
            "tool_timeout",
            FailureCategory.TOOL_EXECUTION,
            "a slow command times out and its output is never produced",
            [
                _start(),
                _tool(4, "fetch_schema", status=CallStatus.TIMEOUT),
                _tool(7, "migrate", exit_code=1),
                _end(8),
            ],
            onset_step=4,
            affected=[7],
        ),
        _case(
            "blocked_action",
            FailureCategory.SAFETY,
            "a destructive command is refused by policy and the task cannot finish",
            [
                _start(),
                _tool(3, "rm", status=CallStatus.BLOCKED, blocked_reason="destructive"),
                _end(4),
            ],
            onset_step=3,
        ),
        _case(
            "model_error",
            FailureCategory.PLANNING,
            "the provider rejects a call and the agent proceeds without a plan",
            [
                _start(),
                _llm(2, status=CallStatus.ERROR),
                _tool(5, "edit_file"),
                _tool(7, "pytest", exit_code=1),
                _end(8),
            ],
            onset_step=2,
            affected=[5, 7],
        ),
        _case(
            "truncated_plan",
            FailureCategory.PLANNING,
            "a plan is cut off by the length limit and the agent acts on half of it",
            [
                _start(),
                _llm(2, finish_reason="length"),
                _tool(4, "edit_file"),
                _tool(6, "pytest", exit_code=1),
                _end(7),
            ],
            onset_step=2,
            affected=[4, 6],
        ),
        _case(
            "retry_storm",
            FailureCategory.CONTROL_FLOW,
            "one failing call is retried four times without changing anything",
            [
                _start(),
                _tool(2, "deploy", exit_code=1),
                *(_tool(step, "deploy", exit_code=1, retry_of=2) for step in (3, 4, 5, 6)),
                _end(7),
            ],
            onset_step=2,
        ),
        _case(
            "tool_loop",
            FailureCategory.CONTROL_FLOW,
            "the same command with identical arguments runs four times",
            [
                _start(),
                *(_tool(step, "pytest", arguments="-x tests/") for step in (2, 3, 4, 5)),
                _end(6, RunOutcome.FAILURE),
            ],
            onset_step=2,
        ),
        _case(
            "state_flapping",
            FailureCategory.STATE,
            "two steps disagree about whether the migration ran",
            [
                _start(),
                _tool(2, "migrate", state={"migrated": True}),
                _tool(3, "rollback", state={"migrated": False}),
                _tool(4, "migrate", state={"migrated": True}),
                _tool(5, "rollback", state={"migrated": False}),
                _tool(7, "pytest", exit_code=1),
                _end(8),
            ],
            onset_step=2,
            affected=[7],
        ),
        _case(
            "stale_memory",
            FailureCategory.MEMORY,
            "a deploy target recalled from an old session is no longer valid",
            [
                _start(),
                MemoryOpEvent(
                    event_id="evt_03",
                    run_id=RUN,
                    sequence=3,
                    timestamp=START + timedelta(seconds=3),
                    memory=MemoryPayload(
                        operation=MemoryOperation.READ, key="deploy_target", age_seconds=900_000
                    ),
                ),
                _tool(5, "deploy", exit_code=1),
                _end(6),
            ],
            onset_step=3,
            affected=[5],
        ),
        _case(
            "incomplete_handoff",
            FailureCategory.HANDOFF,
            "a sub-agent is given a task without the branch it should work on",
            [
                _start(),
                HandoffEvent(
                    event_id="evt_02",
                    run_id=RUN,
                    sequence=2,
                    timestamp=START + timedelta(seconds=2),
                    handoff=HandoffPayload(
                        from_agent_id="main", to_agent_id="tester", missing_fields=("branch",)
                    ),
                ),
                _tool(5, "pytest", exit_code=1),
                _end(6),
            ],
            onset_step=2,
        ),
        _case(
            "unsupported_claim",
            FailureCategory.REASONING,
            "the agent asserts the fix works with nothing backing it",
            [
                _start(),
                ClaimEvent(
                    event_id="evt_04",
                    run_id=RUN,
                    sequence=4,
                    timestamp=START + timedelta(seconds=4),
                    claim=ClaimPayload(
                        claim_id="c1",
                        text_hash=hash_text("the fix works"),
                        support_status=SupportStatus.UNSUPPORTED,
                    ),
                ),
                _tool(6, "pytest", exit_code=1),
                _end(7),
            ],
            onset_step=4,
        ),
        _case(
            "contradicted_claim",
            FailureCategory.REASONING,
            "the agent asserts success while the evidence says otherwise",
            [
                _start(),
                ClaimEvent(
                    event_id="evt_03",
                    run_id=RUN,
                    sequence=3,
                    timestamp=START + timedelta(seconds=3),
                    claim=ClaimPayload(
                        claim_id="c1",
                        text_hash=hash_text("tests pass"),
                        support_status=SupportStatus.CONTRADICTED,
                    ),
                ),
                _tool(5, "pytest", exit_code=1),
                _end(6),
            ],
            onset_step=3,
        ),
        _case(
            "outcome_mismatch",
            FailureCategory.OUTCOME,
            "the run reports success while a step plainly failed",
            [_start(), _tool(3, "pytest", exit_code=1), _end(5, RunOutcome.SUCCESS)],
            onset_step=3,
        ),
        _case(
            "interrupted_run",
            FailureCategory.OUTCOME,
            "the process is killed before it can report an outcome",
            [_start(), _tool(3, "build"), _tool(6, "pytest", exit_code=1)],
            onset_step=6,
        ),
        _case(
            "missing_run_start",
            FailureCategory.GOAL_INPUT,
            "the stream begins mid-run, so the task is unknown",
            [_tool(4, "edit_file"), _tool(6, "pytest", exit_code=1), _end(7)],
            onset_step=6,
        ),
        _case(
            "trace_gap",
            FailureCategory.VALIDATION,
            "events are missing, so any conclusion is drawn over a hole",
            [_start(), _tool(2, "build"), _tool(9, "pytest", exit_code=1), _end(10)],
            onset_step=9,
        ),
        _case(
            "early_failure_late_symptom",
            FailureCategory.TOOL_EXECUTION,
            "the run breaks at step 4 but nothing visible happens until step 20",
            [
                _start(),
                _tool(4, "write_config", exit_code=1, state={"endpoint": "staging"}),
                *(_tool(step, "edit_file") for step in (6, 8, 10, 12, 14, 16, 18)),
                _tool(20, "pytest", exit_code=1),
                _end(21),
            ],
            onset_step=4,
            affected=[20],
        ),
        _case(
            "two_failures_earliest_wins",
            FailureCategory.TOOL_EXECUTION,
            "two steps fail; the earlier one is where the run went wrong",
            [
                _start(),
                _tool(5, "fetch_deps", exit_code=1),
                _tool(9, "build", exit_code=1),
                _tool(12, "pytest", exit_code=1),
                _end(13),
            ],
            onset_step=5,
            affected=[9, 12],
        ),
        _case(
            "silent_wrong_config",
            FailureCategory.TOOL_SELECTION,
            "a write succeeds but stores the wrong environment; nothing looks anomalous",
            [
                _start(),
                _tool(4, "write_config", state={"endpoint": "staging"}),
                *(_tool(step, "edit_file") for step in (6, 8, 10)),
                _tool(14, "pytest", exit_code=1),
                _end(15),
            ],
            onset_step=4,
            detectable=False,
            affected=[14],
        ),
        _case(
            "healthy_run",
            FailureCategory.OUTCOME,
            "a clean run that must produce no finding at all",
            [
                _start(task="add a changelog entry"),
                _llm(1),
                _tool(2, "edit_file"),
                _tool(3, "pytest"),
                _end(4, RunOutcome.SUCCESS),
            ],
            onset_step=None,
        ),
    )
