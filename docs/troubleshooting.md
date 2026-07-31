# When it does not work

Every entry here is a failure that actually happened during development, in the form it
first appeared. They share a shape: nothing errors, and the trace is simply missing
something.

## The session ran fine and recorded nothing

Start here:

```bash
runopsy adapter hermes status
```

It names the three ways this breaks silently.

**The config could not be parsed.** Hermes discards a config it cannot read and runs
with defaults — a session that behaves entirely normally and records nothing. The usual
cause is quoting only the path:

```yaml
# broken: a quoted scalar followed by junk
command: "C:/Program Files/runopsy.exe" hook post_tool_call

# correct: the whole invocation is one scalar
command: 'C:/Program Files/runopsy.exe hook post_tool_call'
```

Paste the block `runopsy adapter hermes` prints rather than writing it by hand; it
quotes correctly for you.

**A hook is registered that can never fire.** `post_llm_call` and `on_session_finalize`
exist in Hermes but only on the plugin path. A shell hook for them never fires.
`hermes hooks test post_llm_call` will still report success — it calls the shell
dispatcher directly, so it tests our handler and never the delivery.

**The store is somewhere else.** Hooks write where `--store` or `RUNOPSY_HOME` points.
`runopsy runs` with no `--store` looks in `.runopsy` beside the project.

## The trace has tool calls but no model calls

Expected without the plugin. Hermes sends token usage only to Python plugins:

```bash
runopsy adapter hermes plugin      # installs it
# then add to Hermes' config.yaml:
#   plugins:
#     enabled:
#       - runopsy
```

Without it the budget detector has nothing to work with. With it you get tokens, cache
statistics, latency and a best-effort cost per call.

## Every test run looks like a successful step

Hermes reports whether the **tool** ran, not whether the command it ran succeeded. Its
terminal tool returns `{"output": ..., "exit_code": N}` and reports status `ok` whenever
the shell was invoked. Runopsy reads the inner `exit_code` out of a structured result,
so this is handled — but if you write your own adapter, this is the trap: a suite
failing on every attempt looked, to the detectors, like twenty-one successful steps.

## `--mode hybrid` says "withheld: not in the local store"

The vault is off, or the trace predates it. The model is being asked about steps whose
content was never kept, and you are paying for the call. Set `vault = true` in
`runopsy.toml` and record again.

## The engine flags a loop that is not one

Re-running a verification command after each edit is not a loop, and the detector knows
the difference: it fires only when the repeated calls keep failing, or keep returning
results already seen. If you are seeing a false positive here, the outputs are probably
not being recorded — with no output there is nothing to judge progress by, and the
detector falls back to counting arguments.

## Replay says "no checkpoint at or before this step"

Expected, and stated rather than hidden. Runopsy did not record a filesystem snapshot,
so a fork can re-run the commands but cannot restore the file state they assumed. Read
the plan before executing: steps it cannot vouch for are marked `approve`, and an
unrecognised tool always is — the gate fails closed.

## `runopsy` crashes printing its answer

Fixed, but worth knowing the shape: a Windows console on a legacy code page raises
rather than substituting when it meets a character it cannot encode. The work was done
and only the printing failed. Output streams are now reconfigured to replace; if you see
this on any surface, it is a bug worth reporting with your code page (`chcp`).

## The web view is blank

The build output is not committed. Either build it, or use the fallback:

```bash
cd packages/runopsy-ui && npm install && npm run build
```

Without it the server serves a plain HTML index instead, which is deliberate — a
diagnosis tool should not go dark because nobody ran a JavaScript build.

## Diagnosis differs between two runs of the same trace

It should not. Diagnosis is a pure function of the trace: no clock, no network, no
model. If you can reproduce a difference, that is a genuine bug and worth an issue —
`trace_fingerprint` in the bundle identifies exactly which events were analysed.
