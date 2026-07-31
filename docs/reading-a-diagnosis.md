# Reading a diagnosis

Runopsy's output is deliberately hedged, and the hedging is the useful part. This page
explains what each label is claiming so you know how much weight to put on it.

## The statuses, and what they cost to earn

| what you see | what it means | how it was earned |
| --- | --- | --- |
| **Observed failure** | the run visibly got this wrong | recorded: an exit code, an exception, an unfinished run |
| **Recovered failure** | this step failed; the run still succeeded | recorded, and the outcome says the agent got past it |
| **Suspected onset** | it may have started going wrong here | ranking over deterministic signals — *not* a cause |
| **Correlated cause** | it lines up in time, nothing more | temporal precedence plus impact reachability |
| **Model judgement, unverified** | a model read the step and thought so | one `--mode hybrid` call, capped below the deterministic ceiling |
| **Cause, supported by replay** | changing it made the failures go away | a counterfactual experiment actually ran |
| **Cause, verified by a person** | someone checked and signed it | a named human verifier |

Only the last two are causal claims. Everything above them is a suspicion with its
confidence attached, and no output path will phrase them otherwise — a test
(`runopsy_cli.language.asserts_causation`) fails the build if any of them starts to.

## Why the top candidate is not "the answer"

The engine ranks; it does not conclude. A suspected onset at 53% means *of the steps
this trace contains, this one best fits the pattern of where trouble started*. It does
not mean there is a 53% chance it caused anything. Two things are worth knowing:

- **Propagation is reachability, not effect.** "may have affected step 9" means step 9
  ran later and could have consumed something this step produced. It is drawn dashed in
  the UI and labelled *may reach* for exactly this reason.
- **The engine cannot see what was not recorded.** A step that succeeded while doing the
  wrong thing — a config written with a plausible wrong value — produces no anomaly at
  all. The deterministic layers will miss it by construction. That is what `--mode
  hybrid` and replay exist for.

## Turning a suspicion into something checked

Every unvalidated diagnosis ends with the command that would test it:

```bash
runopsy replay <run> --from-step 9                    # read the plan first
runopsy replay <run> --from-step 9 --execute --skip-onset
```

The second runs the replayable steps in a disposable copy of your project with one
intervention. Three outcomes, all reported plainly:

- **supported** — the downstream failures disappeared when the onset changed. The
  candidate is upgraded to *cause, supported by replay*.
- **not supported** — they did not. Either this step is not the cause, or the sandbox
  could not reproduce the state it needed. Both are left open.
- **inconclusive** — a straight re-run with no intervention. This is never reported as
  reproduction and never as causation, because it demonstrates neither.

## When there is nothing to say

> Nothing detectable went wrong in this run.

This is a real answer, not a failure to answer. A healthy run produces no finding, and
the false-positive rate on the benchmark is exactly zero rather than approximately zero
— a tool that warns during ordinary work teaches you to ignore it, and an ignored
warning discredits the ones that matter.

Note the word *detectable*. A run can go wrong in ways that leave no trace: see
`benchmarks/baseline-report.md` for the cases the engine misses and the ones it cannot
see at all.

## Confidence ceilings

Confidence is capped, not calibrated to a probability:

- unvalidated findings can never exceed **0.75**, whatever the signals say
- a replay-supported cause sits at **0.9**
- only a human verifier removes the ceiling

The ceiling exists so that a pile of weak signals cannot add up to a confident wrong
answer. If you find yourself trusting a 0.74 the way you would trust a 0.95, the number
is doing its job and the reading is not.
