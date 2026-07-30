"""Controlled vocabularies for the Runopsy trace schema.

Every value here is part of the on-disk format and the public API. Renaming a member
changes stored traces, so additions are cheap but changes require a schema version bump.
"""

from enum import StrEnum
from typing import Final

SCHEMA_VERSION: Final = "0.1"
"""Version of the normalized event and graph format.

Bumped whenever a field is removed or its meaning changes. Adding an optional field
does not require a bump; readers must tolerate unknown fields from newer producers.
"""


class EventKind(StrEnum):
    """Kind of normalized event emitted by a runtime adapter."""

    RUN_START = "run_start"
    RUN_END = "run_end"
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    STATE_SNAPSHOT = "state_snapshot"
    MEMORY_OP = "memory_op"
    CLAIM = "claim"
    EVIDENCE = "evidence"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"
    HANDOFF = "handoff"


class NodeKind(StrEnum):
    """Node types in the trace graph (design document section 7.1)."""

    RUN = "run"
    AGENT = "agent"
    TURN = "turn"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    STATE_SNAPSHOT = "state_snapshot"
    MEMORY_OP = "memory_op"
    CLAIM = "claim"
    EVIDENCE = "evidence"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"
    HANDOFF = "handoff"
    FAILURE_SIGNAL = "failure_signal"
    DIAGNOSIS = "diagnosis"
    REPLAY_RUN = "replay_run"


class EdgeKind(StrEnum):
    """Edge types in the trace graph (design document section 7.2).

    The distinction between ``DEPENDS_ON`` and ``AFFECTS`` carries the product's core
    epistemic claim: the former is an observed data or execution dependency, the latter
    is only a *possible* downstream effect and must never be presented as proven.
    """

    PRECEDES = "precedes"
    DEPENDS_ON = "depends_on"
    PRODUCED = "produced"
    CONSUMED = "consumed"
    DERIVED_FROM = "derived_from"
    CONTRADICTS = "contradicts"
    VALIDATES = "validates"
    AFFECTS = "affects"
    FORKED_FROM = "forked_from"


class FailureCategory(StrEnum):
    """Failure taxonomy (design document section 9)."""

    GOAL_INPUT = "goal_input"
    PLANNING = "planning"
    RETRIEVAL = "retrieval"
    TOOL_SELECTION = "tool_selection"
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_EXECUTION = "tool_execution"
    STATE = "state"
    MEMORY = "memory"
    HANDOFF = "handoff"
    REASONING = "reasoning"
    VALIDATION = "validation"
    CONTROL_FLOW = "control_flow"
    BUDGET = "budget"
    SAFETY = "safety"
    OUTCOME = "outcome"
    UNDETERMINED = "undetermined"
    """Causation established, mechanism not classified.

    Used when a counterfactual replay demonstrates that a step caused the failure but
    nothing in the trace says *how* — the silent-wrong-value case the deterministic
    layers cannot see. Naming a mechanism there would be a guess dressed as taxonomy.
    """


class Severity(StrEnum):
    """How strongly a detector believes something is wrong."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AnalysisLayer(StrEnum):
    """Which analysis layer produced a signal (design document section 8.1).

    ``L0``-``L2`` are deterministic and must never spend tokens. ``L3`` is the only
    layer permitted to call a model, and only for spans already flagged as suspicious.
    """

    L0_STRUCTURAL = "l0_structural"
    L1_BEHAVIORAL = "l1_behavioral"
    L2_GRAPH_IMPACT = "l2_graph_impact"
    L3_SEMANTIC = "l3_semantic"
    L4_VALIDATION = "l4_validation"


class DiagnosisStatus(StrEnum):
    """Epistemic status of a diagnosis (design document section 8.3).

    These are deliberately distinct and must stay distinct in every output surface.
    Collapsing ``SUSPECTED_ONSET`` or ``CORRELATED_CAUSE`` into a confident root-cause
    claim is the single failure mode the product exists to avoid: temporal ordering plus
    correlation is not causal proof. Only ``REPLAY_SUPPORTED`` and ``HUMAN_VERIFIED``
    license definitive language.
    """

    OBSERVED_FAILURE = "observed_failure"
    SUSPECTED_ONSET = "suspected_onset"
    CORRELATED_CAUSE = "correlated_cause"
    REPLAY_SUPPORTED = "replay_supported"
    HUMAN_VERIFIED = "human_verified"
    UNKNOWN = "unknown"


DEFINITIVE_STATUSES: Final = frozenset(
    {DiagnosisStatus.REPLAY_SUPPORTED, DiagnosisStatus.HUMAN_VERIFIED}
)
"""Statuses that permit stating a cause as established rather than suspected."""


class ReplayLevel(StrEnum):
    """Intervention strength of a replay (design document section 10.1).

    ``R0``-``R2`` are in MVP scope. ``R5`` is a research direction and must stay behind
    explicit opt-in because it acts without per-step human approval.
    """

    R0_EXPLAIN_ONLY = "r0_explain_only"
    R1_TURN_ROLLBACK = "r1_turn_rollback"
    R2_SESSION_FORK = "r2_session_fork"
    R3_GUIDED_REPLAY = "r3_guided_replay"
    R4_STEP_REPLAY = "r4_step_replay"
    R5_AUTOMATED_RECOVERY = "r5_automated_recovery"


class RunOutcome(StrEnum):
    """How a run finished, as reported by the runtime rather than inferred."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class CallStatus(StrEnum):
    """Completion status of a single model or tool call."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


class MemoryOperation(StrEnum):
    """Direction of a memory access."""

    READ = "read"
    WRITE = "write"


class SupportStatus(StrEnum):
    """Whether a claim made by the agent is backed by evidence."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    UNCHECKED = "unchecked"
