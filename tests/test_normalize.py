"""Normalizer tests.

The load-bearing property is the last class in this file: normalization records only
what the adapter reported, and never invents a causal edge. Inference belongs to the
impact layer, where it can be labelled as inference.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import START, run_end, run_start, tool_call
from runopsy_core import build_graph
from runopsy_core.schema import (
    ArtifactEvent,
    ArtifactPayload,
    ClaimEvent,
    ClaimPayload,
    EdgeKind,
    EvidenceEvent,
    EvidencePayload,
    NodeKind,
    ToolCallEvent,
    ToolPayload,
    TraceGraph,
)

DIGEST = "sha256:" + "a" * 64
RUN = "run_0042"


def tool(sequence: int, agent_id: str = "main", **payload: object) -> ToolCallEvent:
    defaults: dict[str, object] = {"name": "terminal"}
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        agent_id=agent_id,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(**{**defaults, **payload}),
    )


def kinds_between(graph: TraceGraph, source: str, target: str) -> set[EdgeKind]:
    return {
        edge.kind for edge in graph.edges if edge.source_id == source and edge.target_id == target
    }


class TestNodes:
    def test_a_run_appears_once_rather_than_as_a_start_and_an_end(self) -> None:
        graph = build_graph(RUN, [run_start(RUN), tool(1), run_end(2, RUN)])

        runs = [node for node in graph.nodes if node.kind is NodeKind.RUN]

        assert len(runs) == 1
        assert runs[0].node_id == RUN

    def test_the_end_payload_is_merged_onto_the_run_node(self) -> None:
        graph = build_graph(RUN, [run_start(RUN), run_end(2, RUN)])

        node = graph.node(RUN)

        assert node is not None
        assert node.attributes["task"] == "fix the failing test"
        assert node.attributes["outcome"] == "failure"

    def test_steps_become_nodes_keyed_by_event_id(self) -> None:
        graph = build_graph(RUN, [tool(1), tool(2)])

        assert {node.node_id for node in graph.nodes} == {"evt_1", "evt_2"}

    def test_events_from_another_run_are_ignored_not_merged(self) -> None:
        """Blaming one run's step for another run's failure is worse than no answer."""
        foreign = tool_call(1, "run_9999")

        graph = build_graph(RUN, [tool(1), foreign])

        assert {node.node_id for node in graph.nodes} == {"evt_1"}


class TestReportedEdges:
    def test_consecutive_steps_are_chained_in_order(self) -> None:
        graph = build_graph(RUN, [tool(3), tool(1), tool(2)])

        chain = [
            (edge.source_id, edge.target_id)
            for edge in graph.edges
            if edge.kind is EdgeKind.PRECEDES
        ]

        assert chain == [("evt_1", "evt_2"), ("evt_2", "evt_3")]

    def test_each_agent_has_its_own_chain(self) -> None:
        """A sub-agent's steps do not precede the parent's; they run beside them."""
        graph = build_graph(RUN, [tool(1), tool(2, agent_id="tester"), tool(3)])

        chain = [
            (edge.source_id, edge.target_id)
            for edge in graph.edges
            if edge.kind is EdgeKind.PRECEDES
        ]

        assert chain == [("evt_1", "evt_3")]

    def test_a_retry_depends_on_the_call_it_repeats(self) -> None:
        graph = build_graph(RUN, [tool(1), tool(2, retry_of="evt_1")])

        assert EdgeKind.DEPENDS_ON in kinds_between(graph, "evt_2", "evt_1")

    def test_an_artifact_is_produced_by_the_step_that_wrote_it(self) -> None:
        artifact = ArtifactEvent(
            event_id="evt_2",
            run_id=RUN,
            parent_id="evt_1",
            sequence=2,
            timestamp=START,
            artifact=ArtifactPayload(path="src/config.py", content_hash=DIGEST),
        )

        graph = build_graph(RUN, [tool(1), artifact])

        assert EdgeKind.PRODUCED in kinds_between(graph, "evt_1", "evt_2")

    def test_evidence_validates_the_claim_it_names(self) -> None:
        claim = ClaimEvent(
            event_id="evt_1",
            run_id=RUN,
            sequence=1,
            timestamp=START,
            claim=ClaimPayload(claim_id="c1", text_hash=DIGEST),
        )
        evidence = EvidenceEvent(
            event_id="evt_2",
            run_id=RUN,
            sequence=2,
            timestamp=START,
            evidence=EvidencePayload(source="pytest", excerpt_hash=DIGEST, supports_claim_id="c1"),
        )

        graph = build_graph(RUN, [claim, evidence])

        assert EdgeKind.VALIDATES in kinds_between(graph, "evt_2", "evt_1")

    def test_a_retry_pointing_at_an_unknown_call_is_dropped_not_dangling(self) -> None:
        """A partial trace must still build a valid graph rather than fail to load."""
        graph = build_graph(RUN, [tool(2, retry_of="evt_missing")])

        assert graph.edges == ()


class TestInferenceBoundary:
    def test_normalization_never_invents_an_affects_edge(self) -> None:
        """AFFECTS is inference and belongs to L2, where it can be labelled as such.

        If normalization emitted it, a guess would enter the graph indistinguishable
        from an observation, and the whole evidence chain would lose its meaning.
        """
        events = [
            run_start(RUN),
            tool(1, exit_code=1),
            tool(2, retry_of="evt_1"),
            run_end(3, RUN),
        ]

        graph = build_graph(RUN, events)

        assert all(edge.kind is not EdgeKind.AFFECTS for edge in graph.edges)

    def test_every_reported_edge_is_fully_confident(self) -> None:
        graph = build_graph(RUN, [tool(1), tool(2, retry_of="evt_1")])

        assert all(edge.confidence == 1.0 for edge in graph.edges)
