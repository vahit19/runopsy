# Runopsy

**Find where an AI agent run started going wrong — not just where it stopped.**

[![CI](https://github.com/vahit19/runopsy/actions/workflows/ci.yml/badge.svg)](https://github.com/vahit19/runopsy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/runopsy)](https://pypi.org/project/runopsy/)
[![Python](https://img.shields.io/pypi/pyversions/runopsy)](https://pypi.org/project/runopsy/)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](https://github.com/vahit19/runopsy/blob/main/LICENSE)

```bash
pip install runopsy && runopsy demo
```

That runs a worked example end to end: no agent, no API key, no configuration.

---

## The problem

When an agent run fails, the last error is rarely the problem. A config written wrongly
at step 9 surfaces as a failing test at step 14, and reading the log bottom-up sends you
to fix the test.

Runopsy records agent runs, localizes the step where things actually broke, shows the
evidence behind that claim, and can re-run the trace with one thing changed to test it.

It runs locally, spends no tokens for its core analysis, and never claims a cause it has
not validated.

## What it looks like

```
Run demo_run — fix the failing integration test in the payments service
demo · 16 events · failure

Observed failure  (what the run visibly got wrong)
  step 14 pytest
  tool 'pytest' failed with exit code 1

Suspected onset  (where it may have started going wrong, unverified)
  step 9 write_config
  tool 'write_config' failed with exit code 1
  47% confidence, unverified
  may have affected step 10 edit_file, step 11 restart_service, and 3 more
  evidence: runopsy evidence demo_run --step 9

  No cause has been confirmed. To test this candidate, replay from it:
    runopsy replay demo_run --from-step 9
```

## Quick start

```bash
pip install runopsy          # or: uv tool install runopsy / pipx install runopsy

runopsy demo                 # a worked example, no setup
runopsy record -s "make" -s "pytest"    # wrap any pipeline you already run
runopsy diagnose latest      # where it started going wrong
runopsy evidence latest --step 9        # the command, the output, why it was flagged
runopsy ui                   # timeline and failure map in a browser, loopback only
```

Driving a coding agent and diagnosing it, in two commands:

```bash
pip install 'runopsy[hermes]'   # Runopsy plus the supported agent
runopsy init                    # wires the agent up, then checks that it took

runopsy run "fix the failing test"
```

Quote the extra: square brackets are glob characters in zsh, the default shell on macOS.

`runopsy init` writes the hook configuration, installs the plugin that carries token
usage, and then verifies the runtime really is wired — a half-configured setup records
nothing while every session looks completely normal. It backs up what was there, refuses
a config it cannot parse, and leaves your comments and formatting alone. If you would
rather do it by hand, `runopsy adapter hermes` still prints the block to paste.

The agent brings its own model key, in its own config; Runopsy never sees it. Runopsy
itself needs no key at all — see *Keys* below.

## What it is, and is not

**It is not a coding assistant and does not replace the one you use.** Runopsy has no
chat, writes no code and makes no suggestions. It attaches to whatever already runs your
work — an agent, a CI pipeline, a Makefile — records what happened, and tells you where
it started going wrong.

| you already have | Runopsy adds |
| --- | --- |
| an agent (Hermes today) | `runopsy run "task"` drives it and diagnoses the session |
| a pipeline or test suite | `runopsy record -s "make" -s "pytest"` wraps it |
| Inspect AI eval logs | `runopsy-inspect import` reads them |
| nothing yet | `runopsy demo`, in one command |

There is no model to pick and no key to enter for the core product: deterministic
diagnosis spends zero tokens and makes zero network calls. A provider key buys exactly one
optional thing — `--mode hybrid`, which asks a model about the few steps already found
suspicious.

## Does it work? Four measurements, including the ones that go badly

| what was measured | result |
| --- | ---: |
| 20 labelled traces, onset top-1 (`runopsy bench --compare`) | **94.4%** |
| the same, versus blaming the last failing step | 22.2% |
| faults injected into a **real recorded run** (`--inject --store`) | **100%** |
| **TRAIL** — expert-labelled SWE-Bench agent traces (`--trail`) | **0.0%** |
| Who&When — expert-labelled multi-agent traces (`--corpus`) | **0.0%** |

The last two are published because they say where this does *not* work, and that is worth
more to you than a single flattering number.

Runopsy localizes onsets that **were themselves failures** — a step that errored before
the visible symptom did. On TRAIL, not one of the 30 annotated onsets carries an error
status of any kind: they are formatting mistakes, instruction non-compliance, a wrong
assumption about a file path. The deterministic layers read exit codes and tool statuses,
so they are blind to those by construction. `--mode hybrid` exists for that case.

Zero false positives on healthy runs, exactly rather than approximately: a spurious
finding is what gets a diagnosis tool switched off.

## What it will not do

- claim a cause it has not validated — a finding stays *suspected onset* until a replay
  or a named human says otherwise
- send anything anywhere without `--mode hybrid`
- execute a replay outside a disposable sandbox, or perform a blocked external side effect
- report a finding on a healthy run

## Commands

`demo` · `run` · `record` · `runs` · `diagnose` · `evidence` · `replay` · `graph` ·
`export` · `ui` · `verify` · `label` · `bench` · `prune` · `doctor` · `setup` · `config` ·
`adapter`

`runopsy --help` lists them all; `runopsy` on its own tells you where this machine stands
and what to type next.

## Packages

`pip install runopsy` gives you everything. If you are writing code against it, depend on
the piece you need:

| package | what it is |
| --- | --- |
| [`runopsy-core`](https://pypi.org/project/runopsy-core/) | schema, normalizer, 15 detectors, ranking — framework-agnostic, needs only Pydantic |
| [`runopsy-collector`](https://pypi.org/project/runopsy-collector/) | JSONL journals, DuckDB index, payload vault, retention |
| [`runopsy-replay`](https://pypi.org/project/runopsy-replay/) | checkpoint restore and counterfactual execution behind a fail-closed gate |
| [`runopsy-adapter`](https://pypi.org/project/runopsy-adapter/) | shell and Hermes runtime adapters |
| [`runopsy-bench`](https://pypi.org/project/runopsy-bench/) | labelled cases, metrics, fault injection, external benchmarks |
| [`runopsy-semantic`](https://pypi.org/project/runopsy-semantic/) | the optional paid layer, budget-capped |
| [`runopsy-server`](https://pypi.org/project/runopsy-server/) | local API and the bundled web view |
| [`runopsy-inspect`](https://pypi.org/project/runopsy-inspect/) | reads Inspect AI eval logs |

```bash
pip install "runopsy[inspect]"   # adds the Inspect AI reader
```

## Privacy

Traces, state and artifacts stay on your machine. Command text is kept in a local vault
with secrets redacted before anything is written; the trace itself stores hashes only.
Journals are sealed as they are written, so `runopsy verify` can tell you a trace is
byte-for-byte the one that was recorded.

## Links

- **Source, documentation and issues**: https://github.com/vahit19/runopsy
- **Changelog**: https://github.com/vahit19/runopsy/blob/main/CHANGELOG.md
- **Licence**: Apache-2.0

Built by Vahit Feryad (ORCID [0000-0002-3282-339X](https://orcid.org/0000-0002-3282-339X)).
