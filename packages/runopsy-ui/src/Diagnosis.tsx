/**
 * The diagnosis panel.
 *
 * Wording is deliberately the same as the terminal's. A finding that reads as a
 * suspicion in one surface and as a conclusion in another has no calibration at all,
 * and the web view is the surface most likely to be screenshotted into a ticket.
 */

import type { Candidate, Diagnosis, TraceNode } from "./api";
import { useSelection } from "./store";

const HEADING: Record<string, string> = {
  observed_failure: "Observed failure",
  suspected_onset: "Suspected onset",
  correlated_cause: "Correlated cause",
  replay_supported: "Cause, supported by replay",
  human_verified: "Cause, verified by a person",
  unknown: "Unclear",
};

const GLOSS: Record<string, string> = {
  observed_failure: "what the run visibly got wrong",
  suspected_onset: "where it may have started going wrong, unverified",
  correlated_cause: "correlated in time, not demonstrated",
  replay_supported: "an experiment changed this step and the failures went away",
  human_verified: "confirmed by a named person",
  unknown: "not enough signal to place this",
};

function describe(nodes: Map<string, TraceNode>, nodeId: string): string {
  const node = nodes.get(nodeId);
  return node ? `step ${node.sequence}${node.label ? ` ${node.label}` : ""}` : nodeId;
}

function confidencePhrase(candidate: Candidate): string {
  const percent = Math.round(candidate.confidence * 100);
  const validated =
    candidate.status === "replay_supported" || candidate.status === "human_verified";
  return `${percent}% confidence, ${validated ? "validated" : "unverified"}`;
}

export function DiagnosisPanel({
  diagnosis,
  nodes,
}: {
  diagnosis: Diagnosis;
  nodes: Map<string, TraceNode>;
}) {
  const focusNode = useSelection((state) => state.focusNode);
  const leading = diagnosis.candidates.filter(
    (candidate) => candidate.onset_node_id !== diagnosis.observed_failure_node_id,
  );
  const primary = leading[0] ?? null;

  if (diagnosis.candidates.length === 0) {
    return (
      <section className="panel ok">
        <h2>Nothing detectable went wrong</h2>
        <p>Deterministic analysis found no failing step, loop, or state conflict.</p>
      </section>
    );
  }

  return (
    <section className="panel">
      {diagnosis.observed_failure_node_id && (
        <div className="block failure">
          <h2>Observed failure</h2>
          <p className="gloss">what the run visibly got wrong</p>
          <button
            className="steplink"
            onClick={() => focusNode(diagnosis.observed_failure_node_id)}
          >
            {describe(nodes, diagnosis.observed_failure_node_id)}
          </button>
          <p>{diagnosis.observed_failure_summary}</p>
        </div>
      )}

      {primary && (
        <div className="block onset">
          <h2>{HEADING[primary.status] ?? primary.status}</h2>
          <p className="gloss">{GLOSS[primary.status] ?? ""}</p>
          <button className="steplink" onClick={() => focusNode(primary.onset_node_id)}>
            {describe(nodes, primary.onset_node_id)}
          </button>
          <p>{primary.summary}</p>
          <p className="gloss">{confidencePhrase(primary)}</p>
          {primary.affected_node_ids.length > 0 && (
            <p className="gloss">
              may have affected{" "}
              {primary.affected_node_ids
                .slice(0, 3)
                .map((id) => describe(nodes, id))
                .join(", ")}
              {primary.affected_node_ids.length > 3
                ? ` and ${primary.affected_node_ids.length - 3} more`
                : ""}
            </p>
          )}
        </div>
      )}

      {leading.length > 1 && (
        <div className="block">
          <h3>Other candidates</h3>
          <ul>
            {leading.slice(1, 6).map((candidate) => (
              <li key={candidate.onset_node_id}>
                <button
                  className="steplink"
                  onClick={() => focusNode(candidate.onset_node_id)}
                >
                  {describe(nodes, candidate.onset_node_id)}
                </button>{" "}
                — {candidate.summary} ({confidencePhrase(candidate)})
              </li>
            ))}
          </ul>
        </div>
      )}

      {primary && !["replay_supported", "human_verified"].includes(primary.status) && (
        <div className="note">
          No cause has been confirmed. Nothing here is proof of causation — the
          propagation arcs show reachability, not a demonstrated effect. To test the
          suspected onset, replay from it in a terminal:
          <code>
            runopsy replay {diagnosis.run_id} --from-step{" "}
            {nodes.get(primary.onset_node_id)?.sequence ?? 0}
          </code>
        </div>
      )}
    </section>
  );
}
