"""Executing a replay plan — the counterfactual half of the product.

Everything before this point produces suspicion; this is what can turn suspicion into
support. The claim being tested is causal, so the machinery is shaped like an
experiment rather than a rerun button:

- **One intervention, applied at the suspected onset.** Skip the step or substitute a
  corrected command. If more than one thing varies and the outcome changes, nothing can
  say which change did it — the plan already warns about this, and the executor only
  supports a single targeted intervention by construction.
- **A sandbox copy, never the working tree.** Steps run in a disposable copy of the
  project directory. The original run's files are evidence and must survive the
  experiment that interrogates them.
- **The verdict is computed, not asserted.** Downstream steps that failed in the
  original are re-run and compared. Only "the failures disappeared when the onset was
  changed" licenses support; reproduction without an intervention is consistency, not
  causation, and is labelled as such.

Commands come from the payload vault. A step whose payload was redacted is skipped and
reported: executing a command with ``[REDACTED]`` spliced into it would test nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from runopsy_adapter.recorder import EventSink, RunRecorder
from runopsy_core.detectors.base import AnalysisContext
from runopsy_core.schema import CallStatus, RunOutcome, ToolCallEvent
from runopsy_core.validate import ReplayEvidence
from runopsy_replay.plan import ReplayPlan, StepAction


class StoredPayload(Protocol):
    """What a vault entry must offer: the text, and whether it survived redaction."""

    text: str

    @property
    def executable(self) -> bool: ...


class PayloadSource(Protocol):
    """Digest in, payload out. Structural, so replay does not depend on the collector."""

    def get(self, digest: str) -> StoredPayload | None: ...


DEFAULT_SANDBOX_IGNORES: tuple[str, ...] = (
    ".runopsy*",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
)
DEFAULT_STEP_TIMEOUT_SECONDS = 600
OUTPUT_LIMIT = 100_000


@dataclass(frozen=True)
class ExecutedStep:
    """What happened when one original step was re-run."""

    original_sequence: int
    original_event_id: str
    command: str | None
    exit_code: int | None
    failed: bool
    skipped_reason: str | None = None

    @property
    def ran(self) -> bool:
        return self.skipped_reason is None


@dataclass(frozen=True)
class ReplayVerdict:
    """What the experiment showed, in terms the diagnosis can safely use."""

    replay_run_id: str
    parent_run_id: str
    intervention_kind: str | None
    intervention_target: int | None
    executed: tuple[ExecutedStep, ...] = ()
    original_downstream_failures: tuple[int, ...] = ()
    replayed_downstream_failures: tuple[int, ...] = ()
    skipped: tuple[ExecutedStep, ...] = ()

    @property
    def intervened(self) -> bool:
        return self.intervention_kind is not None and self.intervention_target is not None

    @property
    def outcome_changed(self) -> bool:
        """Whether the downstream failures observed originally disappeared."""
        rerun = {step.original_sequence for step in self.executed if step.ran}
        compared = [seq for seq in self.original_downstream_failures if seq in rerun]
        return bool(compared) and not any(
            seq in self.replayed_downstream_failures for seq in compared
        )

    @property
    def supports_onset(self) -> bool:
        """The only condition that licenses ``replay_supported``.

        An intervention was applied at the suspected onset, the downstream failures
        that existed in the original run were actually re-run, and they passed.
        """
        return self.intervened and self.outcome_changed

    @property
    def reproduced(self) -> bool:
        """Without an intervention: the original failures happened again.

        Consistency evidence only. It says the failure is deterministic enough to study;
        it does not say the onset caused it, and nothing downstream treats it as if it
        did.
        """
        if self.intervened:
            return False
        rerun = {step.original_sequence for step in self.executed if step.ran}
        compared = [seq for seq in self.original_downstream_failures if seq in rerun]
        return bool(compared) and all(seq in self.replayed_downstream_failures for seq in compared)


def evidence_from_stored_run(
    original_events: tuple[object, ...], child_events: tuple[object, ...]
) -> ReplayEvidence | None:
    """Reconstruct what a stored replay run established about its parent.

    Works from the journals alone, so a diagnosis can pick up replay evidence recorded
    in an earlier session — the upgrade does not depend on the process that ran the
    experiment still being alive.
    """
    from runopsy_core.schema import RunStartEvent

    start = next((e for e in child_events if isinstance(e, RunStartEvent)), None)
    if start is None or start.run.parent_run_id is None:
        return None
    target = start.run.intervention_target

    def failed(event: ToolCallEvent) -> bool:
        return event.tool.status is CallStatus.ERROR or (event.tool.exit_code or 0) != 0

    original_by_id = {e.event_id: e for e in original_events if isinstance(e, ToolCallEvent)}
    original_failures = {
        e.sequence
        for e in original_by_id.values()
        if target is not None and e.sequence > target and failed(e)
    }

    rerun: set[int] = set()
    rerun_failures: set[int] = set()
    for event in child_events:
        if not isinstance(event, ToolCallEvent) or not event.tool.retry_of:
            continue
        source = original_by_id.get(event.tool.retry_of)
        if source is None:
            continue
        rerun.add(source.sequence)
        if failed(event):
            rerun_failures.add(source.sequence)

    compared = original_failures & rerun
    outcome_changed = bool(compared) and not (compared & rerun_failures)

    return ReplayEvidence(
        replay_run_id=start.run_id,
        parent_run_id=start.run.parent_run_id,
        intervention_target=target,
        outcome_changed=outcome_changed,
        intervened=start.run.intervention_kind is not None and target is not None,
    )


def _copy_sandbox(source: Path, ignores: tuple[str, ...]) -> Path:
    """A disposable copy of the project for the experiment to run in."""
    root = Path(tempfile.mkdtemp(prefix="runopsy-replay-"))
    target = root / "work"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*ignores), symlinks=True)
    return target


def execute_plan(
    plan: ReplayPlan,
    context: AnalysisContext,
    vault: PayloadSource,
    sink: EventSink,
    *,
    replay_run_id: str,
    cwd: Path | None = None,
    substitute: str | None = None,
    skip_onset: bool = False,
    approve_unknown: bool = False,
    sandbox_ignores: tuple[str, ...] = DEFAULT_SANDBOX_IGNORES,
    timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS,
    keep_sandbox: bool = False,
) -> ReplayVerdict:
    """Run the replayable steps of ``plan`` in a sandbox and record a child run.

    ``substitute`` replaces the onset step's command; ``skip_onset`` omits it. At most
    one may be given — a second variable would make the result unreadable.
    """
    if substitute is not None and skip_onset:
        msg = "choose one intervention: substitute or skip, not both"
        raise ValueError(msg)

    intervention_kind = "substitute" if substitute is not None else "skip" if skip_onset else None
    target = plan.from_sequence if intervention_kind else None

    events_by_id = {event.event_id: event for event in context.events}
    original_failures = tuple(
        event.sequence
        for event in context.events
        if isinstance(event, ToolCallEvent)
        and event.sequence > plan.from_sequence
        and (event.tool.status is CallStatus.ERROR or (event.tool.exit_code or 0) != 0)
    )

    sandbox = _copy_sandbox(cwd or Path.cwd(), sandbox_ignores)
    executed: list[ExecutedStep] = []
    skipped: list[ExecutedStep] = []

    try:
        with RunRecorder(replay_run_id, sink) as recorder:
            recorder.start_run(
                task=f"replay of {plan.parent_run_id} from step {plan.from_sequence}",
                runtime="replay",
                parent_run_id=plan.parent_run_id,
                intervention_kind=intervention_kind,
                intervention_target=target,
            )

            for step in plan.steps:
                if step.action is StepAction.SKIP:
                    continue
                original = events_by_id.get(step.node_id)
                outcome = _execute_step(
                    step_sequence=step.sequence,
                    step_node_id=step.node_id,
                    action=step.action,
                    original=original if isinstance(original, ToolCallEvent) else None,
                    vault=vault,
                    recorder=recorder,
                    sandbox=sandbox,
                    substitute=substitute if step.sequence == plan.from_sequence else None,
                    skip_onset=skip_onset and step.sequence == plan.from_sequence,
                    approve_unknown=approve_unknown,
                    timeout_seconds=timeout_seconds,
                )
                (executed if outcome.ran else skipped).append(outcome)

            replay_failures = tuple(step.original_sequence for step in executed if step.failed)
            recorder.end_run(
                RunOutcome.FAILURE if replay_failures else RunOutcome.SUCCESS,
                summary=(f"{len(replay_failures)} of {len(executed)} replayed steps failed"),
            )
    finally:
        if not keep_sandbox:
            shutil.rmtree(sandbox.parent, ignore_errors=True)

    return ReplayVerdict(
        replay_run_id=replay_run_id,
        parent_run_id=plan.parent_run_id,
        intervention_kind=intervention_kind,
        intervention_target=target,
        executed=tuple(executed),
        original_downstream_failures=original_failures,
        replayed_downstream_failures=tuple(
            step.original_sequence for step in executed if step.failed
        ),
        skipped=tuple(skipped),
    )


def _execute_step(
    *,
    step_sequence: int,
    step_node_id: str,
    action: StepAction,
    original: ToolCallEvent | None,
    vault: PayloadSource,
    recorder: RunRecorder,
    sandbox: Path,
    substitute: str | None,
    skip_onset: bool,
    approve_unknown: bool,
    timeout_seconds: int,
) -> ExecutedStep:
    def skipped(reason: str) -> ExecutedStep:
        return ExecutedStep(
            original_sequence=step_sequence,
            original_event_id=step_node_id,
            command=None,
            exit_code=None,
            failed=False,
            skipped_reason=reason,
        )

    if skip_onset:
        return skipped("intervention: onset step deliberately skipped")
    if action is StepAction.BLOCK:
        return skipped("plan marked this step blocked")
    if action is StepAction.APPROVE and not approve_unknown:
        return skipped("unrecognised tool and no approval was given")
    if original is None:
        return skipped("not a tool call")

    command = substitute
    if command is None:
        digest = original.tool.arguments_hash
        entry = vault.get(digest) if digest else None
        if entry is None:
            return skipped("command text not in the local vault")
        if not entry.executable:
            return skipped("payload was redacted; refusing to run a censored command")
        command = entry.text

    started = time.perf_counter()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        output = (completed.stdout + completed.stderr)[:OUTPUT_LIMIT]
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = 124
        output = f"timed out after {timeout_seconds}s"

    recorder.tool_call(
        original.tool.name if substitute is None else f"{original.tool.name} (substituted)",
        arguments=command,
        output=output,
        exit_code=exit_code,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status=CallStatus.TIMEOUT if timed_out else None,
        retry_of=step_node_id,
    )
    return ExecutedStep(
        original_sequence=step_sequence,
        original_event_id=step_node_id,
        command=command,
        exit_code=exit_code,
        failed=timed_out or exit_code != 0,
    )
