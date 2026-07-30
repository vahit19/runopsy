"""Planning a replay without performing one.

A plan is a proposal, never an action. It says which step it would return to, what it
would re-run, what it refuses to re-run, and — importantly — what it cannot promise.
Producing the plan separately from executing it is what makes the risky part reviewable
by a person before anything happens.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from runopsy_core.detectors.base import AnalysisContext
from runopsy_core.schema import CheckpointEvent, Identifier, ReplayLevel, ToolCallEvent
from runopsy_replay.risk import NEVER_AUTOMATIC, SideEffect, classify


class StepAction(StrEnum):
    """What the plan proposes to do with one recorded step."""

    SKIP = "skip"
    """Before the replay point. Left untouched."""

    REPLAY = "replay"
    """Safe to run again as-is."""

    SANDBOX = "sandbox"
    """Writes files; runs only inside the fork or worktree."""

    APPROVE = "approve"
    """Unclassified. A person must confirm before it runs."""

    BLOCK = "block"
    """Reaches outside the machine or destroys something. Excluded from replay."""


_ACTION_FOR: dict[SideEffect, StepAction] = {
    SideEffect.READ_ONLY: StepAction.REPLAY,
    SideEffect.LOCAL_WRITE: StepAction.SANDBOX,
    SideEffect.EXTERNAL: StepAction.BLOCK,
    SideEffect.DESTRUCTIVE: StepAction.BLOCK,
    SideEffect.UNKNOWN: StepAction.APPROVE,
}


class PlannedStep(BaseModel):
    """One step of the proposed replay."""

    model_config = ConfigDict(frozen=True)

    node_id: Identifier
    sequence: int = Field(ge=0)
    label: str
    action: StepAction
    side_effect: SideEffect
    reason: str


class Intervention(BaseModel):
    """What the replay would change relative to the original run.

    Recorded explicitly because a replay that differs in several ways at once proves
    nothing: if the outcome changes, there is no way to say which change did it.
    """

    model_config = ConfigDict(frozen=True)

    model: str | None = None
    prompt_note: str | None = None
    tool_policy: str | None = None
    note: str | None = None

    def changed_fields(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("model", "prompt_note", "tool_policy")
            if getattr(self, name) is not None
        )

    @property
    def is_controlled(self) -> bool:
        """Whether at most one variable changes, which is what makes the result readable."""
        return len(self.changed_fields()) <= 1


class ReplayPlan(BaseModel):
    """A reviewable proposal to re-run part of a run."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    parent_run_id: Identifier
    level: ReplayLevel
    from_sequence: int = Field(ge=0)
    checkpoint_id: Identifier | None = None
    checkpoint_sequence: int | None = None
    intervention: Intervention = Intervention()
    steps: tuple[PlannedStep, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime | None = None

    @property
    def replayable(self) -> tuple[PlannedStep, ...]:
        return tuple(
            step for step in self.steps if step.action in {StepAction.REPLAY, StepAction.SANDBOX}
        )

    @property
    def blocked(self) -> tuple[PlannedStep, ...]:
        return tuple(step for step in self.steps if step.action is StepAction.BLOCK)

    @property
    def needs_approval(self) -> tuple[PlannedStep, ...]:
        return tuple(step for step in self.steps if step.action is StepAction.APPROVE)

    @property
    def is_dry_run(self) -> bool:
        """Always true in this release.

        Execution belongs to a runtime adapter. Keeping the flag on the plan means the
        distinction between proposing and doing is visible in the data, not only in the
        documentation.
        """
        return True

    @property
    def requires_human_decision(self) -> bool:
        return bool(self.blocked or self.needs_approval)


def _reason(effect: SideEffect, action: StepAction) -> str:
    return {
        StepAction.REPLAY: "observes without changing anything",
        StepAction.SANDBOX: "writes files; confined to the fork",
        StepAction.APPROVE: "unrecognised tool, so treated as unsafe until confirmed",
        StepAction.BLOCK: (
            "destroys data and is excluded from replay"
            if effect is SideEffect.DESTRUCTIVE
            else "reaches outside this machine and is excluded from replay"
        ),
        StepAction.SKIP: "runs before the replay point",
    }[action]


def build_plan(
    context: AnalysisContext,
    from_sequence: int,
    *,
    level: ReplayLevel = ReplayLevel.R2_SESSION_FORK,
    intervention: Intervention | None = None,
    created_at: datetime | None = None,
) -> ReplayPlan:
    """Propose a replay of ``context`` starting at ``from_sequence``.

    Nothing is executed and nothing is written. The returned plan is the artefact a
    person reviews before any of it is allowed to happen.
    """
    intervention = intervention or Intervention()
    warnings: list[str] = []

    checkpoints = [
        event
        for event in context.events
        if isinstance(event, CheckpointEvent) and event.sequence <= from_sequence
    ]
    anchor = max(checkpoints, key=lambda event: event.sequence, default=None)

    if anchor is None:
        warnings.append(
            "No checkpoint at or before this step, so file state cannot be restored. "
            "The plan can fork the session but the working tree may differ."
        )
    elif anchor.sequence != from_sequence:
        warnings.append(
            f"Nearest checkpoint is step {anchor.sequence}, not step {from_sequence}. "
            "File state and message history would be restored to different points."
        )

    if not intervention.is_controlled:
        warnings.append(
            "More than one variable changes in this intervention. If the outcome "
            "differs, the result will not say which change caused it."
        )

    if not context.integrity.is_intact:
        warnings.append(
            f"The recorded trace is not intact ({context.integrity.describe()}), so the "
            "replay may not reproduce the original conditions."
        )

    steps: list[PlannedStep] = []
    for node in context.graph.in_order():
        if node.kind.value in {"run", "agent"}:
            continue
        if node.sequence < from_sequence:
            steps.append(
                PlannedStep(
                    node_id=node.node_id,
                    sequence=node.sequence,
                    label=node.label,
                    action=StepAction.SKIP,
                    side_effect=SideEffect.READ_ONLY,
                    reason=_reason(SideEffect.READ_ONLY, StepAction.SKIP),
                )
            )
            continue

        event = next(
            (e for e in context.events if e.event_id == node.node_id),
            None,
        )
        name = event.tool.name if isinstance(event, ToolCallEvent) else node.kind.value
        effect = classify(name) if isinstance(event, ToolCallEvent) else SideEffect.READ_ONLY
        action = _ACTION_FOR[effect]
        steps.append(
            PlannedStep(
                node_id=node.node_id,
                sequence=node.sequence,
                label=node.label,
                action=action,
                side_effect=effect,
                reason=_reason(effect, action),
            )
        )

    blocked = [step for step in steps if step.action is StepAction.BLOCK]
    if blocked:
        warnings.append(
            f"{len(blocked)} step(s) reach outside this machine or destroy data. They "
            "are excluded from replay and would have to be performed by hand."
        )

    return ReplayPlan(
        plan_id=f"plan:{context.run_id}:{from_sequence}",
        parent_run_id=context.run_id,
        level=level,
        from_sequence=from_sequence,
        checkpoint_id=anchor.checkpoint.checkpoint_id if anchor else None,
        checkpoint_sequence=anchor.sequence if anchor else None,
        intervention=intervention,
        steps=tuple(steps),
        warnings=tuple(warnings),
        created_at=created_at,
    )


__all__ = [
    "NEVER_AUTOMATIC",
    "Intervention",
    "PlannedStep",
    "ReplayPlan",
    "StepAction",
    "build_plan",
]
