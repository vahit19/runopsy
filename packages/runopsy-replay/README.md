# runopsy-replay

Proposes a controlled re-run of part of a run. It never performs one.

Separating the proposal from the act is the whole point. A replay is the only thing in
Runopsy that can change the world, so the risky decision — what gets executed again — is
made by a person looking at a written plan, not by a scoring function that happened to
rank a step highly.

## Fail closed

The side-effect classifier only ever sees a tool's *name*. Arguments are stored as
hashes by design, so it cannot know what a command actually did. A heuristic gate built
on that has exactly one safe default: anything unrecognised needs human approval.

The asymmetry decides it. A false alarm costs one keystroke. A missed classification
re-sends an email, re-charges a card, or deletes a branch a second time, and no quality
of diagnosis afterwards undoes that.

## What a plan will not promise

A plan states its own limits, because a replay whose caveats are invisible produces
false confidence rather than evidence:

- **No checkpoint at the chosen step** — the session can fork but the working tree may
  not match, so a difference in outcome may come from the files rather than the change.
- **Checkpoint and fork point at different steps** — file state and message history
  would be restored to different moments.
- **More than one variable changed** — if the outcome differs, nothing says which change
  did it. Counterfactual validation needs one intervention at a time.
- **A trace with gaps** — the original conditions cannot be reconstructed from an
  incomplete recording.
