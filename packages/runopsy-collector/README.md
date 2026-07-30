# runopsy-collector

Local, append-only ingest for normalized trace events.

Storage has two layers and they are not equal partners. The **JSONL journal is the
source of truth**: one append-only file per run, written with sorted keys so the same
run always serializes to the same bytes. **DuckDB is a derived index** built from those
journals to make runs queryable, and it can be thrown away and rebuilt at any time with
`Collector.rebuild()`.

That asymmetry is deliberate. An agent run fails at unpredictable moments, often taking
the process with it, and a half-written database is a far more likely outcome than a
half-written append. Keeping the plain log authoritative means a crashed run is still
diagnosable, a corrupted index is a one-command repair rather than lost history, and a
user can read their own trace with nothing but a text editor.

Ingest is idempotent on `event_id`, so an adapter that retries a write cannot duplicate
a step and inflate a loop detector's count.

Nothing here reaches the network. Content is referenced by hash, and the
`contains_secret` flag travels with each event so export and payload minimization can
consult it before anything leaves the machine.
