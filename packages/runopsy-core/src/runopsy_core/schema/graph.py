"""The typed trace graph and the diagnosis bundle built on top of it.

The graph is the boundary between capture and analysis: adapters produce events, the
normalizer turns them into nodes and edges, and every detector, ranker and view reads
only from here.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runopsy_core.schema.enums import (
    DEFINITIVE_STATUSES,
    SCHEMA_VERSION,
    AnalysisLayer,
    DiagnosisStatus,
    EdgeKind,
    FailureCategory,
    NodeKind,
    Severity,
)
from runopsy_core.schema.events import Identifier

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class TraceNode(_Frozen):
    """A single point in the execution graph.

    ``attributes`` holds kind-specific detail already validated at the event layer, so
    the graph stays uniform for traversal while keeping what detectors need.
    """

    node_id: Identifier
    kind: NodeKind
    run_id: Identifier
    agent_id: Identifier = "main"
    sequence: int = Field(ge=0)
    timestamp: datetime
    label: str = ""
    attributes: dict[str, object] = Field(default_factory=dict)


class TraceEdge(_Frozen):
    """A typed relationship between two nodes.

    ``confidence`` exists because not every edge is observed. ``PRODUCED`` and
    ``CONSUMED`` are facts recorded by the adapter; ``AFFECTS`` is inferred and carries
    the uncertainty that must survive all the way into what the user is shown.
    """

    source_id: Identifier
    target_id: Identifier
    kind: EdgeKind
    confidence: Confidence = 1.0
    detector: str | None = None


class FailureSignal(_Frozen):
    """Something a detector found wrong, anchored to the node where it was observed."""

    signal_id: Identifier
    node_id: Identifier
    category: FailureCategory
    severity: Severity
    layer: AnalysisLayer
    detector: str
    summary: str
    evidence_node_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _deterministic_layers_need_no_model(self) -> FailureSignal:
        """Guard the token-free promise of the deterministic layers.

        L0-L2 must be reproducible offline. A signal claiming one of those layers while
        naming a model-backed detector would mean the "no tokens for basic analysis"
        guarantee had quietly broken.
        """
        deterministic = {
            AnalysisLayer.L0_STRUCTURAL,
            AnalysisLayer.L1_BEHAVIORAL,
            AnalysisLayer.L2_GRAPH_IMPACT,
        }
        if self.layer in deterministic and self.detector.startswith("semantic:"):
            msg = f"semantic detector {self.detector!r} cannot report a deterministic layer"
            raise ValueError(msg)
        return self


class DiagnosisCandidate(_Frozen):
    """One ranked explanation of why a run failed.

    ``affected_node_ids`` is the propagation chain: what this step is believed to have
    broken downstream. It is reported separately from the evidence so a user can see the
    difference between what was measured and what was inferred.
    """

    candidate_id: Identifier
    onset_node_id: Identifier
    category: FailureCategory
    status: DiagnosisStatus
    confidence: Confidence
    score: float
    summary: str
    signal_ids: tuple[Identifier, ...] = ()
    evidence_node_ids: tuple[Identifier, ...] = ()
    affected_node_ids: tuple[Identifier, ...] = ()
    replay_run_id: Identifier | None = None
    verified_by: str | None = None

    @model_validator(mode="after")
    def _definitive_claims_need_validation(self) -> DiagnosisCandidate:
        """Refuse to construct an unearned definitive claim.

        This is the product's central rule expressed as a type invariant rather than a
        style guideline: a candidate may only claim ``replay_supported`` if a replay run
        backs it, or ``human_verified`` if a person signed off. Without this, a confident
        wrong answer is one careless assignment away, and a diagnosis tool that is
        confidently wrong is worse than no tool at all.
        """
        if self.status is DiagnosisStatus.REPLAY_SUPPORTED and self.replay_run_id is None:
            msg = "status 'replay_supported' requires replay_run_id"
            raise ValueError(msg)
        if self.status is DiagnosisStatus.HUMAN_VERIFIED and not self.verified_by:
            msg = "status 'human_verified' requires verified_by"
            raise ValueError(msg)
        return self

    @property
    def is_definitive(self) -> bool:
        """Whether this candidate may be described in causal rather than hedged language."""
        return self.status in DEFINITIVE_STATUSES


class DiagnosisBundle(_Frozen):
    """What ``runopsy diagnose`` produces: the observed failure plus ranked candidates.

    The observed failure is kept separate from the candidates on purpose. "The test
    failed at step 14" is a fact; "step 9 set the wrong config" is a hypothesis, and
    conflating the two is exactly the reading error that sends people to the wrong fix.
    """

    schema_version: str = SCHEMA_VERSION
    diagnosis_id: Identifier
    run_id: Identifier
    created_at: datetime
    observed_failure_node_id: Identifier | None = None
    observed_failure_summary: str = ""
    candidates: tuple[DiagnosisCandidate, ...] = ()
    tokens_spent: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    @property
    def primary(self) -> DiagnosisCandidate | None:
        """Highest scoring candidate, or ``None`` when nothing could be localized."""
        return max(self.candidates, key=lambda c: c.score, default=None)


class TraceGraph(_Frozen):
    """An immutable normalized run.

    Traversal helpers live here rather than in a graph library so the schema package
    stays dependency-light; heavier centrality and reachability analysis belongs in the
    detector layer.
    """

    schema_version: str = SCHEMA_VERSION
    run_id: Identifier
    nodes: tuple[TraceNode, ...] = ()
    edges: tuple[TraceEdge, ...] = ()

    @model_validator(mode="after")
    def _edges_must_reference_known_nodes(self) -> TraceGraph:
        known = {node.node_id for node in self.nodes}
        dangling = {
            endpoint
            for edge in self.edges
            for endpoint in (edge.source_id, edge.target_id)
            if endpoint not in known
        }
        if dangling:
            msg = f"edges reference unknown nodes: {sorted(dangling)}"
            raise ValueError(msg)
        return self

    def node(self, node_id: str) -> TraceNode | None:
        """Return the node with ``node_id``, or ``None`` when it is absent."""
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def in_order(self) -> tuple[TraceNode, ...]:
        """Nodes sorted by execution sequence."""
        return tuple(sorted(self.nodes, key=lambda n: n.sequence))

    def outgoing(
        self, node_id: str, *, kinds: frozenset[EdgeKind] | None = None
    ) -> tuple[TraceEdge, ...]:
        """Edges leaving ``node_id``, optionally restricted to ``kinds``."""
        return tuple(
            edge
            for edge in self.edges
            if edge.source_id == node_id and (kinds is None or edge.kind in kinds)
        )

    def descendants(
        self, node_id: str, *, kinds: frozenset[EdgeKind] | None = None
    ) -> tuple[str, ...]:
        """Node ids reachable from ``node_id``, breadth-first and cycle-safe.

        Retry storms and repair loops make real traces cyclic, so this cannot assume a
        DAG; the visited set is what keeps a looping run from hanging the analysis.
        """
        seen: set[str] = {node_id}
        order: list[str] = []
        queue: deque[str] = deque([node_id])
        while queue:
            current = queue.popleft()
            for edge in self.outgoing(current, kinds=kinds):
                if edge.target_id not in seen:
                    seen.add(edge.target_id)
                    order.append(edge.target_id)
                    queue.append(edge.target_id)
        return tuple(order)
