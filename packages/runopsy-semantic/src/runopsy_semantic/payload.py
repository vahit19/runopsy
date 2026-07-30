"""Building the smallest packet that can answer the question.

Section 12.2 of the design document: send the suspected step, the state around it, and
the relevant evidence — never the whole trace. That is a privacy control first and a cost
control second, and the ordering matters. A trace holds command lines, file paths and
outputs from someone's private repository; every field that leaves the machine has to
earn its place.

Minimization is structural rather than a habit. ``build_packet`` accepts a window of
events, not an ``AnalysisContext``, so no caller can accidentally hand it the entire
run. Everything it produces passes through the secret scanner, and payload text comes
from the local vault only when the user has one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from runopsy_adapter.secrets import scan
from runopsy_core.schema import (
    ClaimEvent,
    Event,
    HandoffEvent,
    LlmCallEvent,
    ToolCallEvent,
)

NEIGHBOURS_BEFORE = 2
NEIGHBOURS_AFTER = 2
MAX_TEXT_CHARS = 1_200


class PayloadLookup(Protocol):
    """Digest in, payload text out. Optional: without one, only metadata is sent."""

    def get(self, digest: str) -> Any: ...


@dataclass(frozen=True)
class EvidencePacket:
    """What will be sent, and what was withheld.

    ``withheld`` is not bookkeeping — it is shown to the user, because a semantic
    verdict reached without the command text is a weaker verdict and they deserve to
    know which.
    """

    run_id: str
    focus_sequence: int
    steps: tuple[dict[str, Any], ...]
    withheld: tuple[str, ...] = ()
    redacted: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {"run_id": self.run_id, "focus_step": self.focus_sequence, "steps": list(self.steps)},
            ensure_ascii=False,
            sort_keys=True,
            indent=None,
        )


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + f"… [{len(text) - MAX_TEXT_CHARS} more characters]"


def _resolve(
    digest: str | None, vault: PayloadLookup | None, withheld: list[str], label: str
) -> str | None:
    """Fetch payload text if it is available and safe to send."""
    if digest is None:
        return None
    if vault is None:
        withheld.append(f"{label} (no local payload store)")
        return None
    entry = vault.get(digest)
    if entry is None:
        withheld.append(f"{label} (not in the local store)")
        return None
    if not getattr(entry, "executable", True):
        withheld.append(f"{label} (contained a credential)")
        return None
    return _clip(str(getattr(entry, "text", "")))


def _describe(event: Event, vault: PayloadLookup | None, withheld: list[str]) -> dict[str, Any]:
    """One step, reduced to what a semantic judgement actually needs."""
    entry: dict[str, Any] = {
        "step": event.sequence,
        "kind": event.kind.value,
    }
    if event.state_delta:
        entry["state_changes"] = {
            key: {"before": change.before, "after": change.after}
            for key, change in sorted(event.state_delta.items())
        }

    match event:
        case ToolCallEvent():
            entry["tool"] = event.tool.name
            entry["status"] = event.tool.status.value
            if event.tool.exit_code is not None:
                entry["exit_code"] = event.tool.exit_code
            if event.tool.error_type:
                entry["error_type"] = event.tool.error_type
            command = _resolve(
                event.tool.arguments_hash, vault, withheld, f"step {event.sequence} command"
            )
            if command:
                entry["command"] = command
            output = _resolve(
                event.tool.output_hash, vault, withheld, f"step {event.sequence} output"
            )
            if output:
                entry["output"] = output
        case LlmCallEvent():
            entry["model"] = event.llm.model
            entry["status"] = event.llm.status.value
            if event.llm.finish_reason:
                entry["finish_reason"] = event.llm.finish_reason
        case ClaimEvent():
            entry["claim_id"] = event.claim.claim_id
            entry["support_status"] = event.claim.support_status.value
        case HandoffEvent():
            entry["handoff_to"] = event.handoff.to_agent_id
            entry["missing_fields"] = list(event.handoff.missing_fields)
        case _:
            pass
    return entry


def build_packet(
    run_id: str,
    window: tuple[Event, ...],
    focus_sequence: int,
    *,
    vault: PayloadLookup | None = None,
) -> EvidencePacket:
    """Assemble the packet for one suspicious step.

    ``window`` is the already-narrowed slice of events. Passing a window rather than the
    whole run is what makes minimization impossible to forget.
    """
    withheld: list[str] = []
    steps = tuple(_describe(event, vault, withheld) for event in window)

    rendered = json.dumps(steps, ensure_ascii=False, sort_keys=True)
    result = scan(rendered)
    if result.found:
        # A credential reaching this point means the capture-time scan missed a shape.
        # The packet is rebuilt from the redacted rendering rather than sent, because
        # this is the last checkpoint before data leaves the machine.
        steps = tuple(json.loads(result.redacted))
        withheld.append("values matching a credential pattern")

    return EvidencePacket(
        run_id=run_id,
        focus_sequence=focus_sequence,
        steps=steps,
        withheld=tuple(withheld),
        redacted=result.found,
    )


def window_around(events: tuple[Event, ...], focus_sequence: int) -> tuple[Event, ...]:
    """The neighbourhood of a step: a couple before, a couple after.

    Bounded deliberately. A wider window would improve the model's context and worsen
    everything else — cost, latency, and how much of a private repository is exposed to
    answer one question.
    """
    ordered = sorted(events, key=lambda event: event.sequence)
    index = next(
        (position for position, event in enumerate(ordered) if event.sequence == focus_sequence),
        None,
    )
    if index is None:
        return ()
    start = max(0, index - NEIGHBOURS_BEFORE)
    return tuple(ordered[start : index + NEIGHBOURS_AFTER + 1])
