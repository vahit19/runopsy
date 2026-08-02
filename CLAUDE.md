# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

The ten-item first sprint from section 24 of the design document is complete. The deterministic pipeline works end to end: record a run, diagnose it, read the evidence, plan a replay, export a report, and score the whole thing against baselines.

The 39-page Turkish design document (v0.2, 30 July 2026) that this project was built from is **no longer in the repository** — it is the owner's private document and was removed deliberately. Do not re-add it.

That makes this file the working authority for what has been decided, so it has to carry the reasoning rather than point at a PDF nobody cloning can read. Where a decision here cites a design section, the citation is a record of where it came from, not somewhere to go and look. If something genuinely needs the original, ask the owner rather than inventing an approach and calling it the design.

**Shipped**, eleven packages. `runopsy` is the meta-distribution `pip install runopsy` resolves to — it depends on `runopsy-cli` and `runopsy-server` at pinned versions and is what makes `uv tool install runopsy` and `uvx runopsy` work; the nine below it are the real code, so a library user can depend on just the piece they need: `runopsy-core` (schema, normalizer, 15 detectors, impact, ranking, diagnosis, structured logging, OpenInference/OTLP export) · `runopsy-collector` (JSONL journals, DuckDB index, payload vault, retention) · `runopsy-cli` (14 commands, see *Interfaces*) · `runopsy-replay` (planning **and** counterfactual execution behind a fail-closed side-effect gate) · `runopsy-bench` (20 labelled cases, metrics, baselines, fault injection, performance) · `runopsy-adapter` (shell + Hermes, verified against hermes-agent 0.19.0) · `runopsy-semantic` (L3, budget-capped, opt-in) · `runopsy-server` (FastAPI, loopback, serves the built web view) · `runopsy-ui` (React + TypeScript + Vite + XYFlow, the 2D timeline and failure map) · `runopsy-inspect` (reads Inspect AI eval logs into traces).

Replay execution runs a counterfactual experiment in a sandbox copy; a supporting result upgrades a candidate to `replay_supported`, including creating one at a step the detectors could not see. Configuration lives in `runopsy.toml` (`runopsy config --init`); every key is honored and unknown keys are reported. Payload text is kept in a local vault (hashes only in the trace; secrets redacted; redacted payloads refuse to execute).

**Published.** All ten Python distributions are on PyPI; `pip install runopsy` works and is verified from a clean venv. 0.1.0 is published *and loses steps under parallel recording* — anyone still on it should upgrade.

**What each step did to the repository is now recorded**, which is a coding agent's actual output and was the largest hole in the trace: the commit and branch as state deltas, the changed files with their line counts as a `state_snapshot`, and a `checkpoint` carrying the commit plus a vault patch of what was uncommitted. That last one is what finally makes R2 session fork real — `runopsy replay --execute` restores the tree before re-running, so the experiment is about the original run rather than about whatever is on disk today.

**A journal can now be checked against itself.** `runopsy verify` recomputes a rolling digest written as events are appended. It is tamper *evidence*, not tamper proofing — whoever can edit a journal can delete the seal beside it — and an unsealed journal is reported as unsealed rather than as broken, because treating unknown as tampered would make the check worthless. The append and the fold happen inside one lock: without that, two parallel writers order their bytes one way in the file and another in the chain, and the recorder accuses itself.

The opt-in real-run corpus (section 17.1 layer four) now has its mechanism: `runopsy label` turns a recorded run into a JSON case carrying the trace's hashes and no payload text, and `runopsy bench --corpus DIR` scores against those instead of the synthetic suite. The corpus itself only grows by being used. Two rules are enforced rather than documented: a label needs a named human (`labelled_by`), and nothing reads what `diagnose` found — a corpus scored against the engine's own opinion would only confirm what it already believes.

The 3D view exists and keeps its designed place: optional, lazy-loaded (Three.js lives in its own chunk nobody downloads without clicking the toggle), never the default. Depth carries time, height carries severity, and inference stays visually weaker than record — propagation is a translucent arc, because a guess must not become more convincing for having been drawn in 3D.

**The web UI.** `cd packages/runopsy-ui && npm install && npm run build` writes into `runopsy-server/src/runopsy_server/static/`, which the server mounts at `/` when present. The build output is **not** in version control: it is produced at release time and bundled into the wheel, so `pip install` gives a working view without a Node toolchain, while a source checkout that never ran the build falls back to the server-rendered index. That fallback is deliberate and worth keeping — a diagnosis tool that shows nothing without a JavaScript build has made itself hardest to reach exactly when it is needed. `create_app(store, serve_ui=False)` forces the fallback, and the tests use it.

The UI reads field names this repo can rename without noticing, so `TestTheContractTheWebViewReliesOn` in `tests/test_server.py` pins them from the Python side. Observed and inferred edges arrive in separate fields and stay separate all the way to the screen: solid for `precedes`, dashed and labelled *may reach N%* for propagation.

`POST /v1/runs/{id}/replay` is the one design endpoint deliberately not built, and a test enforces its absence. Executing a replay is the only thing here that can change the world; the CLI gets its approval from a terminal, and shipping the endpoint without an equivalent would put the fail-closed gate behind a request body.

**The first real agent sessions were recorded on 31 July 2026** — live Hermes 0.19.0 runs captured through the shell hooks: two that succeeded (33 and 7 events) and one given a contradictory specification that ran 42 events and never finished. They found four things twenty synthetic cases had not, and they are the template for what real traces do to this engine:

1. The loop detector fired on the edit-verify cycle. The agent re-ran its verification command after every edit; the command never changed, the file on disk did, and an argument hash cannot see a file. Seven identical calls read as *stuck* and outranked the steps that had actually failed.
2. Fixing that by requiring outputs to be **identical** was then too strict, and the failing run proved it: twenty-five steps of writing a file and re-running the same check, whose output alternated between two answers. The engine went silent on the only thing that had really gone wrong. The rule that separates them is whether calls keep turning up results that are *new* — 0.71 and 0.80 distinct outputs per call on the healthy cycles, 0.31 on the stuck one.
3. A run that **succeeded** was described as having an "observed failure", because three patches failed and were recovered from mid-run. Real agents fail and recover constantly; synthetic traces do not.
4. **No `llm_call` events were captured at all.** Hermes 0.19.0 dispatches `post_llm_call` only through the plugin path; its shell dispatcher emits exactly five events (`agent/shell_hooks.py`). **Closed by the bundled Hermes plugin**: `runopsy adapter hermes plugin` installs it into `<hermes_home>/plugins/runopsy/` (enabling stays in the user's config — the CLI prints the `plugins.enabled` line to paste). The plugin listens to `post_api_request` — the one hook that carries token usage — and forwards it as a `post_llm_call` payload to the same `runopsy hook` subprocess the shell hooks use, so it depends on no Runopsy internals and Runopsy depends on no Hermes internals. Verified live: a session recorded 27 `llm_call` events interleaved with 28 tool calls, with real token counts, cache statistics and latency. The budget detector now has data on the primary runtime. Cost is best-effort from Hermes' own pricing tables and may be absent.

Both corrections left the benchmark at 94.4% top-1 and zero false positives. That is the finding, not a footnote: real traces did not improve the score, they changed what the score was failing to measure.

A third change was tried and **reverted after measuring it.** A loop is attributed to the repeated call's first occurrence, which on the stuck run is step 1 — long before the agent was actually stuck — so anchoring at the first repeated answer looked obviously better. It was worse on both counts: synthetic top-1 fell to 88.9%, and on the real run the loop dropped below a transient patch failure the agent had already recovered from. Ranking weights precedence deliberately, so moving an onset later demotes the finding it was meant to sharpen. Naming the moment repetition turns unproductive needs the ranker to understand a span, not a step — do not retry it as a one-line change.

5. **The Hermes mapper hashed payload text and threw the text away**, so the vault the shell adapter fills stayed empty. Every layer that needs to *read* a step degraded quietly: `--mode hybrid` on a real session withheld all twenty steps as "not in the local store" and billed for the model call regardless. The `hook` command now passes the vault through, redacting before anything lands on disk. On the same trace the semantic layer went from twenty withheld steps to a real finding for $0.0003.

6. **The most basic detector was blind on the primary runtime.** Hermes reports whether the *tool* ran, not whether the command it ran succeeded: its terminal tool returns `{"output": ..., "exit_code": N}` and reports status `ok` whenever the shell was invoked at all. A test suite failing on every attempt was recorded as a run of successful steps — twenty-one of them in one session — so the L0 failed-call detector had nothing to fire on. The mapper now reads `exit_code` out of a structured result, and only a structured one: inferring failure from prose would manufacture findings. Same trace, before and after: a handful of candidates became twenty-two, including every failing test run.

**Recording under parallelism was broken and is the one defect nobody would have reported.** Every test and every live session so far recorded one step at a time. An agent that delegates to parallel subagents does not: it fires a `runopsy hook` process per event, several within the same millisecond. Thirty-two concurrent hooks against one store lost twelve events, silently — a hook's first duty is not to break the run it observes, so it swallowed the error and exited zero, and the run looked recorded. Three separate faults, each invisible while the others were present: dedup queried the index *before* the journal append, so a locked database discarded the event before anything durable existed; opening the collector connects to the index, so contention failed the write path outright; and step numbers came from `SELECT MAX(sequence) + 1`, which two subagents in one session — what Hermes subagents actually are — took simultaneously, producing one event id for two steps and deduplicating the second away. Thirty-two of thirty-two now, pinned by `tests/test_concurrency.py`, which launches real subprocesses because none of this is visible in-process.

**Which detectors real data has actually exercised**, re-measured on 2 August 2026 after
four more live Hermes sessions and the repository capture that came with them. **Seven of
fifteen** have now produced a finding on a real trace, up from three: `structural:
tool_execution`, `behavioral:tool_loop`, `structural:incomplete_run`,
`structural:outcome_mismatch`, `behavioral:budget`, `behavioral:state_flapping` and
`structural:timeout`. Two of those are new because the trace now carries the working tree
— `state_flapping` fires on `git.head` returning to a commit the run had already left,
which is an agent undoing its own work and was invisible before. `budget` needed real
token counts, which needed the Hermes plugin to actually be found (see below).

The eight that have not fired split three ways, and the difference is the point:

- **Correct silence, verified.** `behavioral:budget` is silent with no ceiling, fires at
  5,000 against a real session that spent 21,675 tokens, and is silent again at 100,000.
  `behavioral:incomplete_handoff` stayed silent on a session that spawned two subagents
  because both handed over summaries.
- **Structurally unreachable, and worth saying so.** `behavioral:retry_storm` keys on
  `retry_of`, and **no adapter sets it while recording** — only the replay executor and
  the synthetic bench cases do. It cannot fire on any recorded run, real or otherwise,
  until an adapter learns to recognise a retry. `behavioral:stale_memory` and
  `behavioral:unsupported_claim` need `memory_op` and `claim` events, which Hermes never
  emits. Three detectors are therefore exercised only by constructed traces, and that is a
  fact about the engine's evidence, not a footnote.
- **Conditions that have not occurred.** `structural:model_call`,
  `structural:blocked_action`, `structural:missing_run_start` and
  `structural:trace_integrity` need failures these runs did not have. Two of them are
  now *harder* to reach on purpose: the sequence allocator prevents the duplicate and
  missing step numbers `trace_integrity` reports, so seeing it fire would mean something
  is wrong that this design is supposed to make impossible.


**Six real runs exist now**, and the engine behaves correctly on all of them: two clean successes produce **zero** findings, the 33-event success reports its three recovered patch failures and labels them recovered, the 42-event stuck run names the loop as its primary candidate, and two one-event runs killed by a provider error are reported as possibly interrupted at 13% confidence. The zero-false-positive invariant now has real traces behind it, not only synthetic ones.

**Every major surface has now been exercised against a real recording, not only tests.** The local API serves real runs; `replay --execute` runs a real trace in a sandbox and reports a straight re-run as *inconclusive*, never as reproduction; `--skip-onset` applies a genuine intervention and reports *not supported* with both explanations left open; the replay child run is recorded with its lineage; and the semantic layer returns a real, capped, clearly-labelled judgement. `runopsy evidence` now prints the recorded command and output rather than only digests, withheld for steps flagged as sensitive unless `--include-sensitive` is passed, matching `export`. Reading that view is what exposed defect 6 above — the exit code sitting in the output body while the step above it claimed success.

`runopsy graph` renders the run as a timeline in pure ASCII (a legacy Windows code page raises `UnicodeEncodeError` on box-drawing characters, and a diagnosis tool must not crash on the terminal it was asked to print to), with `--format dot` for Graphviz. Propagation is fetched from `infer_affects` rather than read off the graph, because normalization deliberately records no `AFFECTS` edges. `runopsy adapter hermes status` reports whether Hermes is really wired: it names an unparseable config, a missing hook, and a hook registered for a plugin-only event that will never fire — the three ways this integration fails silently.

Six sessions are still not a corpus. All of them ran one model — `openai/gpt-4o-mini`, since the available OpenRouter key 404s on other families — on small single-file tasks, and none exercised handoff, memory, subagents or budget ceilings. Every headline number still comes from constructed traces, so prefer work that records more real runs over work that adds surface.

To record more: install `hermes-agent` in its own venv, write the block that `runopsy adapter hermes` prints into the path `hermes config path` reports, then run with `RUNOPSY_HOME` set and `--accept-hooks`. Paste the generated block rather than hand-writing it: the whole `<command> <event>` string is one YAML scalar, and quoting only the path — `command: "C:/x/runopsy" hook post_tool_call` — makes Hermes discard the entire config, run with defaults and record nothing, while looking like a completely normal session.

**Keep this section true.** It is the first thing read and the easiest thing to leave stale; a wrong "not built yet" costs a future session real time. If a change ships a capability listed here as missing, move it in the same commit.

Measured onset localization, reproducible via `runopsy bench --compare` and recorded in `benchmarks/baseline-report.md`: top-1 94.4%, top-3 100%, mean step distance 0.11, zero false positives — against 22.2% for blaming the last failing step, which is what reading a log bottom-up achieves.

## Commands

```bash
uv sync                       # install; pins Python 3.12 via .python-version
uv run pytest                 # ~905 tests, ~260s; coverage gate is 85%
uv run pytest tests/test_diagnose.py::TestConfidence   # one class
uv run ruff check . && uv run ruff format .
# Every package, named explicitly: the CI matrix runs bash and PowerShell, and
# PowerShell does not expand a glob for a native command, so packages/*/src would
# reach mypy as a literal path — checking nothing while still exiting zero.
uv run mypy packages/runopsy-core/src packages/runopsy-collector/src \
            packages/runopsy-cli/src packages/runopsy-bench/src \
            packages/runopsy-replay/src packages/runopsy-adapter/src \
            packages/runopsy-semantic/src packages/runopsy-server/src \
            packages/runopsy-inspect/src packages/runopsy/src tests
uv run bandit -c pyproject.toml -r packages -q   # exits non-zero on any finding
uv run python examples/coding_failure/seed.py   # seed the demo trace
uv run python examples/multi_agent_handoff/seed.py   # a handoff that dropped the brief
uv run python examples/research_failure/seed.py      # a claim outrunning its evidence
uv run runopsy diagnose --store .runopsy-demo
uv run runopsy bench --write benchmarks/baseline-report.md
```

`tests/test_packaging.py` fails if this command, CONTRIBUTING.md's or CI's falls behind
the `packages/` directory. It exists because the CI list once named six of the eight, so
two packages would have gone unchecked while mypy still reported success — a gate that
narrows silently is worse than none, because the green tick goes on being believed.

`uv` lives at `~/.local/bin` and may not be on PATH in a fresh shell.

## Authorship and commits

The project is authored by Vahit Feryad (Independent Researcher, Istanbul; ORCID `0000-0002-3282-339X`). Commits are made as `Vahit FERYAD <vahit.feryat@gmail.com>`, set repo-locally.

**Do not add `Co-Authored-By` trailers naming an AI assistant to commit messages**, and do not reintroduce them when amending or rebasing. The repository owner is the sole listed contributor. This is a deliberate instruction that overrides any default tooling convention.

Write commit messages that explain why a change was made, not what the diff shows. `CITATION.cff` carries the academic citation metadata; keep `version` and `date-released` in step with releases.

## Language

**The product ships in English.** It targets a global open-source audience, so everything a user or contributor can see is written in English: CLI help and output, TUI and web UI copy, error and diagnosis messages, README and `docs/`, code comments, identifiers, schema field names, commit messages, issue templates, and release notes. No Turkish strings in shipped artifacts.

The design PDF is Turkish and the repository owner writes in Turkish — that applies to conversation only, never to committed content. Translate concepts from the PDF into English terminology rather than transliterating (e.g. "aday başlangıç" → *suspected onset*, "yayılım" → *propagation*, "kanıt" → *evidence*).

Keep user-facing strings out of scattered f-strings where practical, so localization stays possible later without a rewrite. Localization itself is not in MVP scope.

## Secrets and environment

Credentials live in `.env` at the repository root, which `.gitignore` excludes and which must never be committed, printed, or pasted into a command. The owner fills the values manually; code reads them from the environment and must never write, echo, or log a key. `.env.example` is committed and holds the variable names with empty values — keep it in sync whenever a new variable is introduced.

| Variable | Needed for | When |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter BYOK provider — task model, hybrid/semantic diagnosis, replay | First real provider call |
| `GITHUB_TOKEN` | GitHub Actions CI, release publishing, dependency/SBOM scans | CI and release setup |
| `HF_TOKEN` | Publishing the Runopsy-Bench dataset and demo to Hugging Face | Benchmark distribution |
| `PYPI_API_TOKEN` | Publishing the CLI to PyPI | First public release |

Runopsy must stay fully usable with **none** of these set: local models via Ollama/llama.cpp, and rules-only mode, require no key at all. Never make a missing key a hard failure at startup — degrade to the offline path and say so.

### How end users supply credentials

Runopsy is BYOK: every user brings their own provider key. No key is ever bundled, defaulted, proxied through a Runopsy-operated service, or shared between users — that is what makes "your code and traces stay on your machine" true rather than a slogan, and it keeps the project free of per-user inference cost.

`runopsy setup` onboards a key interactively and stores it in the **OS keyring** (Windows Credential Manager, macOS Keychain, Secret Service on Linux), not in a file. Resolution order, first match wins:

1. explicit `--api-key` flag (scripted and CI use)
2. process environment (`OPENROUTER_API_KEY`)
3. OS keyring entry written by `runopsy setup` — the normal path for an end user
4. `.env` in the working directory — developer convenience only, warn when used

Rules that hold everywhere: a key is never written into a trace, log, diagnosis bundle, export or crash report; the secret scanner runs over any payload before it leaves the machine; `runopsy doctor` reports only whether a credential resolved and from which source, never its value.

## Open-core boundary

This determines where code goes, not just how it is sold. Everything under `packages/` is Apache-2.0 and must work standalone, offline, forever: the CLI, core engine, collector, replay, runtime adapters, the 2D/3D UI and Runopsy-Bench.

A future commercial tier is a **separate server product** for teams — shared run history, cross-run trend and regression analysis, RBAC/SSO, audit export, central policy management, alerting and long retention. Two constraints follow:

- Nothing in `packages/` may call a Runopsy-hosted service, or degrade when one is absent. No telemetry-by-default, no feature that quietly needs an account.
- A commercial feature may add team-scale capability, but may never remove or cripple something the local product already does. Crippling the open core to sell the paid tier destroys the adoption channel the paid tier depends on.

The defensible asset is not the visualization: it is the labeled failure corpus, the calibrated onset/propagation measurements, and the replay-verified recovery data in Runopsy-Bench. Protect the benchmark's rigor accordingly.

## What Runopsy is

Runopsy ("Run + Autopsy") is a local-first, open-source coding-agent CLI with a built-in **Causal Failure Analysis (CFA)** engine. When an agent run fails, instead of dumping logs it shows the *suspected failure onset*, the evidence behind that claim, the downstream propagation chain, and a user-approved replay plan from the right checkpoint.

Positioning boundaries that matter when making design calls:

- It is an **independent product**, not a Hermes plugin. Hermes Agent is only the *first runtime adapter* and the first distribution surface.
- It is **not** a general-purpose assistant. The target user runs coding/repository/terminal tasks.
- The product value is **provable diagnosis and verifiable recovery**, not 3D visualization. The 3D view is optional; 2D is the default.

## Architecture

Pipeline: **capture → normalize → find candidates → measure impact → validate**

```
User (Runopsy CLI / Hermes TUI)
  └─ Agent Runtime (Hermes v1 first; others later) ── Model Provider (local / OpenRouter)
       └─ Runtime Adapter (hooks + policy)
            └─ Trace Normalizer (OpenInference-compatible)
                 └─ Local store (DuckDB + JSONL)
                      └─ Runopsy CFA Engine (rules + graph + eval)
                           ├─ Terminal diagnosis (runopsy diagnose)
                           ├─ Replay engine (fork + rollback)
                           ├─ 2D/3D UI (timeline + failure map)
                           └─ Runopsy-Bench (measurement + regression)
```

Component responsibilities:

| Component | Responsibility |
|---|---|
| Runtime adapter (Hermes) | Hook capture, runtime metadata, policy, snapshots, slash commands |
| Trace Normalizer | Runtime events → OpenInference-compatible typed graph (`TraceNode` / `TraceEdge`) |
| Local Collector | Append-only event ingest with sequence and integrity checks |
| Detector Registry | Loop, exception, tool, state, handoff, evidence, budget detectors → `FailureSignal[]` |
| Causal Ranker | Ranks failure-onset candidates and downstream impact → `Candidate[]` |
| Semantic Evaluator | LLM-based claim/evidence and handoff checks **on selected spans only** |
| Replay Orchestrator | Checkpoint, rollback, fork, alternative model/policy execution |
| TUI / Web UI | Live status, timeline, graph, evidence, original-vs-replay comparison |
| Runopsy-Bench | Labeled trajectories, fault injection, regression measurement |

### Planned monorepo layout

```
packages/
  runopsy-core/       # graph, schema, detectors, ranking  (framework-agnostic)
  adapter-hermes/     # Hermes plugin + event mapping
  runopsy-collector/  # local ingest and persistence
  runopsy-replay/     # checkpoint/fork orchestration
  runopsy-cli/        # Typer + Textual
  runopsy-server/     # FastAPI
  runopsy-ui/         # React 2D/3D
  runopsy-inspect/    # Inspect AI benchmark adapter
benchmarks/{synthetic,fault_injection,labeled_runs}/
examples/{coding_failure,research_failure,multi_agent_handoff}/
docs/  scripts/  tests/  pyproject.toml  uv.lock  LICENSE
```

`runopsy-core` must never import runtime-specific code — it accepts only the normalized trace graph.

## Non-negotiable design principles

These come from sections 5.1, 16 and 23 of the design document. Violating them changes what the product is.

1. **Do not fork Hermes.** Integrate through its plugin/hook system and programmatic API. Missing hooks are proposed upstream first; forking is the last resort.
2. **Local-first.** Traces, state, and artifacts stay on the user's machine by default. Nothing goes to a provider without explicit opt-in and payload minimization (hashes and excerpts, not whole files).
3. **Deterministic-first.** L0–L2 analysis (structural, behavioral, graph impact) must run with **zero LLM tokens**, always on. Semantic analysis is optional, scoped to suspicious spans, and hard-capped by budget config.
4. **Evidence-first, calibrated language.** Every diagnosis links back to source events, state diffs, and evaluator signals. "Definitive root cause" may only be stated when backed by replay or human verification — temporal ordering plus correlation is *not* causal proof.
5. **Human in control.** Risky interventions and replays require explicit approval. Replays run in a fork/worktree/sandbox; external side effects (email, payment, delete, publish, remote mutation) are blocked by default. The original run is never mutated — a replay is a new `run_id` with `parent_run_id`.
6. **Separate actor from diagnostician.** The model doing the task and the model diagnosing it are configured and budgeted separately; a model self-grading is not treated as trustworthy on its own.

Default safe-mode config (section 16.1):

```yaml
privacy:  { storage: local, send_raw_trace_to_provider: false, redact_secrets: true, redact_pii: true, retain_raw_days: 7 }
replay:   { require_confirmation: true, sandbox: worktree, external_side_effects: block }
diagnosis:{ causal_language: calibrated, allow_definitive_root_cause_without_replay: false }
analysis: { mode: deterministic, llm_on: suspicious, max_diagnostic_calls: 2,
            max_diagnostic_input_tokens: 6000, max_replay_runs: 1, max_cost_usd: 0.10,
            cache_by_trace_hash: true }
```

## Tech stack (already chosen — do not substitute without reason)

**Backend/CLI:** Python 3.12 · Typer (CLI) · Textual + Rich (TUI) · Pydantic v2 (typed trace/config) · OpenTelemetry SDK + OpenInference semconv · DuckDB (local analytics store) · NetworkX (graph analysis, MVP) · FastAPI + Uvicorn (local API) · httpx · orjson · structlog · psutil (hardware discovery)

**Web:** React + TypeScript · Vite · React Flow/XYFlow (2D DAG) · Three.js + React Three Fiber (optional 3D) · TanStack Query · Zustand

**Quality/packaging:** pytest + pytest-asyncio · Hypothesis (property-based graph/schema tests) · coverage.py · Ruff (lint + format) · mypy · Bandit + Semgrep · uv + hatchling · pipx / uv tool (install) · Docker (optional sandbox) · GitHub Actions

**Models:** Hermes Agent (required runtime for MVP) · Ollama / llama.cpp (optional local) · OpenRouter (optional BYOK) · Inspect AI (benchmark harness) · Arize Phoenix (optional)

**Licensing:** Apache-2.0 for Runopsy core; preserve Hermes MIT notices; run SBOM + license scan before any release.

## Domain vocabulary

**Node types:** `Run`, `Agent`, `Turn`, `LLMCall`, `ToolCall`, `StateSnapshot`, `MemoryOp`, `Claim`, `Evidence`, `Artifact`, `Checkpoint`, `FailureSignal`, `Diagnosis`, `ReplayRun`

**Edge types:** `PRECEDES`, `DEPENDS_ON`, `PRODUCED`, `CONSUMED`, `DERIVED_FROM`, `CONTRADICTS`, `VALIDATES`, `AFFECTS`, `FORKED_FROM`

**Analysis layers:** L0 structural (sequence, schema, exit code, exception, timeout) · L1 behavioral (loop, retry storm, plan divergence, state invariants) · L2 graph impact (precedence, reachability, centrality) · L3 semantic (claim-evidence, handoff completeness — *optional, token-spending*) · L4 validation (counterfactual replay, human label)

**Diagnosis statuses** (distinct, never collapsed in output): `observed failure` · `suspected onset` · `correlated cause` · `replay-supported` · `human-verified` · `unknown`

**Replay levels:** R0 explain-only · R1 turn rollback · R2 session fork *(these three are MVP)* · R3 guided replay · R4 step replay · R5 automated recovery *(research)*

**Failure taxonomy** (section 9): goal/input, planning, retrieval, tool selection, tool arguments, tool execution, state, memory, handoff, reasoning, validation, control flow, budget, safety, outcome.

## Interfaces

Implemented:

```bash
runopsy demo                                             # the worked example, no setup
runopsy run "TASK"                                       # drive an agent, then diagnose it
runopsy record -s CMD ...                                # wrap any pipeline
runopsy runs
runopsy diagnose [RUN|latest] [--json] [--fail-on-finding] [--mode hybrid] [--budget-usd U]
runopsy evidence [RUN|latest] --step N [--include-sensitive]
runopsy replay  [RUN|latest] --from-step N [--model M]   # plans; --execute tests it
       [--execute] [--skip-onset | --substitute CMD]     # one intervention, sandbox copy
runopsy graph   [RUN|latest] [--format text|dot] [-o FILE]
runopsy export  [RUN|latest] [-o FILE] [--include-sensitive] [--otlp]
runopsy ui                                               # loopback only
runopsy verify [RUN|latest] [--all]                       # was this trace edited after recording?
runopsy prune [--apply]                                  # never expires anything on its own
runopsy label  [RUN|latest] --onset N --by NAME [--healthy] [--category C]
runopsy bench [--compare] [--corpus DIR] [--inject [--store DIR]] [--perf] [--write PATH]
runopsy setup                                            # key to the OS keyring
runopsy doctor
runopsy config [--init]
runopsy adapter hermes [config|status]                   # paste, or check it took
runopsy hook                                             # called by Hermes, not by hand
```

`runopsy run` drives the runtime through its own documented command line — no Hermes module is imported, nothing is patched, and the store is passed through `RUNOPSY_HOME` rather than by rewriting the user config. It checks afterwards whether anything was actually recorded, because "the agent finished and the trace is empty" is a real state that otherwise looks like success.

Conventions worth preserving when adding commands: every command that reads or writes recorded runs takes `--store` (`setup` and `config` do not, and should not — they touch the keyring and `runopsy.toml`. `bench` takes one because `--inject --store` breaks a *recorded* run rather than a generated one, which is the nearest thing to a labelled real corpus that needs no human judgement); `latest` resolves to the most recently started run; findings never fail the command unless CI opts in; anything that could leak is redacted by default.

Anything the output tells a user to run must actually run. `tests/test_real_run.py` executes the replay command that `diagnose` prints, because for two months it printed a `--dry-run` flag that does not exist — asserting on the text of a hint is not the same as checking the hint works.

Hermes slash commands: `/runopsy status|diagnose|evidence <n>|graph|replay <n>|mode offline|budget <usd>`

Local API, loopback only. Implemented: `GET /v1/health` · `POST /v1/events` · `GET /v1/runs` · `GET /v1/runs/{id}` · `GET /v1/runs/{id}/graph` · `POST /v1/runs/{id}/diagnose` · `GET /v1/diagnoses/{id}` · `POST /v1/runs/{id}/replay/plan` · `GET /v1/runs/{id}/report` · `GET /v1/runs/{id}/stream` (SSE) · `POST /v1/export`.

Designed but not implemented: `POST /v1/runs/{id}/replay`, deliberately — executing over HTTP needs an approval path the CLI gets from the terminal, and shipping it without one would put the fail-closed gate behind a request body. A test enforces its absence.

Storage split: structured events → DuckDB · raw event stream → append-only JSONL · artifacts → content-addressed local folder (SHA-256, size limit, secret scan) · graph cache → Parquet/DuckDB · diagnoses → JSON.

## MVP scope

**In:** standalone Runopsy CLI running locally · Hermes runtime adapter for coding tasks · trace/state capture and deterministic detectors · candidate onset, evidence and propagation view · approved checkpoint/session-fork replay · local model, OpenRouter BYOK and rules-only modes · Runopsy-Bench with a fault-injected demo set

**Out:** general-purpose chat assistant · every agent framework on day one · guaranteed exact root cause on every run · unattended repo mutation or recovery · 3D view as the default or main product · enterprise SaaS platform · access to or storage of hidden chain-of-thought

**MVP acceptance criteria (section 18.1)** — in offline mode zero external model calls; at least eight deterministic detectors working end to end; diagnosis JSON carrying evidence, confidence and affected nodes per candidate; dry-run plans for rollback and fork; risky external side-effect replay blocked by default; 2D timeline and causal graph sharing the same trace node IDs; reproducible benchmark report; one-command local demo.

**First sprint (section 24): complete**, including item 1 — the Hermes adapter now has a live session behind it, not just the documented protocol. See *Project status* for what that session immediately broke. Every headline number still comes from constructed traces, so accumulating real runs remains worth more than adding surface.

Priority rule: event capture and schema correctness first, then the deterministic engine, then evidence/2D UX, then replay and semantic evaluation, and 3D/cloud last.

## Invariants a change must not break

These are encoded in code and tests, not conventions. If one starts failing, the fix is the change, not the test.

- A candidate may claim `replay_supported` only with a replay behind it, `human_verified` only with a named verifier. Confidence is capped below certainty for everything else.
- No default detector may report a deterministic layer while calling a model. L0–L2 spend zero tokens.
- Normalization emits no `AFFECTS` edges; propagation is inference and belongs to the impact layer, labelled and confidence-weighted.
- Nothing may affect the past: propagation only reaches steps that ran later.
- No output path asserts causation for an unvalidated finding (`runopsy_cli.language.asserts_causation` guards this).
- The side-effect gate fails closed: an unrecognised tool needs human approval.
- A healthy run produces no finding. The false-positive rate is exactly zero, not approximately.
- The JSONL journal is authoritative; the DuckDB index is rebuildable from it. Recording writes the journal **first** and survives an index failure, and reading reconciles the index against the journals rather than trusting it. Neither half is optional: DuckDB admits a single writing process, so an adapter that spawns a process per event will lose index writes as a matter of course, and an invariant nothing acts on is a comment.
- A step number is an identity, not a label — an adapter builds the event id out of it — so allocation goes through `SequenceAllocator` under a file lock. Two events sharing a number are one event to every layer downstream, and the second is deduplicated out of the trace without anything reporting a loss.
- Diagnosis is a pure function of the trace — no clock, no network, no model — so the same run always yields the same bundle.
- Recording observes; it does not intervene. The store excludes itself from the repository it sits in, because an agent's own `git add -A` swept it into a commit and then failed on the open index — Runopsy changing the outcome of the run it was watching. Checkpoint patches go to Runopsy's vault, never into the user's object store.
- A journal is sealed as it is written, and the seal is checked rather than assumed. `verify` says intact, broken or **unsealed**; the third is not the second. Sealing must never call a correct journal modified, which is why the append and the digest happen under one lock.
- The store's version stamp is written once, at creation, and compared afterwards. Upserting it meant reading a store relabelled it — destroying the only evidence that its history predated a format change. A store from a newer build is refused rather than written to in a shape it cannot describe.
- Anything shown to a user that is not `intact` must distinguish "we know it is wrong" from "we cannot tell". Collapsing the two is how a check becomes noise and then gets ignored.
