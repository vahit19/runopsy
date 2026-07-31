---
name: False positive on a healthy run
about: Runopsy reported a finding on a run where nothing went wrong
labels: false-positive
---

This is the most serious kind of bug in a diagnosis tool. A finding on ordinary work
teaches people to ignore the tool, and an ignored warning discredits the ones that
matter — the benchmark holds the false-positive rate at exactly zero, not approximately.

**What the run was doing** — and why you consider it healthy.

**What was reported** — paste `runopsy diagnose <run>`.

**A labelled case, ideally**, which turns this report into a regression test:

```bash
runopsy label <run> --healthy --by "Your Name" --describe "why nothing went wrong"
```

**Environment** — `runopsy doctor`, and the runtime that recorded it.
