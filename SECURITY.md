# Security policy

## Reporting a vulnerability

Email **vahit.feryat@gmail.com** with `[runopsy security]` in the subject. Please do not
open a public issue for anything exploitable.

Include what you did, what happened, and the version or commit. A proof of concept helps
but is not required to report something.

You should get an acknowledgement within 72 hours. If a fix is warranted, the advisory
and the patch are published together, and you are credited unless you prefer otherwise.

## What Runopsy handles

Runopsy records what an AI agent did. That makes it a tool that sits close to source
code, command lines, and credentials, so the following are treated as security issues
rather than bugs:

- **A credential reaching the trace.** Prompts, tool arguments and file contents are
  referenced by hash and must never be stored as text. If a value survives into the
  JSONL journal, the DuckDB index, an HTML export, or terminal output, that is a
  vulnerability.
- **Redaction failing open.** The scanner flags credentials at capture time. A pattern it
  misses is a gap worth reporting; redaction silently doing nothing is a defect. This has
  happened once already — the flag was not travelling with the graph node, so export
  published flagged steps as though the scanner had never run.
- **The replay gate failing open.** Unrecognised tools must require approval, and
  external or destructive ones must be excluded from replay entirely. A classification
  that lets a side-effecting tool through unattended is a security issue, not a
  usability one.
- **Escaping the store.** Run ids and session ids arrive from third-party runtimes and end
  up in filesystem paths. Anything that turns recording into an arbitrary write qualifies.
- **A hook taking down the observed run.** `runopsy hook` executes inside somebody's agent
  session. A crash, a hang, or a non-zero exit there is a denial of service against the
  thing it was meant to help.

## What is out of scope

- `runopsy record` runs the commands you give it, with their real side effects. That is
  its purpose, not a sandbox escape.
- Diagnostic accuracy. A wrong onset candidate is a correctness bug — please report it,
  but through the normal issue tracker.
- Vulnerabilities in Hermes Agent or other runtimes. Report those upstream.

## Handling your own data

Runopsy is local-first: traces stay in `.runopsy/` in your project and nothing is sent
anywhere. Before sharing a trace or an exported report, note that `runopsy export`
redacts flagged values by default and that `--include-sensitive` deliberately does not.
`runopsy doctor` reports whether a credential resolved and from where, never its value.
