# runopsy-core

The framework-agnostic heart of Runopsy: the normalized trace schema, the typed
execution graph, the deterministic detector registry and causal ranking.

This package must never import runtime-specific code. Runtime adapters (Hermes and
later others) translate their own events into the schema defined here, and everything
downstream — diagnosis, replay planning, the UI and the benchmark — consumes only
this normalized form. That boundary is what keeps Runopsy independent of any single
agent framework.

Two properties are structural rather than optional:

- **Content is referenced by hash, not stored.** Prompts, tool arguments, claims and
  evidence carry `*_hash` fields. Raw text stays on the user's machine, so a trace can
  be shared or sent to a diagnostic model without leaking source code or secrets.
- **Events are ordered and gap-checked.** Every event carries a monotonic `sequence`
  within its run, so a truncated or tampered trace is detectable instead of silently
  producing a confident, wrong diagnosis.
