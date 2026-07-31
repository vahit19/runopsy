---
name: Wrong or unhelpful diagnosis
about: Runopsy pointed at the wrong step, or missed the real one
labels: diagnosis
---

**What the run actually did wrong** — the step you believe was the cause, and why.

**What Runopsy said** — paste `runopsy diagnose <run>` output.

**The trace, if you can share it.** The best possible report is a labelled case:

```bash
runopsy label <run> --onset <the real step> --by "Your Name" \
  --category <see the taxonomy> --describe "one line" -o case.json
```

That file carries hashes and no payload text, so it does not contain your source code —
check with `runopsy export <run> --otlp` if you want to see exactly what travels. A case
attached here can go straight into the corpus, which is how the accuracy numbers improve.

**Environment** — `runopsy doctor` output, and which runtime recorded the run.
