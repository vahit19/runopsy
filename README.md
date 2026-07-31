# Runopsy

**Find where an AI agent run started going wrong — not just where it stopped.**

When an agent fails, the last error is rarely the problem. A config written wrongly at
step 9 surfaces as a failing test at step 14, and reading the log bottom-up sends you to
fix the test. Runopsy records agent runs, localizes the step where things actually broke,
shows the evidence behind that claim, and plans a controlled replay to test it.

It runs locally, spends no tokens for its core analysis, and never claims a cause it has
not validated.

```
Observed failure  (what the run visibly got wrong)
  step 14 pytest
  tool 'pytest' failed with exit code 1

Suspected onset  (where it may have started going wrong, unverified)
  step 9 write_config
  tool 'write_config' failed with exit code 1
  47% confidence, unverified
  may have affected step 10, step 11, step 12 and 2 more

  No cause has been confirmed. To test this candidate, replay from it:
    runopsy replay run_0042 --from-step 9
```

## Does it actually work?

Measured on 20 labelled traces with declared ground truth, reproducible offline with
`runopsy bench --compare`:

| strategy | top-1 | top-3 | mean step distance |
| --- | ---: | ---: | ---: |
| no diagnosis | 0.0% | 0.0% | — |
| blame the last failing step *(what reading a log achieves)* | 22.2% | 44.4% | 3.50 |
| blame the earliest failing step | 50.0% | 50.0% | 1.31 |
| **Runopsy deterministic engine** | **94.4%** | **100.0%** | **0.11** |

Zero false positives on healthy runs — a spurious finding is what gets a diagnosis tool
switched off, so that threshold is exact rather than approximate.

**What this does not show.** These are synthetic single-fault traces. They establish that
the ranking behaves as designed; they do not establish that it saves anyone time on real
work. That needs fault injection on real workloads and a measured reduction in
time-to-diagnosis. The full report, including the cases the engine still misses and the
failures it cannot see at all, is in [`benchmarks/baseline-report.md`](benchmarks/baseline-report.md).

How much that gap matters is not a guess. The first real agent session recorded — a live
Hermes run fixing an ordinary bug — broke the engine in a way none of the twenty cases
had: the agent re-ran its verification command after every edit, and the loop detector
read seven identical calls as being stuck, outranking the steps that had actually failed.
Synthetic traces only ever repeat a call when something *is* stuck, so "same arguments"
and "making no progress" were never distinguishable in them. The obvious fix — demand
identical outputs — then went silent on a run that spent twenty-five steps cycling
between two answers, so the rule became whether calls keep turning up results that are
*new*. Both corrections, and the regressions that pin them, are in
`tests/test_real_run.py`.

Across the six real runs recorded so far the engine now behaves: the two clean successes
produce no findings at all, the stuck run names its loop, and the run that failed and
recovered says so in those words. Neither correction moved the table above by a tenth of
a point, which is the honest summary of what that table measures — the ranking, not the
product.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/vahit19/runopsy && cd runopsy
uv sync
```

## Try it in one minute

```bash
uv run python examples/coding_failure/seed.py     # a demo trace
uv run runopsy diagnose --store .runopsy-demo
uv run runopsy evidence --step 9 --store .runopsy-demo
```

## Record your own runs

Wrap any pipeline. Nothing else is needed — no agent framework, no API key:

```bash
uv run runopsy record -s "make" -s "ruff check ." -s "pytest"
uv run runopsy diagnose latest
```

Or connect [Hermes Agent](https://hermes-agent.nousresearch.com/) and diagnose real agent
sessions:

```bash
uv run runopsy adapter hermes           # prints the config to paste into its cli-config.yaml
uv run runopsy adapter hermes status    # check it took
uv run runopsy run "make the failing test pass"
```

That last command starts the agent, records what it does through the hooks, and
diagnoses the result. It drives the runtime through its own command line — nothing is
forked, imported or patched — and tells you plainly if the session recorded nothing.

## Prove it, don't just rank it

A suspicion becomes a supported cause only when an experiment says so:

```bash
uv run runopsy replay latest --from-step 9 --execute --substitute "make config ENV=prod"
```

The replayable steps run in a **disposable copy** of your project — never the working
tree — with external and destructive steps excluded. If the downstream failures disappear
when the onset is changed, `runopsy diagnose` upgrades that candidate to *cause, supported
by replay*. If they do not, it says so. A straight re-run without an intervention is
reported as reproduction, never as causation.

## Commands

| command | what it does |
| --- | --- |
| `runopsy run "TASK"` | drive an agent and diagnose the run, in one command |
| `runopsy record -s CMD` | run commands and record them as a trace |
| `runopsy runs` | list recorded runs |
| `runopsy diagnose [RUN]` | find the onset, the evidence and the propagation |
| `runopsy diagnose --mode hybrid` | additionally ask a model about the suspicious steps |
| `runopsy evidence --step N` | the command, the output, and why the step was flagged |
| `runopsy replay --from-step N` | plan a controlled re-run; `--execute` tests it |
| `runopsy export [-o FILE]` | a self-contained HTML report |
| `runopsy export --otlp` | the same run as OpenInference-shaped OTLP JSON |
| `runopsy graph` | the run as a timeline; `--format dot` for Graphviz |
| `runopsy adapter hermes status` | check the runtime is really wired and recording |
| `runopsy adapter hermes plugin` | install the plugin that records model calls and tokens |
| `runopsy-inspect import LOG` | read an Inspect AI eval log into a trace |
| `runopsy prune` | delete traces past the retention window |
| `runopsy ui` | the React timeline and failure map (optional 3D), loopback only |
| `runopsy bench [--compare\|--inject]` | score the engine against labelled traces |
| `runopsy config --init` | write a commented `runopsy.toml` |
| `runopsy setup` | store a provider key in the OS keyring |
| `runopsy doctor` | what is configured, without revealing any secret |

## The optional paid layer

Everything above is free and offline. `--mode hybrid` asks a model about the few steps
the deterministic engine already found suspicious, which is the only way to reach a step
that *succeeded* while doing the wrong thing:

```bash
uv run runopsy setup                       # key goes to the OS keyring, not a file
uv run runopsy diagnose --mode hybrid --budget-usd 0.05
```

A model finding is capped below the deterministic engine's own confidence ceiling and
labelled *model judgement, unverified*. It can add evidence; it can never produce a
verdict.

**A note on the default budget.** `max_calls` defaults to 2, which is enough to
corroborate a candidate the engine already found. Reaching a silent step several places
upstream needs more — in a live test it took four calls, at a total cost of $0.0004. The
default is deliberately low because the ceiling is your money; raise it in
`runopsy.toml` under `[semantic]` when you want the deeper search.

## How it works

```
runtime adapter → normalized trace graph → deterministic detectors
                                        → causal ranking → diagnosis
                                        → replay planning
```

Analysis runs in layers. **L0 structural** and **L1 behavioral** — failed calls, timeouts,
retry storms, argument-identical loops, oscillating state, stale memory, incomplete
handoffs, budget ceilings — are pure functions of the trace: no model, no network, no
clock, and therefore no tokens. **L2 graph impact** infers what a step may have broken
downstream, with confidence decaying by distance. **L3 semantic** and **L4 validation**
are opt-in and cost money; only they can promote a suspicion to a cause.

## Design commitments

These are enforced by tests, not by convention:

- **Local-first.** Traces stay on your machine. Prompts, arguments and file contents are
  referenced by hash and never stored, so a trace can be shared without carrying your
  source code with it.
- **Deterministic-first.** Core analysis spends zero tokens and works fully offline. No
  provider key is required for anything in this table.
- **Calibrated language.** A cause is stated as established only when a counterfactual
  replay or a person confirms it. Everything else is labelled a suspicion and carries its
  confidence. No output path asserts causation for an unvalidated finding.
- **Bring your own key.** No credential is bundled, defaulted, or proxied through any
  service we operate.
- **Replay asks first, and runs in a copy.** Execution requires an explicit `--execute`
  and a confirmation, happens in a disposable sandbox rather than your working tree, and
  excludes external and destructive steps outright. Unrecognised tools need approval —
  the gate fails closed.
- **Observing never breaks the observed.** A runtime hook that cannot record reports the
  reason on stderr and exits cleanly.
- **Nothing expires on its own.** Retention deletes only when you run `runopsy prune`,
  only with `--apply`, and never a run whose age it cannot determine.

## Status

Schema, collector, 15 detectors, ranking, causal replay with counterfactual
validation, an optional semantic layer, a local API, fault injection, and a Hermes
adapter verified against hermes-agent 0.19.0.

All ten sprint items are done, plus replay execution, the semantic layer, fault
injection, the local API and keyring onboarding.

Traces export to OpenInference-shaped OTLP, so a diagnosed run opens in Phoenix,
Langfuse or anything else that speaks it — with the localized onset travelling along as
span attributes. Import is deliberately not attempted: reading somebody else's spans
means guessing what their attributes mean, and a wrong guess produces a confident
diagnosis of a trace we misunderstood.

Measured at scale with `runopsy bench --perf`: 100,000 events ingest in about three
seconds and every stage stays roughly linear.

Not built yet: the opt-in real-run corpus (section 17.1 layer four), and publication to
PyPI — see [RELEASING.md](RELEASING.md) for what publishing still needs.

## The web view

```bash
cd packages/runopsy-ui && npm install && npm run build
uv run runopsy ui            # then open the printed loopback address
```

The build writes into the server package, which mounts it at `/`. It is not committed:
releases bundle it into the wheel, and a source checkout that skips the build gets a
plain server-rendered index instead. A diagnosis tool should not go dark because nobody
ran a JavaScript build.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

If you use Runopsy in academic work, please cite it — see [CITATION.cff](CITATION.cff).
