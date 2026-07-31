# Changelog

All notable changes to Runopsy. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses semantic versioning once published.

## [Unreleased]

## [0.1.0] — 2026-07-31

First release. The deterministic pipeline works end to end: record a run, find where it
started going wrong, read the evidence, test the suspicion with a counterfactual replay,
and score the whole thing against labelled traces.

Install with `uv tool install runopsy`, `pipx install runopsy` or `pip install runopsy`.
Everything works offline with no provider key.

**Measured**, reproducible with `runopsy bench --compare`: onset top-1 94.4%, top-3
100%, mean step distance 0.11, false positives 0.0% — against 22.2% for blaming the last
failing step, which is what reading a log bottom-up achieves. These are twenty synthetic
single-fault traces; they show the ranking behaves as designed and do not yet show it
saves anyone time on real work. `runopsy label` exists so that changes.

**Eleven packages.** `runopsy` (the meta-distribution) · core · collector · cli · replay
· bench · adapter · semantic · server · ui · inspect.

**Sixteen commands**, including `run` (drive an agent and diagnose it in one), `label`
(turn a real failure into a benchmark case), `graph`, and `adapter hermes plugin`.

**What it will not do**: claim a cause it has not validated, send anything anywhere
without `--mode hybrid`, execute a replay outside a disposable sandbox, or report a
finding on a healthy run.


### Added
- **Replay execution.** `runopsy replay --execute` runs the replayable steps of a plan
  in a disposable sandbox copy of the project. With `--skip-onset` or `--substitute`
  it becomes a counterfactual experiment: one intervention at the suspected onset,
  downstream failures re-run and compared. Only "the failures disappeared when the
  onset was changed" upgrades a candidate to a replay-supported cause — reproduction
  without an intervention is reported as consistency, never causation.
- **Payload vault.** Command text is kept content-addressed on the local machine so a
  replay can re-run it. The trace itself still stores hashes only; secrets are redacted
  before the vault, and redacted payloads refuse to execute.
- **Replay lineage in the schema.** Replay runs record `parent_run_id`,
  `intervention_kind` and `intervention_target`, so `runopsy diagnose` folds stored
  experiments back into the parent's diagnosis across sessions.
- **Validation can name what detection could not.** When an experiment establishes a
  step that produced no anomaly at all — the silent-wrong-value case — a
  replay-supported candidate is created with category `undetermined`, stating that
  causation was demonstrated but the mechanism was not classified.
- **`runopsy.toml`.** Detector thresholds, budgets, replay sandbox settings and the
  vault switch. Every key is honored; unknown keys are reported rather than silently
  ignored. `runopsy config --init` writes a commented starter.
- **Hermes adapter** verified against hermes-agent 0.19.0's documented shell-hook wire
  protocol, with `runopsy hook` (never fails the observed run) and `runopsy adapter
  hermes` (prints the config to paste).
- **Shell adapter and `runopsy record`**: wrap any pipeline and diagnose it.
- Benchmark baselines (`runopsy bench --compare`) with a committed, timestamp-free
  report; CI on three platforms; security workflow with dependency audit, static
  analysis, secret scan and SBOM.

### Added (continued)
- **`runopsy setup`** stores a provider key in the OS credential store, and resolution
  now honours flag, environment, keyring and a developer `.env` in that order, with
  `doctor` naming the source and never the value.
- **Local API and `runopsy ui`** (section 19.2): runs, graph, diagnosis, replay plan and
  the HTML report over loopback. Replay execution is deliberately not exposed.
- **Fault injection** (`runopsy bench --inject`): break a clean run on purpose and score
  the engine on faults it was not written from.
- **Release workflow** with a full three-platform gate, a tag-versus-version check and
  trusted publishing.

### Added (final)
- **Performance measurement** (`runopsy bench --perf`) and the ingest rewrite it forced:
  batched journal appends and DuckDB bulk loading took recording from 17 to 31,000
  events per second.
- **Retention** (`runopsy prune`): delete traces past a window, only on request, only
  with `--apply`, never a run whose age is unknown.
- **Structured logging** via `RUNOPSY_LOG`, silent by default and redacting anything
  credential-shaped.
- **OpenInference/OTLP export** (`runopsy export --otlp`), carrying the diagnosis as
  span attributes.

### Measured
- Onset localization on 20 labelled synthetic traces: top-1 94.4%, top-3 100%, mean
  step distance 0.11, zero false positives. `last_failure` (reading a log bottom-up)
  scores 22.2% top-1.
