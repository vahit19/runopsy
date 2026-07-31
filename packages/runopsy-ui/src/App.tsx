import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { api, type TraceNode } from "./api";
import { DiagnosisPanel } from "./Diagnosis";
import { EvidencePanel } from "./Evidence";
import { FailureMap } from "./FailureMap";
import { useSelection } from "./store";

export function App() {
  const { runId, focusedNodeId, selectRun } = useSelection();

  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs });

  // Open on the most recent run, which is what someone arriving after a failure wants.
  useEffect(() => {
    if (!runId && runs.data?.length) selectRun(runs.data[0]!.run_id);
  }, [runId, runs.data, selectRun]);

  const graph = useQuery({
    queryKey: ["graph", runId],
    queryFn: () => api.graph(runId!),
    enabled: Boolean(runId),
  });

  const diagnosis = useQuery({
    queryKey: ["diagnosis", runId],
    queryFn: () => api.diagnose(runId!),
    enabled: Boolean(runId),
  });

  const nodesById = useMemo(() => {
    const map = new Map<string, TraceNode>();
    graph.data?.nodes.forEach((node) => map.set(node.node_id, node));
    return map;
  }, [graph.data]);

  const leading =
    diagnosis.data?.candidates.filter(
      (candidate) => candidate.onset_node_id !== diagnosis.data?.observed_failure_node_id,
    ) ?? [];
  const onset = leading[0] ?? null;
  const focusedCandidate =
    diagnosis.data?.candidates.find((c) => c.onset_node_id === focusedNodeId) ?? null;

  return (
    <div className="layout">
      <header>
        <h1>Runopsy</h1>
        <select
          value={runId ?? ""}
          onChange={(event) => selectRun(event.target.value)}
          disabled={!runs.data?.length}
        >
          {runs.data?.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.run_id} — {run.event_count} events — {run.finished ? run.outcome : "unfinished"}
            </option>
          ))}
        </select>
        {runId && (
          <a href={api.reportUrl(runId)} target="_blank" rel="noreferrer">
            open report
          </a>
        )}
        <span className="gloss local">local only — nothing leaves this machine</span>
      </header>

      {runs.isError && <p className="error">Could not reach the local API. Is `runopsy ui` running?</p>}
      {runs.data?.length === 0 && <p className="gloss">No runs recorded yet.</p>}

      <main>
        <div className="map">
          {graph.data ? (
            <FailureMap
              graph={graph.data}
              onset={onset}
              observedFailureNodeId={diagnosis.data?.observed_failure_node_id ?? null}
            />
          ) : (
            <p className="gloss">{graph.isError ? String(graph.error) : "Loading the trace…"}</p>
          )}
        </div>

        <aside>
          {diagnosis.data && <DiagnosisPanel diagnosis={diagnosis.data} nodes={nodesById} />}
          <EvidencePanel
            node={focusedNodeId ? (nodesById.get(focusedNodeId) ?? null) : null}
            candidate={focusedCandidate}
          />
        </aside>
      </main>
    </div>
  );
}
