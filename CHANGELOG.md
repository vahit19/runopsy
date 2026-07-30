# Changelog

All notable changes to Runopsy. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses semantic versioning once published.

## [Unreleased]

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

### Measured
- Onset localization on 20 labelled synthetic traces: top-1 94.4%, top-3 100%, mean
  step distance 0.11, zero false positives. `last_failure` (reading a log bottom-up)
  scores 22.2% top-1.
