# runopsy-semantic

The optional layer that costs money. Everything else in Runopsy runs offline and free;
this asks a model about the handful of steps the deterministic engine already found
suspicious.

## What it may conclude

Very little, on purpose. A model can see what structural analysis cannot — a command
that succeeded while writing the wrong value, a claim the observed output contradicts —
and it can invent all of that with complete fluency. So the boundary is enforced, not
advised:

- It cannot produce a status. Findings are L3 signals that feed evidence and the
  ranking's evaluator term. `replay_supported` is reachable only through an experiment,
  and that path does not run through here.
- Its confidence is capped below the deterministic engine's own ceiling, and its
  severity is capped at medium regardless of how certain it claims to be.
- Unparseable output is discarded, never repaired. Coaxing meaning from a malformed
  response is how a hallucination becomes a finding.
- It only ever sees spans the deterministic layers flagged. It never roams the trace.

## What leaves the machine

The focus step and two neighbours on each side — not the run. `build_packet` takes a
window rather than a context, so minimization cannot be forgotten by a caller. Command
text comes from the local vault when one exists; steps whose payload was redacted are
sent as metadata only, and the packet records what was withheld so a weaker verdict is
visibly weaker.

Everything passes the secret scanner once more immediately before sending. That check
should never fire — the capture-time scan already ran — and it exists because "should
never" is not a security control.

## Budget

Ceilings are checked *before* a call, never after: a limit enforced afterwards is one
you discover on an invoice. Defaults follow section 12.1 — two calls, 6000 input tokens
each, ten cents. Verdicts are cached by evidence, model and prompt version, so re-reading
a diagnosis costs nothing.

Bring your own key (`OPENROUTER_API_KEY`). No key means the deterministic diagnosis runs
and answers anyway, which is the point of the offline-first design.
