"""L2 graph impact: what a step may have broken downstream.

This is the first module allowed to produce ``AFFECTS`` edges, and every one of them is
an inference rather than an observation. Two rules keep that inference honest:

- Nothing may affect the past. An edge is only proposed toward steps that ran later, so
  a retry loop cannot make a step appear to have caused its own predecessor.
- Confidence decays with distance. A step two hops away is a weaker claim than the one
  immediately downstream, and the number that says so travels with the edge into the
  diagnosis and the UI rather than being flattened away.

Reachability alone is not causation. What this produces is the *candidate* blast radius,
which narrows the search; only a counterfactual replay can promote it to a cause.
"""

from __future__ import annotations

from collections import deque
from typing import Final

from runopsy_core.schema import EdgeKind, TraceEdge, TraceGraph

DETECTOR: Final = "impact:reachability"

PROPAGATING_EDGES: Final = frozenset(
    {
        EdgeKind.PRECEDES,
        EdgeKind.DEPENDS_ON,
        EdgeKind.PRODUCED,
        EdgeKind.CONSUMED,
        EdgeKind.DERIVED_FROM,
    }
)
"""Edge kinds along which an error can plausibly travel."""

DECAY: Final = 0.75
"""Confidence retained per hop. Chosen so a fourth-hop claim lands near 0.3."""

MAX_HOPS: Final = 6
"""Distance past which a propagation claim is too weak to be worth showing."""

MINIMUM_CONFIDENCE: Final = 0.05


def reachable_from(
    graph: TraceGraph, onset_node_id: str, *, max_hops: int = MAX_HOPS
) -> dict[str, int]:
    """Map each downstream node to its hop distance from ``onset_node_id``.

    Breadth-first, so the recorded distance is the shortest path — a node reachable both
    directly and through a long detour is judged by the strong link, not the weak one.
    """
    onset = graph.node(onset_node_id)
    if onset is None:
        return {}

    distances: dict[str, int] = {}
    seen = {onset_node_id}
    queue: deque[tuple[str, int]] = deque([(onset_node_id, 0)])

    while queue:
        current, distance = queue.popleft()
        if distance >= max_hops:
            continue
        for edge in graph.outgoing(current, kinds=PROPAGATING_EDGES):
            target = graph.node(edge.target_id)
            if edge.target_id in seen or target is None:
                continue
            if target.sequence <= onset.sequence:
                continue
            seen.add(edge.target_id)
            distances[edge.target_id] = distance + 1
            queue.append((edge.target_id, distance + 1))

    return distances


def confidence_at(distance: int) -> float:
    """Confidence in a propagation claim that many hops downstream."""
    return round(DECAY**distance, 3)


def affected_nodes(
    graph: TraceGraph, onset_node_id: str, *, max_hops: int = MAX_HOPS
) -> tuple[str, ...]:
    """Downstream nodes in execution order, nearest and strongest first."""
    distances = reachable_from(graph, onset_node_id, max_hops=max_hops)
    positions = {node.node_id: node.sequence for node in graph.nodes}
    return tuple(
        sorted(distances, key=lambda node_id: (distances[node_id], positions.get(node_id, 0)))
    )


def infer_affects(
    graph: TraceGraph, onset_node_id: str, *, max_hops: int = MAX_HOPS
) -> tuple[TraceEdge, ...]:
    """Propose ``AFFECTS`` edges from an onset candidate to what it may have broken.

    Returned separately from the graph rather than merged into it, so a caller always
    knows which edges were recorded and which were proposed here.
    """
    distances = reachable_from(graph, onset_node_id, max_hops=max_hops)
    edges = [
        TraceEdge(
            source_id=onset_node_id,
            target_id=node_id,
            kind=EdgeKind.AFFECTS,
            confidence=confidence_at(distance),
            detector=DETECTOR,
        )
        for node_id, distance in distances.items()
        if confidence_at(distance) >= MINIMUM_CONFIDENCE
    ]
    return tuple(sorted(edges, key=lambda edge: (-edge.confidence, edge.target_id)))
