---
name: Nothing was recorded
about: A run finished but produced no trace, or a partial one
labels: adapter
---

Start with the built-in check, which names the three ways this fails silently:

```bash
runopsy adapter hermes status
```

See [docs/troubleshooting.md](../../docs/troubleshooting.md) — the common causes are a
config Hermes could not parse (it discards the whole file and runs with defaults), a
hook registered for an event that only reaches plugins, and a store somewhere other
than where you are looking.

**What you ran**, and **what `runs` shows** afterwards.

**The hook configuration** you pasted, verbatim.

**Environment** — operating system, runtime version, and how Runopsy was installed.
