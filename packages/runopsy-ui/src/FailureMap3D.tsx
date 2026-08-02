/**
 * The optional 3D view of a run.
 *
 * The design document is blunt about this view's place: optional, last, never the
 * default, and never the product. It exists because a long run reads differently as a
 * path through space — depth carries time, height carries trouble — not because a
 * failure map needs to rotate. The 2D map stays the default and this file is loaded
 * lazily, so nobody who ignores the toggle pays for Three.js.
 *
 * The epistemic rule survives the extra dimension: recorded steps are solid geometry,
 * inferred propagation is a translucent arc. A guess must not become more convincing
 * for having been drawn in 3D.
 */

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useMemo, useRef, useState } from "react";
import * as THREE from "three";

import type { Candidate, GraphResponse, TraceNode } from "./api";
import { useSelection } from "./store";

const STEP_SPACING = 1.4;
const CROWDED_RUN = 30;
// Above this many steps the blocks are too small to read individually, and saying so
// under a picture where they plainly are readable is worse than saying nothing.

type Role = "onset" | "failure" | "affected" | "candidate" | "plain";

const ROLE_COLOR: Record<Role, string> = {
  onset: "#c8951a",
  failure: "#c0392b",
  affected: "#8a8a96",
  candidate: "#d4b45a",
  plain: "#b9c2d0",
};

// Trouble literally raises the step: a run with no findings is a flat road. The spread
// is deliberately wide — seen from the distance a fifty-step run needs, a difference of
// half a unit is invisible, and a signal you cannot see is not a signal.
const ROLE_HEIGHT: Record<Role, number> = {
  onset: 6.0,
  failure: 5.0,
  affected: 1.6,
  candidate: 3.0,
  plain: 0.5,
};

function roleOf(
  node: TraceNode,
  onset: Candidate | null,
  observedFailureNodeId: string | null,
  candidateIds: Set<string>,
): Role {
  if (node.node_id === onset?.onset_node_id) return "onset";
  if (node.node_id === observedFailureNodeId) return "failure";
  if (onset?.affected_node_ids.includes(node.node_id)) return "affected";
  if (candidateIds.has(node.node_id)) return "candidate";
  return "plain";
}

function StepBox({
  node,
  role,
  index,
  focused,
}: {
  node: TraceNode;
  role: Role;
  index: number;
  focused: boolean;
}) {
  const focusNode = useSelection((state) => state.focusNode);
  const [hovered, setHovered] = useState(false);
  const height = ROLE_HEIGHT[role];

  return (
    <mesh
      position={[0, height / 2, -index * STEP_SPACING]}
      onClick={(event) => {
        event.stopPropagation();
        focusNode(node.node_id);
      }}
      onPointerOver={() => setHovered(true)}
      onPointerOut={() => setHovered(false)}
    >
      <boxGeometry args={[1, height, 1]} />
      <meshStandardMaterial
        color={ROLE_COLOR[role]}
        emissive={focused || hovered ? ROLE_COLOR[role] : "#000000"}
        emissiveIntensity={focused ? 0.7 : hovered ? 0.35 : 0}
      />
    </mesh>
  );
}

/** A translucent arc from the onset to a possibly-affected step. Inference, visibly. */
function PropagationArc({
  from,
  to,
  confidence,
}: {
  from: number;
  to: number;
  confidence: number;
}) {
  const geometry = useMemo(() => {
    const start = new THREE.Vector3(0, ROLE_HEIGHT.onset, -from * STEP_SPACING);
    const end = new THREE.Vector3(0, 0.6, -to * STEP_SPACING);
    const middle = start
      .clone()
      .add(end)
      .multiplyScalar(0.5)
      .add(new THREE.Vector3(1.6, 1.2, 0));
    const curve = new THREE.QuadraticBezierCurve3(start, middle, end);
    return new THREE.TubeGeometry(curve, 24, 0.035, 6, false);
  }, [from, to]);

  return (
    <mesh geometry={geometry}>
      <meshStandardMaterial
        color="#c8951a"
        transparent
        opacity={0.15 + confidence * 0.45}
        depthWrite={false}
      />
    </mesh>
  );
}

/** Gentle orbit around the timeline; a static 3D view is a worse 2D view.
 *
 * The orbit distance scales with the length of the run. A fixed radius framed a
 * forty-step run as a corridor receding to a vanishing point: the onset sat at the far
 * end as a few pixels, the propagation arcs were off-screen, and the picture showed
 * nothing the 2D map does not show better. A view that has to be readable at fifty
 * steps cannot be framed for five.
 */
function Rig({ length }: { length: number }) {
  const { camera } = useThree();
  const angle = useRef(0.6);
  const span = Math.max(length, 6) * STEP_SPACING;

  useFrame((_, delta) => {
    angle.current += delta * 0.06;
    const middle = -span / 2;
    // Framed from above rather than from the end. A long run seen along its own axis is
    // a corridor: the first steps fill the frame and the last are a vanishing point, so
    // the onset — which is usually early — falls off the near edge entirely. Looking
    // down at it turns the run into a line across the ground, where every step is
    // visible at once and the raised ones still read as raised.
    const radius = span * 0.42 + 8;
    camera.position.set(
      Math.sin(angle.current) * radius,
      span * 0.78 + 6,
      middle + Math.cos(angle.current) * radius,
    );
    camera.lookAt(0, 0, middle);
  });
  return null;
}

export default function FailureMap3D({
  graph,
  onset,
  observedFailureNodeId,
}: {
  graph: GraphResponse;
  onset: Candidate | null;
  observedFailureNodeId: string | null;
}) {
  const focused = useSelection((state) => state.focusedNodeId);
  const ordered = useMemo(
    () => [...graph.nodes].sort((a, b) => a.sequence - b.sequence),
    [graph.nodes],
  );
  const indexById = useMemo(
    () => new Map(ordered.map((node, index) => [node.node_id, index])),
    [ordered],
  );
  const candidateIds = useMemo(
    () => new Set((onset ? [onset] : []).map((candidate) => candidate.onset_node_id)),
    [onset],
  );

  const stepOf = (id: string | null | undefined): number | null =>
    ordered.find((node) => node.node_id === id)?.sequence ?? null;
  const onsetStep = stepOf(onset?.onset_node_id);
  const failureStep = stepOf(observedFailureNodeId);

  const arcs = useMemo(() => {
    if (!onset) return [];
    const from = indexById.get(onset.onset_node_id);
    if (from === undefined) return [];
    return onset.affected_node_ids
      .map((id, position) => ({
        to: indexById.get(id),
        confidence: Math.max(0.15, 0.8 - position * 0.12),
      }))
      .filter((arc): arc is { to: number; confidence: number } => arc.to !== undefined)
      .map((arc) => ({ ...arc, from }));
  }, [onset, indexById]);

  return (
    <div className="map3d">
      <Canvas camera={{ position: [8, 6, 6], fov: 50 }}>
        <color attach="background" args={["#101319"]} />
        <ambientLight intensity={0.7} />
        <directionalLight position={[6, 10, 4]} intensity={1.1} />
        <Rig length={ordered.length} />
        {ordered.map((node, index) => (
          <StepBox
            key={node.node_id}
            node={node}
            index={index}
            role={roleOf(node, onset, observedFailureNodeId, candidateIds)}
            focused={node.node_id === focused}
          />
        ))}
        {arcs.map((arc) => (
          <PropagationArc key={`${arc.from}:${arc.to}`} {...arc} />
        ))}
        <gridHelper
          args={[Math.max(20, ordered.length * STEP_SPACING * 1.2), 24, "#2a2f3a", "#1a1e26"]}
          position={[0, 0, (-ordered.length * STEP_SPACING) / 2]}
        />
      </Canvas>
      <div className="caption3d">
        <p>
          The run reads left to right, one block per step. It{" "}
          <b className="amber">started going wrong</b> at the tall amber block
          {onsetStep !== null ? ` (step ${onsetStep})` : ""} and{" "}
          <b className="red">failed visibly</b> at the red one
          {failureStep !== null ? ` (step ${failureStep})` : ""}. Height is how serious
          the finding was, so a healthy run is a flat road.
        </p>
        <p className="gloss">
          A translucent arc is inference — what the onset <i>may</i> have reached — never
          something observed.
          {ordered.length > CROWDED_RUN
            ? " At this length the blocks are too small to read one by one, so the 2D map" +
              " is the better view; this one is here for the shape of the run."
            : " Click the 2D map for the detail behind any step."}
        </p>
      </div>
    </div>
  );
}
