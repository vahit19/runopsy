"""A working adapter that records real command executions.

Every number in this repository until now came from traces we wrote ourselves. This is
the first thing that captures a run nobody staged: real commands, real exit codes, real
timing, real output. It is not an agent runtime, but the events it produces are the same
events an agent runtime produces, so the engine finally sees data it did not help
invent.

It is also useful on its own. Wrapping a build or test pipeline gives you the failure
onset for free, and it is the fastest way for someone to try Runopsy without installing
an agent framework first.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from runopsy_adapter.recorder import EventSink, PayloadStore, RunRecorder
from runopsy_adapter.repo import Observation, RepositoryWatch, capture_patch
from runopsy_core.schema import CallStatus, RunOutcome

ADAPTER_NAME = "shell"
DEFAULT_TIMEOUT_SECONDS = 600
OUTPUT_LIMIT = 100_000


@dataclass(frozen=True)
class StepOutcome:
    """What one recorded command did."""

    command: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    node_id: str

    @property
    def failed(self) -> bool:
        return self.timed_out or self.exit_code != 0


def _tool_name(command: str) -> str:
    """Name the step after the program being run, which is how a person refers to it.

    Splitting is platform-aware because ``shlex`` treats a backslash as an escape in
    POSIX mode, which silently shreds every Windows path into one unreadable token.
    """
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = command.split()
    if not parts:
        return "command"
    return Path(parts[0].strip("\"'")).stem or "command"


def _look(watch: RepositoryWatch | None, cwd: Path) -> Observation | None:
    """Observe the repository, or decide there is nothing to say.

    Swallows everything. Watching the working tree is an enrichment of the trace, and an
    enrichment that can stop a pipeline is a defect: the command already ran, and its
    result is the thing the user asked to record.
    """
    if watch is None:
        return None
    try:
        return watch.observe(cwd)
    except Exception:
        return None


def _observe_repository(
    recorder: RunRecorder,
    watch: RepositoryWatch | None,
    cwd: Path,
    vault: PayloadStore | None,
) -> None:
    """Record the repository as it stood before the first command ran.

    Without this baseline the first step's changes have nothing to be changes *from*,
    and a run that began on a dirty tree would look as though the agent made the mess.
    """
    observed = _look(watch, cwd)
    if observed is not None:
        recorder.state_snapshot(observed.state.values())
        _record_checkpoint(recorder, observed, cwd, vault)


def _record_checkpoint(
    recorder: RunRecorder,
    observed: Observation,
    cwd: Path,
    vault: PayloadStore | None,
) -> None:
    """A point this run can be returned to, wherever the tree moved.

    ``runopsy replay`` has always looked for these and never found one, so every plan it
    produced carried the warning that file state could not be restored. Nothing was
    taking them: a checkpoint needs the working tree, and the trace held only commands.

    The commit and the uncommitted changes together reconstruct the tree exactly. The
    patch goes to Runopsy's vault — secret-scanned like every other payload, and
    deletable — rather than into the user's repository, for the same reason the store
    excludes itself from their commits.
    """
    if observed.state.head is None or vault is None:
        # No commit to anchor against, or nowhere to keep the changes. Either way a
        # checkpoint could be named but not restored, which is the situation this exists
        # to end rather than to reproduce more quietly.
        return
    try:
        patch = capture_patch(cwd)
        digest = vault.put(patch) if patch else None
    except Exception:
        return
    recorder.checkpoint(
        f"{recorder.run_id}_ck_{recorder.sequence:04d}",
        repo_state=observed.state.head,
        patch_digest=digest,
    )


def record_steps(
    commands: list[str],
    *,
    run_id: str,
    task: str,
    sink: EventSink,
    vault: PayloadStore | None = None,
    cwd: Path | None = None,
    stop_on_failure: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    capture_git: bool = True,
) -> tuple[StepOutcome, ...]:
    """Run each command in order, recording it, and return what happened.

    Execution continues past a failure by default. That is what makes the resulting
    trace interesting: an agent carries on after a step goes wrong, and the gap between
    the step that broke and the step where it became visible is the thing Runopsy
    exists to close. Stopping at the first error would only ever produce traces whose
    onset is also the symptom.
    """
    outcomes: list[StepOutcome] = []
    # In-process, so the cursor lives in memory: this adapter records a whole run without
    # ever leaving, unlike the hook path where every event is a new process.
    watch = RepositoryWatch() if capture_git else None
    working_directory = cwd or Path.cwd()

    with RunRecorder(run_id, sink, vault=vault) as recorder:
        recorder.start_run(
            task=task,
            runtime=ADAPTER_NAME,
            repo=Path(working_directory).name,
        )
        _observe_repository(recorder, watch, working_directory, vault)

        for command in commands:
            started = time.perf_counter()
            timed_out = False
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd,
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

            duration_ms = int((time.perf_counter() - started) * 1000)
            # Looked at before the step is written, so the commit and branch it moved to
            # can be carried on the step itself rather than on an event beside it.
            observed = _look(watch, working_directory)
            node_id = recorder.tool_call(
                _tool_name(command),
                arguments=command,
                output=output,
                exit_code=exit_code,
                duration_ms=duration_ms,
                status=CallStatus.TIMEOUT if timed_out else None,
                # Deliberately no state_delta. An earlier version recorded the exit code
                # as state, which made every pipeline look like a state conflict: the
                # value necessarily changes as steps succeed and fail, so the flapping
                # detector fired on it and nominated the first step of every run as the
                # onset. state_delta is for facts the run believes about the world, not
                # for restating a field the event already carries.
                #
                # What the repository did is a different matter: it is a fact about the
                # world rather than a restatement of the event, and it is the one thing a
                # coding agent's trace was missing.
                state_delta=observed.deltas if observed else None,
            )
            if observed is not None and observed.worth_a_snapshot:
                recorder.state_snapshot(observed.state.values())
                _record_checkpoint(recorder, observed, working_directory, vault)

            outcome = StepOutcome(
                command=command,
                exit_code=exit_code,
                duration_ms=duration_ms,
                timed_out=timed_out,
                node_id=node_id,
            )
            outcomes.append(outcome)

            if outcome.failed and stop_on_failure:
                break

        failed = any(outcome.failed for outcome in outcomes)
        recorder.end_run(
            RunOutcome.FAILURE if failed else RunOutcome.SUCCESS,
            summary=(
                f"{sum(o.failed for o in outcomes)} of {len(outcomes)} steps failed"
                if failed
                else f"all {len(outcomes)} steps succeeded"
            ),
        )

    return tuple(outcomes)
