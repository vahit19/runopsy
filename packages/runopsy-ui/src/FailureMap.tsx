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

// Nodes are 210 wide; the gap between columns has to hold a "may reach 42%" label
// without it colliding with the box beside it.
const COLUMN = 330;
const ROW = 82;
const MAX_LABEL = 26;

/** One line that fits the box.
 *
 * A run's label is its task, and a task is often a repository path. Left whole it
 * overflowed the node, wrapped across three lines and covered the two steps below it —
 * so the first thing anyone saw in the web view was a filesystem path obscuring the
 * trace. The terminal graph has truncated for exactly this reason since it was written;
 * this view simply never got the same treatment.
 */
function fitLabel(text: string): string {
  const flat = text.replace(/\s+/gu, " ").trim();
  if (flat.length <= MAX_LABEL) return flat;
  // Keep the end of a path rather than the start: `...\hermes-live\fail` identifies the
  // run, `C:\Users\someone\AppData\...` identifies everybody's machine.
  return "..." + flat.slice(-(MAX_LABEL - 3));
}

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

/** Styling carries the reading order, because fifty near-identical boxes do not.
 *
 * A user looking at a fifty-step run said the map told them nothing, and they were
 * right: every step had the same weight, so the two that matter were lost among the
 * forty-eight that do not. The marked steps are now larger, bolder and darker, and the
 * ordinary ones are deliberately faint — a step nobody needs to read should not compete
 * with the one they do.
 */
const STYLES: Record<string, React.CSSProperties> = {
  onset: {
    background: "#ffe9a8",
    border: "3px solid #b8860b",
    fontWeight: 700,
    color: "#4a3600",
    fontSize: 13,
  },
  failure: {
    background: "#ffd0d0",
    border: "3px solid #b0281a",
    fontWeight: 700,
    color: "#5c1410",
    fontSize: 13,
  },
  affected: { background: "#fdf6e6", border: "1px solid #d8b878", color: "#6b5a30" },
  plain: { background: "#fbfbfd", border: "1px solid #e6e6ee", color: "#9a9aa8" },
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
            // The two steps that matter say what they are, in words. A colour is only
            // legible to someone who has already read the legend; a label is not.
            data: {
              label:
                role === "onset"
                  ? `STARTED HERE - step ${node.sequence} ${fitLabel(node.label || node.kind)}`
                  : role === "failure"
                    ? `FAILED HERE - step ${node.sequence} ${fitLabel(node.label || node.kind)}`
                    : `${node.sequence}  ${fitLabel(node.label || node.kind)}`,
            },
            title: node.label || node.kind,
            style: {
              ...STYLES[role],
              borderRadius: 6,
              padding: "6px 10px",
              // Belt and braces with fitLabel: a node must never grow to fit its text
              // and cover the steps around it, whatever a future runtime puts here.
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              fontSize: 12,
              width: role === "onset" || role === "failure" ? 268 : 214,
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
    <div className="mapframe">
      <div className="maphint">
        Each box is one step, in order. The run <b>failed</b> at the red box — it{" "}
        <b>started going wrong</b> at the amber one. Dashed amber lines are what that
        step <i>may</i> have reached: inference, not something observed. Click any step
        to see what it recorded.
      </div>
      <div className="mapcanvas">
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
      <div className="maplegend">
        <span>
          <i className="swatch onset" /> started going wrong
        </span>
        <span>
          <i className="swatch failure" /> visible failure
        </span>
        <span>
          <i className="swatch affected" /> may have been affected
        </span>
        <span>
          <i className="swatch plain" /> nothing found here
        </span>
      </div>
    </div>
  );
}
