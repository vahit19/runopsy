/**
 * The local API, typed.
 *
 * Every shape here mirrors what runopsy-server returns. The one that matters is
 * `GraphResponse`: observed edges and inferred ones arrive in separate fields and are
 * kept separate all the way to the screen. A view that merged them would draw a
 * measurement and a guess as the same line, which is the single thing this product
 * exists not to do.
 */

export type RunSummary = {
  run_id: string;
  task: string;
  runtime: string;
  outcome: string;
  event_count: number;
  finished: boolean;
};

export type TraceNode = {
  node_id: string;
  kind: string;
  sequence: number;
  label: string;
  timestamp: string;
  attributes: Record<string, unknown>;
};

export type TraceEdge = {
  source_id: string;
  target_id: string;
  kind: string;
  confidence: number;
};

export type GraphResponse = {
  run_id: string;
  nodes: TraceNode[];
  edges: TraceEdge[];
  inferred_edges: TraceEdge[];
};

export type Candidate = {
  onset_node_id: string;
  status: string;
  confidence: number;
  summary: string;
  category: string;
  affected_node_ids: string[];
  signal_ids: string[];
  score_breakdown: Record<string, number>;
};

export type Diagnosis = {
  diagnosis_id: string;
  run_id: string;
  observed_failure_node_id: string | null;
  observed_failure_summary: string;
  candidates: Candidate[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    // Surface the server's own message. A generic "request failed" would hide the 404
    // that says the run has no events, which is the answer rather than an error.
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  runs: () => request<RunSummary[]>("/v1/runs"),
  graph: (runId: string) => request<GraphResponse>(`/v1/runs/${encodeURIComponent(runId)}/graph`),
  diagnose: (runId: string) =>
    request<Diagnosis>(`/v1/runs/${encodeURIComponent(runId)}/diagnose`, { method: "POST" }),
  reportUrl: (runId: string) => `/v1/runs/${encodeURIComponent(runId)}/report`,
};
