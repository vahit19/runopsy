# runopsy-inspect

Reads [Inspect AI](https://inspect.aisi.org.uk/) eval logs into Runopsy traces, so a
sample that went wrong can be diagnosed with the same engine as any other run.

```bash
uv run runopsy-inspect import logs/2026-07-31_task.eval --store .runopsy
uv run runopsy diagnose latest
```

Unlike the OpenInference direction, this is an import rather than an export, and it is
safe to do here for a specific reason: Inspect's log is a typed schema of its own —
`ToolEvent.function`, `.arguments`, `.error` — read through its own reader. Nothing is
inferred from attribute names that might mean something else.
