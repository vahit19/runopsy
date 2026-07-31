/** What one step recorded. Digests stay truncated; the trace holds no raw content. */

import type { Candidate, TraceNode } from "./api";

const DIGEST_PREVIEW = 14;

function formatValue(value: unknown): string {
  if (typeof value === "string" && value.startsWith("sha256:")) {
    return `${value.slice(0, DIGEST_PREVIEW)}…`;
  }
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function EvidencePanel({
  node,
  candidate,
}: {
  node: TraceNode | null;
  candidate: Candidate | null;
}) {
  if (!node) {
    return (
      <section className="panel">
        <h3>Evidence</h3>
        <p className="gloss">Select a step to see what it recorded.</p>
      </section>
    );
  }

  const entries = Object.entries(node.attributes).filter(
    ([, value]) => value !== null && value !== "" && value !== undefined,
  );

  return (
    <section className="panel">
      <h3>
        step {node.sequence} {node.label || node.kind}
      </h3>
      <p className="gloss">
        {node.kind} · {new Date(node.timestamp).toLocaleTimeString()}
      </p>

      <dl className="attrs">
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{formatValue(value)}</dd>
          </div>
        ))}
      </dl>

      {candidate ? (
        <>
          <h4>Why it ranked here</h4>
          <dl className="attrs">
            {Object.entries(candidate.score_breakdown).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          {candidate.signal_ids.length > 0 && (
            <>
              <h4>Signals</h4>
              <ul className="gloss">
                {candidate.signal_ids.map((id) => (
                  <li key={id}>{id}</li>
                ))}
              </ul>
            </>
          )}
        </>
      ) : (
        <p className="gloss">No failure signal is attached to this step.</p>
      )}
    </section>
  );
}
