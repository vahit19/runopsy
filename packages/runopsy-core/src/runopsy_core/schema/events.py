"""Normalized event stream.

A runtime adapter's only job is to turn its runtime's native callbacks into these
events. Everything downstream — the graph builder, the detectors, the ranker, the UI —
reads this format and nothing else, which is what keeps the engine independent of any
one agent framework.

Each event is a shared envelope plus one payload named after its kind, matching the
wire format in section 7.3 of the design document. Events are immutable and
append-only, and each carries a monotonic ``sequence`` within its run so a truncated,
reordered or tampered trace is detectable rather than quietly producing a confident and
wrong diagnosis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from runopsy_core.hashing import DIGEST_PATTERN
from runopsy_core.schema.enums import (
    SCHEMA_VERSION,
    CallStatus,
    EventKind,
    MemoryOperation,
    RunOutcome,
    SupportStatus,
)

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$"),
]
"""Opaque adapter-assigned identifier. Constrained so ids stay safe in paths and URLs."""

Digest = Annotated[str, StringConstraints(pattern=DIGEST_PATTERN.pattern)]
"""A ``sha256:<hex>`` reference to content that is not itself stored in the trace."""


class _Payload(BaseModel):
    """Base for every model here: immutable, and tolerant of fields from newer producers.

    Unknown fields are kept rather than rejected so a trace written by a newer adapter
    stays readable by an older engine. Forward compatibility matters more than
    strictness here, because refusing to load a trace means refusing to diagnose a
    failure the user is already stuck on.
    """

    model_config = ConfigDict(extra="allow", frozen=True)


class StateChange(_Payload):
    """A single observed key transition, used to detect state and invariant failures."""

    before: object | None = None
    after: object | None = None


class SecurityMetadata(_Payload):
    """Redaction bookkeeping carried per event.

    ``contains_secret`` records that the scanner matched something; it is what the
    payload minimizer consults before any content may leave the machine.
    """

    redacted: bool = False
    contains_secret: bool = False


class TokenUsage(_Payload):
    """Token accounting for a model call, used by the budget and loop detectors."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class RunPayload(_Payload):
    """Identity of the run and the runtime that produced it.

    A replay run carries its lineage explicitly: the run it forked from, and exactly
    what was changed. The intervention is recorded structurally rather than as prose
    because a replay that cannot say what it varied proves nothing — the comparison
    downstream keys off these fields.
    """

    task: str = ""
    repo: str | None = None
    runtime: str = "unknown"
    provider: str | None = None
    model: str | None = None
    outcome: RunOutcome = RunOutcome.UNKNOWN
    summary: str | None = None
    parent_run_id: Identifier | None = None
    intervention_kind: Literal["skip", "substitute"] | None = None
    intervention_target: int | None = Field(default=None, ge=0)
    """Sequence number in the parent run that the intervention was applied to."""


class AgentPayload(_Payload):
    """Identity of a main or sub-agent."""

    role: str = ""
    model: str | None = None
    parent_agent_id: Identifier | None = None
    outcome: RunOutcome = RunOutcome.UNKNOWN


class TurnPayload(_Payload):
    """One user-agent interaction unit."""

    order: int = Field(default=0, ge=0)
    input_hash: Digest | None = None
    output_hash: Digest | None = None


class LlmPayload(_Payload):
    """A model call, recorded by hash so prompts never enter the trace."""

    model: str
    provider: str | None = None
    prompt_hash: Digest | None = None
    response_hash: Digest | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = Field(default=0, ge=0)
    status: CallStatus = CallStatus.OK
    finish_reason: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)


class ToolPayload(_Payload):
    """An external action and its result.

    ``retry_of`` is what lets the loop and retry-storm detectors work without reading
    arguments: repetition is visible structurally.
    """

    name: str
    arguments_hash: Digest | None = None
    output_hash: Digest | None = None
    exit_code: int | None = None
    duration_ms: int = Field(default=0, ge=0)
    status: CallStatus = CallStatus.OK
    retry_of: Identifier | None = None
    error_type: str | None = None
    blocked_reason: str | None = None


class StatePayload(_Payload):
    """The observed values of execution state at a point in the run."""

    values: dict[str, object] = Field(default_factory=dict)


class MemoryPayload(_Payload):
    """A memory read or write.

    ``age_seconds`` exists so stale-memory failures are detectable without judging the
    content: recalling something written long ago is a structural signal.
    """

    operation: MemoryOperation
    key: str
    source: str | None = None
    value_hash: Digest | None = None
    age_seconds: float | None = Field(default=None, ge=0)


class ClaimPayload(_Payload):
    """An assertion the agent made, tracked so it can be checked against evidence."""

    claim_id: Identifier
    text_hash: Digest
    support_status: SupportStatus = SupportStatus.UNCHECKED


class EvidencePayload(_Payload):
    """Data offered in support of a claim."""

    source: str
    excerpt_hash: Digest
    supports_claim_id: Identifier | None = None
    quality: float | None = Field(default=None, ge=0, le=1)


class ArtifactPayload(_Payload):
    """A file, patch, table or report the run produced."""

    path: str
    artifact_type: str = "file"
    content_hash: Digest
    size_bytes: int | None = Field(default=None, ge=0)


class CheckpointPayload(_Payload):
    """A point the run can be returned to, and the anchor for R1/R2 replay."""

    checkpoint_id: Identifier
    repo_state: str | None = None
    turn_order: int | None = Field(default=None, ge=0)


class HandoffPayload(_Payload):
    """Context passed from one agent to another.

    ``missing_fields`` is recorded at capture time because incomplete handoff is one of
    the hardest multi-agent failures to reconstruct afterwards.
    """

    from_agent_id: Identifier
    to_agent_id: Identifier
    context_hash: Digest | None = None
    missing_fields: tuple[str, ...] = ()


class BaseEvent(_Payload):
    """Envelope shared by every event kind."""

    schema_version: str = SCHEMA_VERSION
    event_id: Identifier
    run_id: Identifier
    agent_id: Identifier = "main"
    parent_id: Identifier | None = None
    sequence: int = Field(ge=0)
    timestamp: datetime
    state_delta: dict[str, StateChange] = Field(default_factory=dict)
    security: SecurityMetadata = Field(default_factory=SecurityMetadata)

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Reject naive timestamps.

        Runs are compared and merged across machines and time zones; a naive timestamp
        silently misorders a trace, and misordering is indistinguishable from a real
        causal signal once the graph is built.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "timestamp must be timezone-aware"
            raise ValueError(msg)
        return value


class RunStartEvent(BaseEvent):
    kind: Literal[EventKind.RUN_START] = EventKind.RUN_START
    run: RunPayload


class RunEndEvent(BaseEvent):
    kind: Literal[EventKind.RUN_END] = EventKind.RUN_END
    run: RunPayload


class AgentStartEvent(BaseEvent):
    kind: Literal[EventKind.AGENT_START] = EventKind.AGENT_START
    agent: AgentPayload


class AgentEndEvent(BaseEvent):
    kind: Literal[EventKind.AGENT_END] = EventKind.AGENT_END
    agent: AgentPayload


class TurnStartEvent(BaseEvent):
    kind: Literal[EventKind.TURN_START] = EventKind.TURN_START
    turn: TurnPayload


class TurnEndEvent(BaseEvent):
    kind: Literal[EventKind.TURN_END] = EventKind.TURN_END
    turn: TurnPayload


class LlmCallEvent(BaseEvent):
    kind: Literal[EventKind.LLM_CALL] = EventKind.LLM_CALL
    llm: LlmPayload


class ToolCallEvent(BaseEvent):
    kind: Literal[EventKind.TOOL_CALL] = EventKind.TOOL_CALL
    tool: ToolPayload


class StateSnapshotEvent(BaseEvent):
    kind: Literal[EventKind.STATE_SNAPSHOT] = EventKind.STATE_SNAPSHOT
    state: StatePayload


class MemoryOpEvent(BaseEvent):
    kind: Literal[EventKind.MEMORY_OP] = EventKind.MEMORY_OP
    memory: MemoryPayload


class ClaimEvent(BaseEvent):
    kind: Literal[EventKind.CLAIM] = EventKind.CLAIM
    claim: ClaimPayload


class EvidenceEvent(BaseEvent):
    kind: Literal[EventKind.EVIDENCE] = EventKind.EVIDENCE
    evidence: EvidencePayload


class ArtifactEvent(BaseEvent):
    kind: Literal[EventKind.ARTIFACT] = EventKind.ARTIFACT
    artifact: ArtifactPayload


class CheckpointEvent(BaseEvent):
    kind: Literal[EventKind.CHECKPOINT] = EventKind.CHECKPOINT
    checkpoint: CheckpointPayload


class HandoffEvent(BaseEvent):
    kind: Literal[EventKind.HANDOFF] = EventKind.HANDOFF
    handoff: HandoffPayload


Event = Annotated[
    RunStartEvent
    | RunEndEvent
    | AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | LlmCallEvent
    | ToolCallEvent
    | StateSnapshotEvent
    | MemoryOpEvent
    | ClaimEvent
    | EvidenceEvent
    | ArtifactEvent
    | CheckpointEvent
    | HandoffEvent,
    Field(discriminator="kind"),
]
"""Any normalized event, dispatched on ``kind``."""
