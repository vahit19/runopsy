# Handoff

How to pick this project up on another machine, and what is true about it today.

`CLAUDE.md` is the design authority — what was decided and why. This file is the
operational companion: where things live, what exists *outside* the repository, and what
was learned the hard way. Read both.

**Last updated: 3 August 2026, at `v0.1.8`.**

---

## 1. What Runopsy is, in one paragraph

A local-first CLI that records agent runs and finds where one *started* going wrong,
rather than where it stopped. It attaches to something that already runs your work — an
agent, a CI pipeline, a Makefile — and produces a ranked onset with the evidence behind
it, plus a replay that can test the claim in a sandbox. It is not a coding assistant and
writes no code. Deterministic analysis spends zero tokens; a provider key buys exactly one
optional thing, `--mode hybrid`.

## 2. Where everything is

| what | where |
| --- | --- |
| Source | https://github.com/vahit19/runopsy — `main`, tagged `v0.1.8` |
| PyPI | https://pypi.org/project/runopsy/ — 10 distributions, all at 0.1.8 |
| Hugging Face | https://huggingface.co/datasets/renderfy/runopsy-bench |
| HF account | `renderfy` |
| Local repo | `C:\Users\vahit.feryad\Documents\GenAI\misc\uygulamalar\runopsy_codebase` |

90 commits, working tree clean, nothing unpushed.

### State that lives outside the repository

These are **not** in git and will not exist on a new machine. Nothing depends on them —
they are evidence, not inputs — but the numbers in the README were measured with them.

| path | what it is |
| --- | --- |
| `~/runopsy-realruns/store` | the recorded real runs (Hermes sessions + shell runs) |
| `~/runopsy-realruns/proj` | the scratch project those runs worked in |
| `~/hermes-venv` | hermes-agent in its own virtualenv |
| `.runopsy-demo`, `.runopsy-live`, … | assorted demo/scratch stores in the repo root |

To recreate: see §4.

## 3. Setting up a new machine

```bash
git clone https://github.com/vahit19/runopsy
cd runopsy
uv sync                     # pins Python 3.12 via .python-version
uv run pytest               # ~916 tests, ~5 min; coverage gate is 85%
uv run runopsy demo         # confirm it works
```

`uv` lives at `~/.local/bin` and may not be on PATH in a fresh shell.

### Credentials

Copy `.env.example` to `.env` and fill in what you need. **Never commit `.env`.**
Everything works with none of them set.

| variable | needed for |
| --- | --- |
| `OPENROUTER_API_KEY` | live agent runs, `--mode hybrid`, replay with a model |
| `PYPI_API_TOKEN` | publishing releases |
| `HF_TOKEN` | reading TRAIL, publishing the dataset |
| `GITHUB_TOKEN` | CI and release automation |

The `HF_TOKEN` on the old machine belongs to account `renderfy` and is fine-grained.
A fine-grained token needs *"Read access to contents of all public gated repos"* ticked
before TRAIL will open, **and** the terms accepted once at
https://huggingface.co/datasets/PatronusAI/TRAIL while logged in as the same account.
Those are two separate things — a valid token alone returns 403.

### The web UI

The built assets are **not** in version control. A source checkout falls back to a
server-rendered index, which is deliberate. To build them:

```bash
cd packages/runopsy-ui && npm install && npm run build
```

Screenshots for the README:

```bash
runopsy ui --store <a store> --port 8971      # in one terminal
cd packages/runopsy-ui && npm run screenshots -- http://127.0.0.1:8971
uv run python scripts/render_demo.py          # the terminal images
```

## 4. Recording real runs again

```bash
uv venv ~/hermes-venv --python 3.12
VIRTUAL_ENV=~/hermes-venv uv pip install hermes-agent pytest

uv run runopsy adapter hermes            # prints the config block
# paste it into the file `hermes config path` reports
uv run runopsy adapter hermes plugin     # installs the token-capturing plugin
# add `plugins:\n  enabled:\n    - runopsy` to the same config

export PATH=~/hermes-venv/Scripts:$PATH
export RUNOPSY_HOME=~/runopsy-realruns/store
runopsy run "fix the failing test" --model openai/gpt-4o-mini
```

**Paste the generated block; do not hand-write it.** The whole `<command> <event>` string
is one YAML scalar. Quoting only the path makes Hermes discard the entire config, run with
defaults, and record nothing — while looking like a completely normal session.

Cost so far, in total, across every real run: about **$0.01**. It is cheap.

## 5. Where the numbers come from

| measurement | command | result |
| --- | --- | ---: |
| 20 synthetic labelled traces | `runopsy bench --compare` | 94.4% top-1, 0% FP |
| faults injected into a real recorded run | `runopsy bench --inject --store DIR` | 100% |
| TRAIL (expert-labelled SWE-Bench traces) | `runopsy bench --trail DIR` | 0.0% |
| Who&When (expert-labelled multi-agent) | import + `--corpus` | 0.0% |

The last two are the important ones and they are published deliberately. Runopsy
localizes onsets that **were themselves failures**. On TRAIL, not one of the 30 annotated
onsets carries an error status of any kind — they are formatting mistakes, instruction
non-compliance, a wrong assumption about a file path. The deterministic layers read exit
codes, so they are blind to those by construction.

Seven of the fifteen detectors have fired on a real recorded run. Three *cannot* fire on
one at all: `retry_storm` keys on a field no adapter sets while recording, and
`stale_memory` / `unsupported_claim` need event kinds Hermes never emits.

## 6. What is deliberately not built

- **`POST /v1/runs/{id}/replay`** — executing a replay is the only thing here that can
  change the world, and the CLI gets its approval from a terminal. A test enforces the
  endpoint's absence.
- **A human-labelled corpus of our own.** `runopsy label` exists; the corpus is empty.
  Filling it means a person reading real failures and saying where each began. Doing that
  from inside the project would make the corpus grade the engine against its own opinion.
- **Anything that phones home.** Nothing in `packages/` may call a Runopsy-hosted service
  or degrade without one.

## 7. Things that cost time — do not rediscover these

**Recording under parallelism was silently lossy.** Three separate faults, each invisible
while the others were present: dedup queried the index before the journal append; opening
the collector connects to the index; and step numbers came from `SELECT MAX(sequence)+1`,
which two subagents took simultaneously. A step number is an *identity* — the adapter
builds the event id from it — so a collision deleted history. Pinned by
`tests/test_concurrency.py`, which spawns real subprocesses because none of it is visible
in-process.

**The Hermes plugin resolved `runopsy` through PATH**, and Hermes lives in its own
virtualenv, so it found nothing and recorded nothing. A trace with tool calls and no model
calls looks exactly like a runtime that does not report them. The installer writes the
absolute path now.

**Hermes reports whether the *tool* ran, not whether the command succeeded.** Its terminal
tool returns `{"output": …, "exit_code": N}` with status `ok`. Twenty-one failing test
runs were recorded as successes until the mapper started reading `exit_code` out of a
*structured* result — and only a structured one, because inferring failure from prose
manufactures findings.

**TRAIL's spans are a tree, not a list.** Two files join by `trace_id`; the trace file has
one root span with `child_spans` beneath. A reader taking the top level at face value
finds one span and resolves none of the annotations — a silent zero that looks exactly
like a real score.

**Windows specifics.** `msvcrt.locking(LK_LOCK)` gives up after ~10 seconds with no way to
wait longer, which on a loaded machine turned a busy moment into a lost event; the lock
now polls non-blocking with backoff. A legacy console code page raises `UnicodeEncodeError`
on box-drawing characters, so `graph` is pure ASCII. PowerShell does not expand globs for
native commands, so the mypy package list is written out in full — and
`tests/test_packaging.py` fails if it falls behind `packages/`.

**Two changes were tried, measured, and reverted.** Anchoring a loop at the first repeated
*answer* (synthetic top-1 fell 94.4% → 88.9%). A `structural:silent_failure` detector
(fell to 83.3%, because cases that fail behaviourally have no failing step and it
displaced their real symptom). Do not retry either as a one-line change.

## 8. Release procedure

```bash
# 1. bump every manifest and __init__ together, plus CITATION.cff
# 2. write the CHANGELOG entry
uv sync && uv run pytest && uv run ruff check . && uv run mypy <all packages> tests
uv run bandit -c pyproject.toml -r packages -q
git commit && git tag -a vX.Y.Z && git push && git push --tags
uv build --all-packages --out-dir /tmp/dist
uv publish /tmp/dist/<pkg>-X.Y.Z*      # in dependency order, core first, meta last
# then verify from a clean venv against real PyPI
```

Sibling packages are pinned exactly (`runopsy-core==X.Y.Z`), so every manifest moves
together. `tests/test_packaging.py` enforces that and fails on a stale pin.

**PyPI's index lags.** After uploading, `uv pip install` may report the version
unsatisfiable for a few minutes even though `pypi.org/simple/` already lists it. Retry
with `--refresh`; it resolves.

## 9. What to do next, in the order I would do it

1. **Record more real runs.** Every headline number except the injection one still comes
   from traces we wrote. This is cheap and it is what the project needs most.
2. **Make the semantic layer earn TRAIL.** It is the only layer that can read a wrong
   *judgement*, and hybrid mode now looks at runs the deterministic layers found nothing
   in. Scoring L3 on TRAIL's 30 cases costs a few cents and would be the first number that
   speaks to the case Runopsy currently misses.
3. **Give `retry_storm` something to fire on** — an adapter that recognises a retry — or
   remove it and say so. A detector that cannot fire on real data is a claim, not a
   capability.
4. **A second live runtime adapter.** The architecture is runtime-agnostic and only one
   adapter proves it.

---

Authored by Vahit Feryad (ORCID 0000-0002-3282-339X). Commits carry no AI co-author
trailers, deliberately — see `CLAUDE.md`.

## 10. Hook overhead: measured, and why it was not "fixed"

Recording spawns one process per agent step. On the development machine that costs
**~3.6s per event**, which for a 40-step session is 2.5 minutes of pure latency.

The obvious fix — route `runopsy hook` around `main.py` so it stops importing Typer,
Rich, the benchmark suite, the detector registry and DuckDB — was measured before being
built, and it is not worth it:

| | ms |
| --- | ---: |
| bare Python startup | 448 |
| what a recorder cannot avoid importing (Pydantic schema, adapter, journal) | 2,065 |
| full CLI surface | 2,707 |
| **most a fast path could save** | **642** |

18% of the cost, in exchange for a second code path through the one component whose
first duty is not to break the run it observes. Making `runopsy_bench` lazy was tried
and measured at 2,727 → 2,758 ms — noise, because its cost is `runopsy_core`, which the
recorder needs anyway. Reverted rather than shipped.

The floor is interpreter startup plus Pydantic model construction, and no amount of
import trimming removes it from a per-event subprocess. The real options are
architectural: batch several events per invocation, or keep a resident recorder the hook
talks to. Both are design changes and neither should be done as a tweak.

Note the machine matters enormously: bare Python startup here is 448 ms against roughly
30 ms on a typical machine, so the same session elsewhere likely costs ~350 ms per step.
Re-measure before deciding this is urgent.
