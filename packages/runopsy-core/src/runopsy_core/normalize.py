"""Turn an event stream into a typed trace graph.

Only relationships the adapter actually reported become edges here. Inferred
``AFFECTS`` edges — the propagation chain — are deliberately *not* produced by this
module; they belong to the L2 impact layer. Keeping observation and inference in
separate passes is what makes it possible to show a user which parts of a causal story
were measured and which were guessed.
"""

from __future__ import annotations

from collections.abc import Iterable

from runopsy_core.schema import (
    AgentEndEvent,
    AgentStartEvent,
    ArtifactEvent,
    CheckpointEvent,
    ClaimEvent,
    EdgeKind,
    Event,
    EvidenceEvent,
    HandoffEvent,
    LlmCallEvent,
    MemoryOperation,
    MemoryOpEvent,
    NodeKind,
    RunEndEvent,
    RunStartEvent,
    StateSnapshotEvent,
    ToolCallEvent,
    TraceEdge,
    TraceGraph,
    TraceNode,
    TurnEndEvent,
    TurnStartEvent,
)

_LIFECYCLE_END = (RunEndEvent, AgentEndEvent, TurnEndEvent)

_STEP_KINDS: dict[str, NodeKind] = {
    "llm_call": NodeKind.LLM_CALL,
    "tool_call": NodeKind.TOOL_CALL,
    "state_snapshot": NodeKind.STATE_SNAPSHOT,
    "memory_op": NodeKind.MEMORY_OP,
    "claim": NodeKind.CLAIM,
    "evidence": NodeKind.EVIDENCE,
    "artifact": NodeKind.ARTIFACT,
    "checkpoint": NodeKind.CHECKPOINT,
}


def node_id_for(event: Event) -> str:
    """The graph node an event belongs to.

    Lifecycle pairs collapse onto one node so a run, agent or turn appears once rather
    than as a start and an end that a reader has to mentally join.
    """
    if isinstance(event, RunStartEvent | RunEndEvent):
        return event.run_id
    if isinstance(event, AgentStartEvent | AgentEndEvent):
        return event.agent_id
    return event.event_id


def _label(event: Event) -> str:
    match event:
        case RunStartEvent() | RunEndEvent():
            return event.run.task or event.run_id
        case AgentStartEvent() | AgentEndEvent():
            return event.agent.role or event.agent_id
        case LlmCallEvent():
            return event.llm.model
        case ToolCallEvent():
            return event.tool.name
        case MemoryOpEvent():
            return f"{event.memory.operation.value} {event.memory.key}"
        case ArtifactEvent():
            return event.artifact.path
        case EvidenceEvent():
            return event.evidence.source
        case CheckpointEvent():
            return event.checkpoint.checkpoint_id
        case HandoffEvent():
            return f"{event.handoff.from_agent_id} -> {event.handoff.to_agent_id}"
        case _:
            return event.kind.value


def _attributes(event: Event, *, only_explicit: bool = False) -> dict[str, object]:
    """Kind-specific payload, kept on the node so views need not re-read the stream.

    ``only_explicit`` drops fields left at their default. It is used when merging an end
    event onto a node an earlier start event created, so that a ``run_end`` carrying an
    empty task cannot blank the task the ``run_start`` recorded.
    """
    payload = event.model_dump(mode="json", exclude_defaults=only_explicit)
    for field in (
        "run",
        "agent",
        "turn",
        "llm",
        "tool",
        "state",
        "memory",
        "claim",
        "evidence",
        "artifact",
        "checkpoint",
        "handoff",
    ):
        if field in payload:
            attributes: dict[str, object] = dict(payload[field])
            if event.state_delta:
                attributes["state_delta"] = payload["state_delta"]
            # The redaction flags must travel with the node. A consumer that only sees
            # the graph — an export, a UI — otherwise has no way to know a step was
            # flagged, and would publish it as though the scanner had never run.
            if event.security.contains_secret:
                attributes["contains_secret"] = True
            if event.security.redacted:
                attributes["redacted"] = True
            return attributes
    return {}


def build_graph(run_id: str, events: Iterable[Event]) -> TraceGraph:
    """Normalize an ordered event stream into a graph for ``run_id``.

    Events from other runs are ignored rather than merged, because attributing one run's
    step to another run's failure is worse than having no answer at all.
    """
    ordered = sorted(
        (event for event in events if event.run_id == run_id),
        key=lambda event: (event.sequence, event.event_id),
    )

    nodes: dict[str, TraceNode] = {}
    edges: list[TraceEdge] = []
    previous_step: dict[str, str] = {}
    claim_nodes: dict[str, str] = {}

    for event in ordered:
        identifier = node_id_for(event)
        if isinstance(event, _LIFECYCLE_END) and identifier in nodes:
            existing = nodes[identifier]
            merged = dict(existing.attributes)
            merged.update(_attributes(event, only_explicit=True))
            nodes[identifier] = existing.model_copy(update={"attributes": merged})
            continue

        nodes[identifier] = TraceNode(
            node_id=identifier,
            kind=_node_kind(event),
            run_id=run_id,
            agent_id=event.agent_id,
            sequence=event.sequence,
            timestamp=event.timestamp,
            label=_label(event),
            attributes=_attributes(event),
        )

        if isinstance(event, ClaimEvent):
            claim_nodes[event.claim.claim_id] = identifier

        if event.kind.value in _STEP_KINDS:
            prior = previous_step.get(event.agent_id)
            if prior is not None:
                edges.append(
                    TraceEdge(source_id=prior, target_id=identifier, kind=EdgeKind.PRECEDES)
                )
            previous_step[event.agent_id] = identifier

        edges.extend(_reported_edges(event, identifier, claim_nodes, nodes))

    return TraceGraph(run_id=run_id, nodes=tuple(nodes.values()), edges=tuple(edges))


def _node_kind(event: Event) -> NodeKind:
    match event:
        case RunStartEvent() | RunEndEvent():
            return NodeKind.RUN
        case AgentStartEvent() | AgentEndEvent():
            return NodeKind.AGENT
        case TurnStartEvent() | TurnEndEvent():
            return NodeKind.TURN
        case HandoffEvent():
            return NodeKind.HANDOFF
        case _:
            return _STEP_KINDS[event.kind.value]


def _reported_edges(
    event: Event,
    identifier: str,
    claim_nodes: dict[str, str],
    nodes: dict[str, TraceNode],
) -> list[TraceEdge]:
    """Edges the adapter stated as fact, never edges we inferred."""
    edges: list[TraceEdge] = []

    if isinstance(event, ToolCallEvent) and event.tool.retry_of in nodes:
        edges.append(
            TraceEdge(
                source_id=identifier,
                target_id=event.tool.retry_of,
                kind=EdgeKind.DEPENDS_ON,
                detector="normalize:retry",
            )
        )

    if isinstance(event, ArtifactEvent) and event.parent_id in nodes:
        edges.append(
            TraceEdge(source_id=event.parent_id, target_id=identifier, kind=EdgeKind.PRODUCED)
        )

    if (
        isinstance(event, MemoryOpEvent)
        and event.memory.operation is MemoryOperation.READ
        and event.parent_id in nodes
    ):
        edges.append(
            TraceEdge(source_id=event.parent_id, target_id=identifier, kind=EdgeKind.CONSUMED)
        )

    if isinstance(event, ClaimEvent) and event.parent_id in nodes:
        edges.append(
            TraceEdge(source_id=identifier, target_id=event.parent_id, kind=EdgeKind.DERIVED_FROM)
        )

    if isinstance(event, EvidenceEvent):
        target = claim_nodes.get(event.evidence.supports_claim_id or "")
        if target is not None:
            edges.append(TraceEdge(source_id=identifier, target_id=target, kind=EdgeKind.VALIDATES))

    if isinstance(event, StateSnapshotEvent) and event.parent_id in nodes:
        edges.append(
            TraceEdge(source_id=event.parent_id, target_id=identifier, kind=EdgeKind.PRODUCED)
        )

    return edges
