/**
 * The causal failure map: the run as a DAG, with propagation drawn as what it is.
 *
 * Observed edges are solid; inferred ones are dashed, labelled "may reach", and carry
 * their confidence. The design document calls the distinction between DEPENDS_ON and
 * AFFECTS the product's core epistemic claim, so it survives into the rendering rather
 * than being flattened into arrows that all look alike.
 */

import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";

import type { Candidate, GraphResponse } from "./api";
import { useSelection } from "./store";

const COLUMN = 260;
const ROW = 74;

type Props = {
  graph: GraphResponse;
  onset: Candidate | null;
  observedFailureNodeId: string | null;
};

function roleOf(
  nodeId: string,
  onset: Candidate | null,
  observedFailureNodeId: string | null,
): "onset" | "failure" | "affected" | "plain" {
  if (nodeId === onset?.onset_node_id) return "onset";
  if (nodeId === observedFailureNodeId) return "failure";
  if (onset?.affected_node_ids.includes(nodeId)) return "affected";
  return "plain";
}

const STYLES: Record<string, React.CSSProperties> = {
  onset: { background: "#fff6d8", border: "2px solid #c8951a", fontWeight: 600 },
  failure: { background: "#ffe1e1", border: "2px solid #c0392b", fontWeight: 600 },
  affected: { background: "#f4f4f6", border: "1px solid #b9b9c4" },
  plain: { background: "#ffffff", border: "1px solid #d8d8e0" },
};

export function FailureMap({ graph, onset, observedFailureNodeId }: Props) {
  const focusNode = useSelection((state) => state.focusNode);
  const focused = useSelection((state) => state.focusedNodeId);

  const nodes = useMemo<Node[]>(
    () =>
      [...graph.nodes]
        .sort((a, b) => a.sequence - b.sequence)
        .map((node, index) => {
          const role = roleOf(node.node_id, onset, observedFailureNodeId);
          return {
            id: node.node_id,
            // A serpentine layout keeps a forty-step run on screen without a
            // dependency on a layout engine that would need its own tuning.
            position: {
              x: (index % 4) * COLUMN,
              y: Math.floor(index / 4) * ROW,
            },
            data: { label: `${node.sequence}  ${node.label || node.kind}` },
            style: {
              ...STYLES[role],
              borderRadius: 6,
              padding: "6px 10px",
              fontSize: 12,
              width: 210,
              outline: node.node_id === focused ? "3px solid #4a6cf7" : undefined,
            },
          };
        }),
    [graph.nodes, onset, observedFailureNodeId, focused],
  );

  const edges = useMemo<Edge[]>(() => {
    const observed = graph.edges
      .filter((edge) => edge.kind === "precedes")
      .map((edge) => ({
        id: `o:${edge.source_id}:${edge.target_id}`,
        source: edge.source_id,
        target: edge.target_id,
        style: { stroke: "#c9c9d4" },
      }));
    const inferred = graph.inferred_edges.map((edge) => ({
      id: `i:${edge.source_id}:${edge.target_id}`,
      source: edge.source_id,
      target: edge.target_id,
      animated: true,
      label: `may reach ${Math.round(edge.confidence * 100)}%`,
      labelStyle: { fontSize: 10, fill: "#8a6d1f" },
      style: { stroke: "#c8951a", strokeDasharray: "6 4" },
    }));
    return [...observed, ...inferred];
  }, [graph.edges, graph.inferred_edges]);

  return (
    <div style={{ height: "100%", border: "1px solid #e2e2ea", borderRadius: 8 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={(_, node) => focusNode(node.id)}
        fitView
        proOptions={{ hideAttribution: false }}
      >
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  );
}
