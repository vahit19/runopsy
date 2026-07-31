# What leaves your machine

Short answer: nothing, unless you pass `--mode hybrid` or run `runopsy setup`. This page
is the long answer, because "local-first" is a claim worth being able to check.

## What a trace actually contains

Runopsy records **hashes, not content**. A tool call is stored as its name, its exit
code, a duration, and a SHA-256 of its arguments and output. The prompt you sent, the
file you edited and the diff you produced are not in the trace.

That is what lets a trace be shared without carrying your source code with it, and it
is why two identical commands are recognisable as identical without either being
readable.

## The vault, and why it exists

Hashes alone make some things impossible: replay cannot re-run a command it cannot
read, and `runopsy evidence` can only show you a digest. So payload **text** is kept
separately, in a content-addressed vault under your store directory.

- It never leaves the machine and is never part of an export.
- The secret scanner runs **before** anything is written, and the redacted form is what
  lands on disk. A secret written anywhere outlives the scan that found it.
- A payload that was redacted refuses to execute in a replay — running a command with
  `[redacted]` where the token used to be is worse than not running it.
- Turn it off with `vault = false` in `runopsy.toml`. Replay execution and the useful
  half of `evidence` stop working; nothing else changes.

## Redaction, and where it applies

Steps whose payload matched the secret scanner are flagged `contains_secret`. Every
sharing surface withholds them by default and takes the same flag to reveal:

| surface | default | reveal with |
| --- | --- | --- |
| `runopsy export` (HTML) | withheld | `--include-sensitive` |
| `runopsy export --otlp` | withheld | `--include-sensitive` |
| `runopsy evidence` | withheld | `--include-sensitive` |
| `POST /v1/export` | withheld | `{"include_sensitive": true}` |
| `GET /v1/runs/{id}/report` | withheld | not available — the served report is always redacted |

Revealing shows the *redacted* text, since that is what the vault holds. There is no
command that prints an unredacted secret back to you.

## When a model is involved

`--mode hybrid` is the only thing that sends anything anywhere, and it is opt-in per
invocation. When you use it:

- only the few steps the deterministic engine already found suspicious are sent
- steps flagged as carrying a credential are withheld and the output says so, per step
- the call is bounded by `max_calls` and `max_cost_usd` in `runopsy.toml`
- the result is labelled *model judgement, unverified* and capped below the
  deterministic ceiling

`runopsy diagnose` with no flags makes zero network calls. So does everything else in
the CLI except `setup`, `run`, and hybrid mode.

## Credentials

A key is never written into a trace, a log, a diagnosis bundle, an export or a crash
report. `runopsy setup` stores it in the OS credential store — Windows Credential
Manager, macOS Keychain, Secret Service — not in a file, because a key in a file is a
key in backups, in a synced folder, and eventually in a screenshot.

Resolution order, first match wins: `--api-key`, then the environment, then the keyring,
then a `.env` in the working directory (developer convenience, and `doctor` warns when
it is used).

`runopsy doctor` reports whether a credential resolved and from which source. It never
prints the value, and a test enforces that.

## The local API

`runopsy ui` binds to `127.0.0.1` and has no authentication, because everything it
serves is the contents of your repository. Do not expose it. It is read-mostly by
design: there is no endpoint that executes a replay, and a test enforces its absence —
the one action that can change the world stays behind a command you type, where the
plan can be read first.

## Retention

Nothing expires on its own. `runopsy prune` shows what is past the retention window and
deletes only with `--apply`, and never a run whose age it cannot determine. Vault
entries are removed only when no surviving run still references them.
