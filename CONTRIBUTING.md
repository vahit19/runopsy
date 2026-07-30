# Contributing

Thanks for looking. Runopsy is a diagnosis tool, so the bar it holds itself to is that a
confident statement can be trusted — most of what follows comes from that.

## Getting set up

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy packages/runopsy-core/src packages/runopsy-collector/src \
            packages/runopsy-cli/src packages/runopsy-bench/src \
            packages/runopsy-replay/src packages/runopsy-adapter/src tests
uv run runopsy bench --compare
```

CI runs all of it on Linux, macOS and Windows. The shell adapter runs real subprocesses
and path handling differs enough between platforms to have already caused one bug, so
please do not assume a green run on your machine is the whole story.

## Invariants

These are enforced by tests. If one starts failing, the change is wrong, not the test —
or the invariant needs an explicit, argued decision to move.

- A candidate may claim `replay_supported` only with a replay behind it, and
  `human_verified` only with a named verifier. Everything else is capped below certainty.
- No output path asserts causation for an unvalidated finding.
- Default detectors spend zero tokens. None may report a deterministic layer while
  calling a model.
- Normalization emits no `AFFECTS` edges. Propagation is inference and belongs to the
  impact layer, labelled and confidence-weighted.
- Nothing may affect the past: propagation only reaches steps that ran later.
- The replay gate fails closed. An unrecognised tool needs human approval.
- A healthy run produces no finding. The false-positive rate is exactly zero.
- The JSONL journal is authoritative; the DuckDB index is rebuildable from it.
- Diagnosis is a pure function of the trace — no clock, no network, no model.
- Observing never breaks the observed run.

## Adding a detector

A detector earns its place by finding something real without firing on ordinary work. A
check that warns during a normal run teaches people to ignore the tool, and an ignored
warning discredits the ones that matter.

1. Put it in `structural.py` (L0, reported facts) or `behavioral.py` (L1, patterns across
   steps). Both must be pure functions of the trace.
2. Add a case to `packages/runopsy-bench/src/runopsy_bench/cases.py` with a declared
   ground-truth onset.
3. Run `runopsy bench --compare`. If accuracy drops, the detector is adding noise.
4. Regenerate the report: `runopsy bench --write benchmarks/baseline-report.md`. CI fails
   if it is stale.

Do not tune thresholds to make a single fixture pass. The suite is 20 synthetic cases; a
number fitted to them measures nothing. An honest 94% beats a tuned 100%.

## Adding a runtime adapter

Adapters translate a runtime's events into the trace schema and nothing more.

- Build events with `RunRecorder` rather than hand-rolling ids and sequence numbers.
- Call `assert_adapter_contract` in your tests. It is shipped for exactly this.
- Record `state_delta` only for beliefs about the world. A per-step readout — an exit
  code, a counter — makes every run look like a state conflict. `warn_about_state_keys`
  catches the pattern.
- Never let a recording failure reach the runtime.

Prefer a documented wire protocol over importing a runtime's internals, so an upgrade
that reorganises its modules cannot break recording.

## Commits and style

Write commit messages that explain **why**, not what the diff shows. Reviewers can read
the diff; they cannot read the reasoning that produced it.

Code is English, including comments and identifiers. Comments should state a constraint
the code cannot show — not narrate the next line.

Please do not add `Co-Authored-By` trailers naming an AI assistant.

## Security

Do not open a public issue for anything exploitable. See [SECURITY.md](SECURITY.md).
