# Changelog

All notable changes to Runopsy. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses semantic versioning once published.

## [Unreleased]

## [0.1.3] — 2026-08-02

Found by installing the published wheel and using it the way a stranger would.

**Fixed — a clean run no longer reads as a tool that does nothing.** Most runs succeed,
so the output most users see most often was "Nothing detectable went wrong" and then
silence. Correct, and useless: somebody records their ordinary green pipeline, is told
there is nothing to report, and concludes the tool does nothing. A clean verdict now says
what it examined — how many tool and model calls, against how many detectors — and what
the run left in the working tree.

**Fixed — the README told readers the thing was not published.** Its first command
installs from PyPI; the paragraph beneath it said the CLI had not landed and to clone the
repository instead. True for a day, false since. `runopsy verify` was also missing from
the command table.

**Fixed — the Hermes plugin found nothing and said nothing.** It resolved the `runopsy`
executable through PATH, and Runopsy and Hermes live in separate virtualenvs, so it never
found one — producing traces with tool calls and no model calls at all. The installer
records the path now. Verified on live sessions: 30 `llm_call` events with real token
counts where there had been none.

**Fixed — the recording lock gave up after ten seconds.** Windows' blocking mode raises
at about that point with no way to wait longer, so a busy moment became an exception on
the recording path — a step of history never written down.

**Changed** — the README's images are regenerated from a real 61-step Hermes session
rather than a constructed trace, and `render_ui.mjs` moved into `packages/runopsy-ui/`,
where its dependency actually is; the documented command could never have run from the
repository root.

## [0.1.2] — 2026-08-02

**Added — checkpoints, so a replay is about the original run.** `runopsy replay` has
always looked for a point to return to and never found one, because nothing recorded the
working tree; every plan carried "file state cannot be restored" and every execution
started from whatever was on disk *now* — after the failure, after any manual fixing,
possibly weeks later. Runs in a repository now record the commit and a patch of the
uncommitted changes wherever the tree moves, and executing a plan restores that tree in
the sandbox first. R2 session fork works end to end: breaking a file, noticing two steps
later, skipping the breaking step, and watching the failure disappear.

The patch goes to Runopsy's own vault, secret-scanned like every other payload, rather
than into the user's repository — the same rule that made the store exclude itself from
the agent's commits. `.git` is kept in the sandbox copy only when a checkpoint needs it,
so replays without one stay as cheap as they were. The verdict states what it restored,
because that decides what the result is evidence *about*.

**Changed** — `CheckpointPayload` gained `patch_digest`, so the trace schema moves to
0.2. Stores written by an older build keep saying so and are read normally; a store
written by a newer one is refused rather than degraded.

**Fixed** — a sandbox copy no longer aborts on one unreadable file. `copytree` raises on
the first error, so a database or log held open by another process ended the experiment
with a WinError stack trace instead of a result.

**Fixed** — every package pins its siblings, not only the meta-distribution. Publishing
0.1.1 while PyPI's index was catching up resolved a 0.1.1 CLI onto a 0.1.0 collector: the
release installed without the fix it was released for, and nothing said anything. These
packages share one trace schema and are released together, so any set that is not one
version is a set nobody has run the tests against. As published, 0.1.1 resolves correctly
today, because every sibling is at its newest.

## [0.1.1] — 2026-08-02

A correctness release. Anyone recording with 0.1.0 should upgrade: it loses steps, and
it loses them quietly.

**Fixed — recording under parallelism.** An agent that delegates to parallel subagents
fires one `runopsy hook` process per event, and DuckDB admits a single writing process.
Thirty-two concurrent hooks against one store lost twelve events. Silently, because a
hook's first duty is not to break the run it observes, so it swallowed the error and
exited zero and the run looked recorded. Three separate faults: dedup queried the index
before the journal was appended, so a locked database discarded the event before anything
durable existed; opening the collector connects to the index, so contention failed the
write path outright; and step numbers came from `SELECT MAX(sequence) + 1`, which two
subagents in one session took simultaneously — an adapter builds the event id out of that
number, so the second step was deduplicated away as a repeat of the first. Now
thirty-two of thirty-two.

**Fixed — the index no longer has to be repaired by hand.** "The journal is
authoritative and the index is rebuildable" was true of the design and not of the code:
nothing rebuilt. Reading now reconciles the two, so a run recorded and diagnosed
immediately is not whichever steps won the race. A run recorded entirely through the
journal-only fallback is also listed by `runopsy runs` and reachable as `latest`.

**Fixed — recording no longer alters the run it observes.** The store sits inside the
working tree by default, so an agent's own `git add -A` swept it into a commit and then
failed, DuckDB holding the index open where git wanted to read. The store excludes itself
now, without touching the user's own ignore file.

**Added — what each step did to the repository.** A coding agent's real output is the
working tree, and the trace did not contain it. Each step now carries the commit and
branch it moved to, and the files it changed with their line counts; `runopsy evidence`
shows them. Turn it off with `capture.git = false`.

**Improved — the loop detector can see files.** A repeated verification command is not a
loop while the working tree keeps reaching states it has not been in. Benchmark
unchanged; an ordinary edit-test cycle no longer reports a loop.

**Added — local models.** `semantic.base_url` accepts any OpenAI-compatible endpoint, and
a loopback address needs no key. Pointed at Ollama, the semantic layer runs with nothing
leaving the machine — which the local-first promise already claimed and this one package
could not deliver.

**Added — provider retry.** Four attempts, waiting 0.1s, 0.2s and 0.4s, and only for a
timeout or a refused connection. An HTTP status means the provider answered: repeating a
4xx cannot mend it, a 429 met with an immediate retry is how a rate limit becomes a ban,
and a 5xx may have already run — and billed for — the model.

**Added — `runopsy doctor` looks at the store**, not only at the settings: whether the
index has fallen behind the journals, and whether any journal has duplicate step numbers,
events out of order, or events belonging to another run.

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
