**Why** — what problem this solves. The diff shows what changed; it cannot show the
reasoning that produced it.

**Invariants** — this project keeps a short list in
[CONTRIBUTING.md](../CONTRIBUTING.md) that tests enforce rather than convention. If one
of them had to move, say so explicitly and argue it; if none did, this line can go.

**Measurement** — if this touches detection, ranking or impact:

```bash
uv run runopsy bench --compare
```

Paste the before and after. An accuracy drop is not automatically wrong, but it has to
be deliberate. Do not tune a threshold to make one fixture pass: the suite is twenty
synthetic cases, and a number fitted to them measures nothing.

**Checks**

- [ ] `uv run pytest`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] mypy, per the command in CONTRIBUTING.md
- [ ] `runopsy bench --write benchmarks/baseline-report.md` if the numbers moved
