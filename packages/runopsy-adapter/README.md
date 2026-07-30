# runopsy-adapter

What a runtime adapter is built from: a recorder that owns trace identity, a secret
scanner that runs at capture time, a contract any adapter must satisfy, and a working
shell adapter that records real command runs.

## Why the recorder owns identity

An adapter's job is to know its runtime, not the trace format. Left to hand-roll ids and
sequence numbers, every adapter reinvents the same three bugs — duplicate ids, gaps,
events attributed to the wrong run — and the integrity checker then reports them as
corruption in the user's trace rather than as a defect in the adapter. `RunRecorder`
allocates sequence numbers and ids so a trace is contiguous by construction.

## Why scanning happens at capture

`contains_secret` originates here and nowhere else. A credential that reaches the
journal is already on disk, and every later control is then trying to unring a bell.
Arguments and output are scanned and hashed at the moment they are recorded; the text
itself never enters the trace.

Detection is best effort by construction. It is one layer among several, never the
reason it is safe to write something down.

## The contract

`assert_adapter_contract` is shipped rather than kept in this repository's tests,
because the adapters that matter will live elsewhere and track their own runtime
versions. Each rule exists because breaking it yields a confident wrong answer instead
of an obvious error:

- exactly one `run_start`, and it comes first
- at most one `run_end`, and nothing recorded after it
- unique event ids — ingest deduplicates on them, so a collision silently drops a step
- contiguous ascending sequence numbers — a diagnosis over a gap misplaces the onset
- every event belongs to the run being recorded
- every timestamp timezone-aware — otherwise traces from two machines interleave wrongly

## The shell adapter

`record_steps` runs real commands and records them. It is not an agent runtime, but it
emits the same events one would, so the engine gets data nobody staged. Execution
continues past a failure on purpose: an agent carries on after a step goes wrong, and
the distance between the step that broke and the step where it became visible is
precisely what Runopsy exists to close. Stopping at the first error would only ever
produce traces whose onset is also the symptom.
