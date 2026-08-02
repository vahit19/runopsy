"""The ``runopsy`` command line."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from runopsy_adapter import hermes, record_steps
from runopsy_adapter import launch as launcher
from runopsy_bench import compare_strategies, comparison_markdown, run_benchmark
from runopsy_cli import __version__, render
from runopsy_cli.config import CONFIG_FILENAME, RunopsyConfig, example_config, load_config
from runopsy_cli.report import render_report
from runopsy_collector import Collector, SealState, SealVerdict, StoreFromTheFutureError
from runopsy_core import AnalysisContext, apply_replay_evidence, to_otlp_json
from runopsy_core import diagnose as run_diagnosis
from runopsy_core.detectors import default_registry
from runopsy_core.schema import (
    CheckpointEvent,
    CheckpointPayload,
    Event,
    RunStartEvent,
    StatePayload,
    StateSnapshotEvent,
    ToolCallEvent,
)
from runopsy_replay import (
    DEFAULT_SANDBOX_IGNORES,
    Intervention,
    build_plan,
    evidence_from_stored_run,
    execute_plan,
)
from runopsy_semantic import (
    API_KEY_VARIABLE,
    Budget,
    KeyringUnavailableError,
    OpenRouterClient,
    delete_keyring,
    describe_source,
    resolve,
    resolve_api_key,
    review_diagnosis,
    write_keyring,
)

app = typer.Typer(
    name="runopsy",
    help="Diagnose AI agent runs: find where a run started failing and what it affected.",
    # Deliberately not `no_args_is_help`. Typer's default listed seventeen commands in
    # declaration order, headed by `hook` — which its own help says the runtime calls,
    # not a person. See `welcome.py` for what replaced it.
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)


def _tolerate_narrow_encodings() -> None:
    """Never let a console code page turn a diagnosis into a crash.

    On Windows a terminal running a legacy code page cannot encode the punctuation this
    output uses, and the failure is a traceback rather than a mangled character. It came
    up twice: ``runopsy graph`` died on a box-drawing character, and ``runopsy run``
    recorded forty-six events and then crashed printing the em dash in its summary — the
    work done, the answer lost.

    Replacing unencodable characters costs a nicer dash. Refusing to print costs the
    entire reason the command was run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):  # stream may be detached
                reconfigure(errors="replace")


_tolerate_narrow_encodings()

console = Console()
errors = Console(stderr=True)

LATEST: Final = "latest"


StoreOption = Annotated[
    Path | None,
    typer.Option("--store", help="Store directory. Defaults to .runopsy in the project."),
]
RunArgument = Annotated[str, typer.Argument(help="Run id, or 'latest' for the most recent.")]


def _config() -> RunopsyConfig:
    """Load runopsy.toml once per command, surfacing its warnings."""
    loaded = load_config()
    for warning in loaded.warnings:
        errors.print(warning, style="yellow")
    return loaded


def _resolve_run(collector: Collector, run_id: str) -> str:
    """Turn ``latest`` into a concrete run id, or exit with a usable message."""
    if run_id != LATEST:
        return run_id
    resolved = collector.latest_run_id()
    if resolved is None:
        errors.print("No runs recorded yet.", style="red")
        raise typer.Exit(code=2)
    return resolved


def cli() -> None:
    """The console entry point.

    Exists so one refusal reaches the user as a sentence rather than a traceback: a store
    written by a newer Runopsy is declined, and the person holding it needs to be told to
    upgrade, not shown a stack. Every command opens a store, so catching it here covers
    all of them without threading a handler through each.
    """
    try:
        app()
    except StoreFromTheFutureError as error:
        errors.print(str(error), style="red")
        raise SystemExit(2) from error


def _print_version(value: bool) -> None:
    """``--version``, which every command-line tool is expected to answer.

    It did not exist, so the flag people reach for first — in a bug report, in a support
    thread, in a CI log — printed a usage error instead of the one fact being asked for.
    """
    if value:
        typer.echo(f"runopsy {__version__}")
        raise typer.Exit


@app.callback()
def main(
    context: typer.Context,
    store: StoreOption = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Print the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Show where this machine stands, and what to type next, when given no command."""
    if context.invoked_subcommand is not None:
        return

    from runopsy_cli import welcome

    try:
        with Collector.open(store) as collector:
            runs = collector.runs()
            root = str(collector.paths.root)
    except Exception:
        # A welcome screen must open on a machine where nothing works yet — an
        # unreadable store is a thing to report, not a thing to crash on.
        runs, root = (), "not created yet"

    latest = runs[0] if runs else None
    status = hermes.adapter_status()
    resolved = resolve()

    console.print(
        welcome.screen(
            welcome.Situation(
                version=__version__,
                run_count=len(runs),
                latest_run=latest.run_id if latest else None,
                latest_state=(
                    (latest.outcome.value if latest.is_finished else "unfinished")
                    if latest
                    else None
                ),
                runtime_wired=status.is_wired,
                runtime_recorded=any(run.runtime == hermes.RUNTIME for run in runs),
                key_source=resolved.source if resolved else None,
                store=root,
            )
        )
    )


@app.command(name="hook", hidden=True)
def hook_command(
    event: Annotated[str, typer.Argument(help="Hermes hook event name, e.g. post_tool_call.")],
    store: StoreOption = None,
) -> None:
    """Record one Hermes hook payload read from stdin. Called by Hermes, not by hand.

    This runs on every tool call in an agent session, so it holds to one rule above all
    others: it must never break the run it is observing. Any failure — malformed
    payload, unwritable store, unknown event — is swallowed, an empty decision is
    printed, and the exit status stays zero. A diagnostic tool that takes down the agent
    it was watching is worse than no tool at all.
    """
    decision = "{}"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        payload.setdefault("hook_event_name", event)

        run = hermes.run_id_for(payload)
        try:
            settings = _config()
            with Collector.open(store) as collector:
                # Same vault the shell adapter fills. Without it a Hermes trace holds
                # hashes of text that was never stored, so evidence has nothing to show,
                # replay has nothing to re-run, and --mode hybrid pays for "withheld".
                mapped = hermes.map_payload(
                    payload,
                    sequence=collector.next_sequence(run),
                    vault=collector.vault if settings.vault_enabled else None,
                )
                if mapped is not None:
                    for event_to_record in _with_repository_state(
                        mapped,
                        collector=collector,
                        run=run,
                        cwd=_text_or_none(payload.get("cwd")),
                        enabled=settings.capture_git,
                    ):
                        collector.record(event_to_record)
        except Exception:
            # The index was unreachable, so opening the collector failed before anything
            # could be written. That is precisely when history matters most: an agent
            # delegating to parallel subagents fires several of these processes at one
            # store within milliseconds, and DuckDB admits a single writer. Losing the
            # step because the *derived* index was busy would break the one invariant
            # the whole design rests on.
            #
            # So fall through to the journal, which is the authoritative record and is
            # append-safe across processes. `runopsy doctor` reports the drift and
            # `rebuild` restores the index from exactly these files.
            _record_to_journal_only(payload, run, store)
    except Exception as error:
        # Reported rather than swallowed. Exiting zero keeps the agent running, but a
        # recorder that fails silently leaves a user with no trace and no reason why,
        # which is a worse outcome than the failure it was trying to survive.
        print(f"runopsy: could not record {event}: {type(error).__name__}", file=sys.stderr)

    typer.echo(decision)


def _port_is_taken(host: str, port: int) -> bool:
    """Whether something is already listening, asked before uvicorn tries to bind.

    A test rather than a caught exception, because uvicorn logs its own failure before
    raising and the message it logs is the one this exists to replace.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            return probe.connect_ex((host, port)) == 0
        except OSError:
            # An address this machine cannot even attempt is not a busy port. Let the
            # server start and report the real problem itself.
            return False


def _is_local_endpoint(url: str) -> bool:
    """Whether a configured endpoint keeps the request on this machine."""
    return any(host in url for host in ("localhost", "127.0.0.1", "[::1]"))


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _with_repository_state(
    event: Event,
    *,
    collector: Collector,
    run: str,
    cwd: str | None,
    enabled: bool,
) -> list[Event]:
    """The recorded event, plus what the repository did around it.

    A coding agent's real output is the working tree, and a trace of commands alone
    cannot say which step changed it — an argument hash cannot see a file. Two things
    come back from a look at the repository.

    ``git.head`` and ``git.branch`` are attached to the step as state deltas, and only
    when they actually changed. That restraint is the whole design: emitting them every
    step would show the flapping detector one value for a while and then another, which
    is what an ordinary mid-run commit looks like, and it would report a healthy run.

    Everything else — which files are dirty, how many, whether the tree is clean — goes
    into a separate ``state_snapshot`` event, written only when something moved. Snapshot
    values are not read by the flapping detector, which is why the dirty file set can
    live there safely: an agent that edits, tests, reverts and edits again returns the
    tree to the same state over and over, and that is ordinary work rather than
    disagreement.

    Never raises. Watching the repository is an enrichment; failing at it must not cost
    the step that was actually being recorded.
    """
    if not enabled or not isinstance(event, ToolCallEvent | RunStartEvent):
        return [event]

    try:
        from runopsy_adapter.repo import RepositoryWatch

        observed = RepositoryWatch(collector.paths.run_dir(run)).observe(Path(cwd) if cwd else None)
    except Exception:
        return [event]

    if observed is None:  # no repository, no git, or git had nothing to say
        return [event]

    recorded: list[Event] = [
        event.model_copy(update={"state_delta": {**event.state_delta, **observed.deltas}})
        if observed.deltas
        else event
    ]
    if observed.worth_a_snapshot:
        # One reservation, used for both the number and the id built out of it. Taking
        # the next sequence and then writing a different one into the event id is the
        # exact shape of the collision that cost two steps of a parallel run.
        sequence = collector.next_sequence(run)
        recorded.append(
            StateSnapshotEvent(
                event_id=f"{run}_evt_{sequence:04d}",
                run_id=run,
                sequence=sequence,
                timestamp=recorded[0].timestamp,
                state=StatePayload(values=observed.state.values()),
            )
        )
        checkpoint = _checkpoint_at(
            collector, run=run, cwd=cwd, head=observed.state.head, after=recorded[-1]
        )
        if checkpoint is not None:
            recorded.append(checkpoint)
    return recorded


def _checkpoint_at(
    collector: Collector,
    *,
    run: str,
    cwd: str | None,
    head: str | None,
    after: Event,
) -> Event | None:
    """A point this run can be returned to, recorded wherever the tree moved.

    ``runopsy replay`` has always looked for these and never found one, so every plan
    carried the warning that file state could not be restored. The reason is that nothing
    took them: a checkpoint needs the working tree, and the trace only had commands.

    What is stored is the commit plus a patch of the uncommitted changes, which together
    reconstruct the tree exactly. The patch goes to the vault — content-addressed,
    secret-scanned, and deletable — rather than into the user's repository, for the same
    reason the store excludes itself from their commits: Runopsy was asked to watch this
    repository, not to write to it.

    Returns ``None`` whenever anything is unavailable. A run without checkpoints is the
    behaviour of every version until now, so failing here costs nothing that was
    previously guaranteed.
    """
    if head is None:
        return None  # a repository with no commit has nothing to anchor a patch against
    try:
        from runopsy_adapter.repo import capture_patch

        patch = capture_patch(Path(cwd) if cwd else None)
        digest = collector.vault.put(patch) if patch else None
    except Exception:
        return None

    sequence = collector.next_sequence(run)
    return CheckpointEvent(
        event_id=f"{run}_evt_{sequence:04d}",
        run_id=run,
        sequence=sequence,
        timestamp=after.timestamp,
        checkpoint=CheckpointPayload(
            checkpoint_id=f"{run}_ck_{sequence:04d}",
            repo_state=head,
            patch_digest=digest,
        ),
    )


def _record_to_journal_only(payload: dict[str, object], run: str, store: Path | None) -> None:
    """Write one hook event using nothing but the journal.

    The fallback for a locked index. It touches no database, so it cannot be blocked by
    another process holding one, and the step number is reserved beside the journal
    rather than queried from the store.

    Which is the same allocator the indexed path uses, deliberately. This runs precisely
    when several processes are contending for one run, so it is the last place that can
    afford a numbering scheme of its own: two fallbacks landing on one number would build
    two events with one id, and the second would be deduplicated away.
    """
    from runopsy_adapter import hermes as hermes_adapter
    from runopsy_collector import StorePaths
    from runopsy_collector.journal import EventJournal
    from runopsy_collector.sequence import SequenceAllocator

    paths = StorePaths.resolve(store)
    paths.ensure()
    journal = EventJournal(paths.journal(run))
    sequence = SequenceAllocator(paths.run_dir(run)).reserve()

    mapped = hermes_adapter.map_payload(payload, sequence=sequence)
    if mapped is not None:
        journal.append(mapped)
        print(
            f"runopsy: index busy, wrote {mapped.event_id} to the journal only "
            "(run `runopsy doctor`)",
            file=sys.stderr,
        )


@app.command()
def setup(
    remove: Annotated[
        bool, typer.Option("--remove", help="Delete the stored key instead of setting one.")
    ] = False,
) -> None:
    """Store a provider key in the OS credential store.

    The key goes to Windows Credential Manager, macOS Keychain, or Secret Service — not
    to a file. A key in a file is a key in backups, in a synced folder, and eventually in
    a screenshot.

    Nothing in Runopsy requires this. Every deterministic feature works with no key at
    all; a key only buys ``diagnose --mode hybrid``.
    """
    if remove:
        removed = delete_keyring()
        console.print("Removed the stored key." if removed else "No stored key to remove.")
        return

    existing = resolve()
    already_stored = existing is not None and existing.source == "OS keyring"
    if already_stored and not typer.confirm("A key is already stored. Replace it?"):
        raise typer.Exit(code=1)

    # hide_input keeps it out of the terminal; it is never echoed, logged or stored
    # anywhere but the credential store.
    key = typer.prompt("OpenRouter API key", hide_input=True).strip()
    if not key:
        errors.print("No key entered; nothing was stored.", style="red")
        raise typer.Exit(code=2)

    try:
        write_keyring(key)
    except KeyringUnavailableError as error:
        errors.print(
            f"Could not use the OS credential store: {error}\n"
            f"Set {API_KEY_VARIABLE} in your environment instead.",
            style="red",
        )
        raise typer.Exit(code=1) from error

    console.print("Stored. Verify with: runopsy doctor")


@app.command()
def adapter(
    runtime: Annotated[str, typer.Argument(help="Runtime to configure. Only 'hermes' today.")],
    action: Annotated[
        str, typer.Argument(help="'config' to print the hook block, or 'status' to check it.")
    ] = "config",
    store: StoreOption = None,
) -> None:
    """Show how to connect a runtime to Runopsy.

    The configuration is printed for the user to paste rather than written into their
    file. Editing another tool's config behind its owner's back is how integrations
    become impossible to debug, and that file may hold settings we know nothing about.
    """
    if runtime != "hermes":
        errors.print(f"No adapter for {runtime!r}. Supported: hermes.", style="red")
        raise typer.Exit(code=2)

    if action == "status":
        _adapter_status(store)
        return
    if action == "plugin":
        installed = hermes.install_plugin()
        console.print(f"Installed the Runopsy plugin at {installed}.")
        console.print(
            "\nHermes loads user plugins only when named in its config. Add to "
            "config.yaml:\n\n  plugins:\n    enabled:\n      - runopsy\n",
            style="bold",
        )
        console.print(
            "The plugin records model calls — tokens, cost, latency — which shell hooks\n"
            "cannot carry: Hermes sends usage data only to Python plugins. Without it a\n"
            "trace has tool calls but no llm_call events, and the budget detector is\n"
            "blind. It only ever forwards to 'runopsy hook'; failures are swallowed so\n"
            "it can never break the run it is observing.",
            style="dim",
        )
        return
    if action != "config":
        errors.print(f"Unknown action {action!r}. Use 'config', 'status' or 'plugin'.", style="red")
        raise typer.Exit(code=2)

    target = Path(store).resolve() if store else None
    command = "runopsy hook" + (f" --store {target}" if target else "")

    console.print("Add this to your Hermes cli-config.yaml:\n", style="bold")
    console.print(hermes.hooks_config_block(command))
    console.print(
        "Then run Hermes once and approve the hooks when prompted, or start it with\n"
        "--accept-hooks. Every hook above only observes; none can block a tool call.\n"
        "Afterwards: runopsy runs\n",
        style="dim",
    )
    console.print(
        "Note: tool calls, sessions and subagents are captured; model calls are not.\n"
        "Hermes 0.19.0 dispatches post_llm_call only to Python plugins, never to shell\n"
        "hooks, so a recorded trace carries no token counts, cost or model latency.\n"
        "'hermes hooks test post_llm_call' will still report success — it calls the\n"
        "shell dispatcher directly — so it is not evidence that the event arrives.",
        style="yellow",
    )


def _adapter_status(store: Path | None) -> None:
    """Report whether Hermes is really wired to Runopsy, and whether it has recorded.

    Every failure this checks for is silent. A malformed hook block makes Hermes discard
    its entire config and run with defaults; a hook registered for a plugin-only event
    never fires. Both look exactly like a normal session that happened to record
    nothing, which is not a state anyone should have to diagnose by hand.
    """
    status = hermes.adapter_status()

    if status.config_path is None:
        console.print("Hermes config: not found", style="yellow")
        console.print(
            "  Looked in the usual locations. Run 'runopsy adapter hermes' for the block\n"
            "  to paste, and 'hermes config path' to see where it belongs.",
            style="dim",
        )
    else:
        console.print(f"Hermes config: {status.config_path}")

    if status.parse_error:
        console.print(f"  unreadable — {status.parse_error}", style="red")
        console.print(
            "  Hermes discards a config it cannot parse and runs with defaults, so the\n"
            "  session records nothing while looking entirely normal.",
            style="dim",
        )

    if status.configured:
        console.print(f"  hooks wired to runopsy: {', '.join(sorted(status.configured))}")
    if status.missing:
        console.print(f"  missing: {', '.join(status.missing)}", style="yellow")
    for event in status.never_fires:
        console.print(
            f"  {event}: configured, but Hermes only sends it to plugins — it will never fire",
            style="yellow",
        )

    with Collector.open(store) as collector:
        recorded = [run for run in collector.store.runs() if run.runtime == hermes.RUNTIME]
    if recorded:
        latest = recorded[0]
        console.print(f"  recorded runs: {len(recorded)}, most recent {latest.run_id}")
    else:
        console.print("  recorded runs: none yet", style="yellow")

    if status.is_wired and recorded:
        console.print("\nWired and recording.", style="green")
    elif status.is_wired:
        console.print("\nWired. Run Hermes once, then 'runopsy runs'.", style="dim")


@app.command()
def record(
    step: Annotated[
        list[str] | None,
        typer.Option("--step", "-s", help="A command to run and record. Repeat for a pipeline."),
    ] = None,
    task: Annotated[
        str, typer.Option("--task", help="What this run was trying to do.")
    ] = "recorded shell run",
    store: StoreOption = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Identifier. Defaults to a timestamped one.")
    ] = None,
    stop_on_failure: Annotated[
        bool, typer.Option("--stop-on-failure", help="Halt at the first failing step.")
    ] = False,
) -> None:
    """Run commands and record them as a trace.

    Execution continues past a failure by default, because that is what makes a trace
    worth diagnosing: an agent carries on after a step goes wrong, and the distance
    between the step that broke and the step where it became visible is the thing this
    tool exists to close.

    Commands run exactly as given, in a shell, with their real side effects. Recording
    observes; it does not sandbox.
    """
    if not step:
        errors.print(
            'Pass at least one --step, for example: runopsy record -s "make" -s "pytest"',
            style="red",
        )
        raise typer.Exit(code=2)

    identifier = run_id or f"run_{datetime.now(UTC):%Y%m%dT%H%M%S}"
    settings = _config()

    with Collector.open(store) as collector:
        outcomes = record_steps(
            step,
            run_id=identifier,
            task=task,
            sink=collector,
            vault=collector.vault if settings.vault_enabled else None,
            stop_on_failure=stop_on_failure,
            capture_git=settings.capture_git,
        )
        report = collector.integrity(identifier)

    failed = sum(outcome.failed for outcome in outcomes)
    console.print(
        f"Recorded {identifier}: {len(outcomes)} step(s), {failed} failed ({report.describe()})."
    )
    if failed:
        console.print(f"  runopsy diagnose {identifier}", style="dim")


@app.command()
def runs(store: StoreOption = None) -> None:
    """List recorded runs."""
    with Collector.open(store) as collector:
        console.print(render.runs_table(collector.runs()))


@app.command()
def diagnose(
    run: RunArgument = LATEST,
    store: StoreOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the diagnosis bundle as JSON.")
    ] = False,
    fail_on_finding: Annotated[
        bool,
        typer.Option(
            "--fail-on-finding",
            help="Exit non-zero when a candidate is found, for use in CI.",
        ),
    ] = False,
    mode: Annotated[
        str,
        typer.Option("--mode", help="deterministic (free, offline) or hybrid (asks a model)."),
    ] = "deterministic",
    budget_usd: Annotated[
        float | None, typer.Option("--budget-usd", help="Spend ceiling for hybrid mode.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model to review suspicious steps with.")
    ] = None,
) -> None:
    """Analyse a run and report where it started going wrong.

    The default mode spends no tokens, needs no provider, and gives the same answer
    every time. ``--mode hybrid`` additionally asks a model about the few steps the
    deterministic engine already found suspicious; it is bounded by a budget, cached,
    and can only add evidence — never a verdict.
    """
    if mode not in {"deterministic", "hybrid"}:
        errors.print(f"Unknown mode {mode!r}. Use deterministic or hybrid.", style="red")
        raise typer.Exit(code=2)

    ledger = None
    withheld: tuple[str, ...] = ()

    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)

        config = _config()
        context = AnalysisContext.from_events(run_id, events, config.detector_settings)
        summary = collector.store.run(run_id)

        if mode == "hybrid":
            # A local endpoint needs no credential, and demanding one would have made
            # "usable with no key at all" false for the only layer that could spend
            # money. The placeholder is sent because OpenAI-compatible servers expect
            # the header to exist, not because anything checks it.
            local = bool(config.semantic_base_url) and _is_local_endpoint(config.semantic_base_url)
            key = resolve_api_key() or ("local" if local else None)
            if key is None:
                errors.print(
                    f"No {API_KEY_VARIABLE} found, so hybrid mode cannot run. "
                    "Falling back to deterministic analysis, which needs no key.\n"
                    "A local model needs no key either: set semantic.base_url in "
                    f"{CONFIG_FILENAME} to an OpenAI-compatible endpoint.",
                    style="yellow",
                )
                bundle = run_diagnosis(context)
            else:
                budget = Budget(
                    max_calls=config.semantic_max_calls,
                    max_cost_usd=budget_usd
                    if budget_usd is not None
                    else config.semantic_cost_budget_usd,
                )
                result = review_diagnosis(
                    context,
                    OpenRouterClient(
                        key,
                        model=model or config.semantic_model,
                        base_url=config.semantic_base_url,
                    ),
                    budget=budget,
                    vault=collector.vault if config.vault_enabled else None,
                    cache_dir=collector.paths.root / "semantic-cache",
                )
                bundle, ledger, withheld = result.bundle, result.ledger, result.withheld
        else:
            bundle = run_diagnosis(context)

        # Replay experiments recorded against this run — in this session or an earlier
        # one — are folded in here. This is the only path that can raise a candidate to
        # replay_supported, and it works from the journals alone.
        for child in collector.runs():
            if child.run_id == run_id:
                continue
            evidence = evidence_from_stored_run(events, collector.events(child.run_id))
            if evidence is not None and evidence.parent_run_id == run_id:
                bundle = apply_replay_evidence(bundle, context.graph, evidence)

    if as_json:
        typer.echo(bundle.model_dump_json(indent=2))
    else:
        console.print(render.diagnosis(bundle, context.graph, summary))
        if ledger is not None:
            console.print(f"\nSemantic review: {ledger.describe()}.", style="dim")
            if ledger.stopped_because:
                console.print(f"Stopped early: {ledger.stopped_because}.", style="yellow")
            for item in withheld:
                console.print(f"Withheld from the model: {item}", style="dim")

    if fail_on_finding and bundle.candidates:
        raise typer.Exit(code=1)


@app.command()
def evidence(
    run: RunArgument = LATEST,
    step: Annotated[
        int | None, typer.Option("--step", help="Sequence number of the step to inspect.")
    ] = None,
    store: StoreOption = None,
    include_sensitive: Annotated[
        bool,
        typer.Option("--include-sensitive", help="Show payloads for steps flagged as sensitive."),
    ] = False,
) -> None:
    """Show the recorded evidence behind one step."""
    if step is None:
        errors.print("Pass --step N to choose a step. 'runopsy diagnose' lists them.", style="red")
        raise typer.Exit(code=2)

    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)

        context = AnalysisContext.from_events(run_id, events)
        bundle = run_diagnosis(context)
        # Read the text inside the collector's lifetime; the vault is on disk beside it.
        payloads = {
            digest: entry.text
            for digest in _payload_digests(context)
            if (entry := collector.vault.get(digest)) is not None
        }

    node = next((n for n in context.graph.nodes if n.sequence == step), None)
    if node is None:
        errors.print(f"Run {run_id} has no step {step}.", style="red")
        raise typer.Exit(code=2)

    console.print(
        render.evidence(
            bundle,
            context.graph,
            node.node_id,
            resolve_payload=payloads.get,
            include_sensitive=include_sensitive,
        )
    )


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What the agent should do.")],
    store: StoreOption = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model to pass to the runtime.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Provider to pass to the runtime.")
    ] = None,
    runtime: Annotated[str, typer.Option("--runtime", help="Only 'hermes' today.")] = "hermes",
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds before giving up on the agent.")
    ] = None,
    diagnose_after: Annotated[
        bool, typer.Option("--diagnose/--no-diagnose", help="Diagnose the run when it finishes.")
    ] = True,
) -> None:
    """Run an agent task and diagnose it, in one command.

    This drives the runtime through its documented command line and nothing else — no
    Hermes module is imported, nothing is monkey-patched, and the user's hook config is
    read rather than rewritten. The store is passed through the environment, so the
    hooks they already approved write where this command is looking.

    The difference from ``record`` is worth keeping in mind: ``record`` runs the steps
    itself and therefore knows every one of them. Here the agent decides its own steps
    and Runopsy learns what happened only through hooks, which is the real case — and
    the case where recording can fail silently. So this checks afterwards, and says so
    when nothing arrived.
    """
    if runtime != "hermes":
        errors.print(f"No launcher for {runtime!r}. Supported: hermes.", style="red")
        raise typer.Exit(code=2)

    executable = launcher.find_executable()
    if executable is None:
        errors.print(
            "No 'hermes' executable on PATH.\n"
            "Install it with: uv tool install hermes-agent\n"
            "Then: runopsy adapter hermes    (and paste the block it prints)",
            style="red",
        )
        raise typer.Exit(code=2)

    status = hermes.adapter_status()
    if not status.is_wired:
        # Warned rather than refused. The person may have configured hooks somewhere this
        # cannot see, and a diagnosis tool that will not let you run your own agent has
        # overstepped. But letting it proceed silently is how you end up with a session
        # that looks fine and recorded nothing.
        console.print(
            "Warning: Hermes does not appear to be wired to Runopsy, so this run may "
            "record nothing.\n  Check with: runopsy adapter hermes status",
            style="yellow",
        )

    with Collector.open(store) as collector:
        before = {run.run_id for run in collector.runs()}
        target = collector.paths.root

    console.print(f"Running {task!r} with {Path(executable).name}...", style="dim")
    try:
        result = launcher.launch(
            task,
            store=target,
            executable=executable,
            model=model,
            provider=provider,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        errors.print(f"The agent did not finish within {timeout}s.", style="red")
        raise typer.Exit(code=1) from None

    with Collector.open(store) as collector:
        fresh = [run for run in collector.runs() if run.run_id not in before]

    if not fresh:
        errors.print(
            "The agent finished but nothing was recorded.\n"
            "  runopsy adapter hermes status    # is the runtime wired?",
            style="red",
        )
        raise typer.Exit(code=1)

    newest = fresh[0]
    console.print(f"Recorded {newest.run_id} — {newest.event_count} events.", style="dim")

    if result.exit_code != 0:
        console.print(f"The agent exited with code {result.exit_code}.", style="yellow")

    if diagnose_after:
        console.print("")
        diagnose(run=newest.run_id, store=store)


@app.command(name="demo")
def demo_command(
    store: StoreOption = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the demo run in the store afterwards.")
    ] = False,
) -> None:
    """See what Runopsy does, in one command. No setup, no agent, no key.

    This is the first thing to run after installing. It records a worked example — an
    agent asked to fix a failing test, which breaks its environment on the way — and
    then diagnoses it, narrating what each part of the output means.

    The trace ships inside the package rather than living in the repository, because a
    demo that needs a git clone is no use to somebody who just ran `pip install`.
    """
    from runopsy_cli import demo as worked

    target = store or Path(".runopsy-demo")
    console.print("[bold]A worked example[/bold]\n", style="")
    console.print(
        "An agent was asked to fix a failing integration test. Fourteen steps later\n"
        "the test still failed. Here is what Runopsy makes of it.\n",
        style="dim",
    )

    with Collector.open(target) as collector:
        events = worked.trace()
        recorded = collector.record_all(events)
        # The originals behind the hashes. Without them `runopsy evidence` on the demo
        # answers "not kept locally" — the product's central claim, that a finding leads
        # back to the thing that happened, unillustrated at the one moment a new user
        # goes looking for it.
        for text in worked.payload_texts():
            collector.vault.put(text)
        context = AnalysisContext.from_events(worked.RUN_ID, collector.events(worked.RUN_ID))
        bundle = run_diagnosis(context)
        summary = collector.store.run(worked.RUN_ID)

    console.print(f"Recorded {recorded} events into {target}.\n", style="dim")
    console.print(render.diagnosis(bundle, context.graph, summary))

    console.print("\n[bold]What just happened[/bold]")
    console.print(
        f"  The run *visibly* failed at step {worked.SYMPTOM_STEP}, where the tests ran.\n"
        f"  Reading the log bottom-up sends you there, and the tests are fine.\n\n"
        f"  Runopsy points at step {worked.ONSET_STEP} instead: a config write that failed\n"
        "  five steps earlier, leaving the service pointed at the wrong environment.\n"
        "  Everything after it was doomed and none of it looked wrong.\n\n"
        "  Note what it does *not* say. It calls that a suspicion, not a cause, and\n"
        "  gives you the command that would settle it by experiment.",
        style="dim",
    )
    console.print("\n[bold]Try next[/bold]")
    console.print(
        f"  runopsy evidence --step {worked.ONSET_STEP} --store {target}"
        "     what that step actually recorded\n"
        f"  runopsy graph --store {target}"
        "                    the whole run as a timeline\n"
        f"  runopsy ui --store {target}"
        "                       the same thing in a browser\n\n"
        "  Then on your own work:\n"
        '  runopsy record -s "make" -s "pytest"'
        "          wrap any commands you already run",
        style="cyan",
    )

    if not keep:
        console.print(
            f"\n(The demo run stays in {target} — delete that folder, or pass --keep to "
            "keep it deliberately.)",
            style="dim",
        )


@app.command()
def label(
    run: RunArgument = LATEST,
    onset: Annotated[
        int | None,
        typer.Option("--onset", help="The step where it actually started going wrong."),
    ] = None,
    healthy: Annotated[
        bool, typer.Option("--healthy", help="Label this run as having nothing wrong.")
    ] = False,
    by: Annotated[str | None, typer.Option("--by", help="Who is making this claim.")] = None,
    category: Annotated[
        str, typer.Option("--category", help="Failure taxonomy category (design section 9).")
    ] = "undetermined",
    name: Annotated[
        str | None, typer.Option("--name", help="Case name. Defaults to the run id.")
    ] = None,
    describe: Annotated[str, typer.Option("--describe", help="One line: what went wrong.")] = "",
    affected: Annotated[
        str, typer.Option("--affected", help="Comma-separated steps the onset broke.")
    ] = "",
    notes: Annotated[str, typer.Option("--notes", help="Anything a reviewer should know.")] = "",
    out: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Where to write. Defaults to benchmarks/labelled/."),
    ] = None,
    store: StoreOption = None,
) -> None:
    """Record where a run *actually* went wrong, as a labelled benchmark case.

    This is how the corpus grows, and the corpus is the part of this project that cannot
    be copied: anyone can rebuild a graph view, nobody can rebuild a body of real agent
    failures where a person has said which step was the cause.

    The label is your claim, not the engine's. Nothing here reads what `diagnose` found,
    because a corpus scored against the engine's own opinion measures nothing — it would
    only ever confirm what the engine already believes.

    Nothing but hashes leaves your machine: a case carries the same digests the trace
    does and no payload text, so contributing a failure is not contributing your source.
    """
    from runopsy_bench import LabelError, carries_payload_text, label_run, to_json
    from runopsy_core.schema import FailureCategory

    if onset is None and not healthy:
        errors.print(
            "Pass --onset N to say where it went wrong, or --healthy if nothing did.\n"
            "  runopsy diagnose  shows the steps; the label is your call, not its.",
            style="red",
        )
        raise typer.Exit(code=2)
    if onset is not None and healthy:
        errors.print("--onset and --healthy contradict each other.", style="red")
        raise typer.Exit(code=2)

    labeller = by or os.environ.get("RUNOPSY_LABELLER") or ""
    if not labeller.strip():
        errors.print(
            "Pass --by 'Your Name' (or set RUNOPSY_LABELLER).\n"
            "  An unattributed label is a claim nobody stands behind.",
            style="red",
        )
        raise typer.Exit(code=2)

    try:
        chosen = FailureCategory(category)
    except ValueError:
        allowed = ", ".join(item.value for item in FailureCategory)
        errors.print(f"Unknown category {category!r}. One of: {allowed}", style="red")
        raise typer.Exit(code=2) from None

    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)
        summary = collector.store.run(run_id)
        runtime = summary.runtime if summary is not None else ""

    steps = {int(part) for part in affected.replace(" ", "").split(",") if part}
    try:
        case = label_run(
            list(events),
            name=name or run_id,
            category=chosen,
            description=describe or f"labelled from {run_id}",
            onset_step=None if healthy else onset,
            affected_steps=steps,
            labelled_by=labeller,
            runtime=runtime or "",
            notes=notes,
        )
    except LabelError as error:
        errors.print(str(error), style="red")
        raise typer.Exit(code=2) from error

    if carries_payload_text(case):
        errors.print(
            "This trace carries what looks like payload text rather than hashes.\n"
            "Refusing to write it: a case is meant to be shareable.",
            style="red",
        )
        raise typer.Exit(code=1)

    destination = out or Path("benchmarks") / "labelled" / f"{case.name}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(to_json(case), encoding="utf-8")

    console.print(f"Wrote {destination}.")
    console.print(
        f"  {len(case.events)} events · "
        f"{'healthy' if case.onset_step is None else f'onset at step {case.onset_step}'} · "
        f"labelled by {case.labelled_by}",
        style="dim",
    )
    console.print(
        "\nScore the engine against it:\n  runopsy bench --corpus benchmarks/labelled",
        style="dim",
    )


@app.command()
def graph(
    run: RunArgument = LATEST,
    store: StoreOption = None,
    output_format: Annotated[
        str, typer.Option("--format", help="'text' for the terminal, or 'dot' for Graphviz.")
    ] = "text",
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Write to a file instead of stdout.")
    ] = None,
) -> None:
    """Show the run as a causal graph: the chain of steps and what may have reached what.

    Recorded structure and inferred propagation are drawn differently on purpose. A
    reader who cannot tell an observed dependency from a guess has been handed a worse
    picture than none at all.
    """
    if output_format not in {"text", "dot"}:
        errors.print(f"Unknown format {output_format!r}. Use 'text' or 'dot'.", style="red")
        raise typer.Exit(code=2)

    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)
        context = AnalysisContext.from_events(run_id, events)
        bundle = run_diagnosis(context)

    if output_format == "dot":
        document = render.graph_dot(bundle, context.graph)
        if output is None:
            typer.echo(document, nl=False)
        else:
            output.write_text(document, encoding="utf-8")
            console.print(f"Wrote {output}.", style="dim")
        return

    console.print(render.causal_graph(bundle, context.graph))


def _payload_digests(context: AnalysisContext) -> set[str]:
    """Every payload digest referenced by a step in this run."""
    digests: set[str] = set()
    for node in context.graph.nodes:
        for key in ("arguments_hash", "output_hash", "prompt_hash", "response_hash"):
            value = (node.attributes or {}).get(key)
            if isinstance(value, str) and value:
                digests.add(value)
    return digests


@app.command()
def replay(
    run: RunArgument = LATEST,
    from_step: Annotated[
        int | None, typer.Option("--from-step", help="Step to replay from.")
    ] = None,
    store: StoreOption = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model to use instead of the original.")
    ] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Run the plan in a sandbox copy of this directory."),
    ] = False,
    skip_onset: Annotated[
        bool,
        typer.Option("--skip-onset", help="Intervention: omit the onset step and re-run the rest."),
    ] = False,
    substitute: Annotated[
        str | None,
        typer.Option("--substitute", help="Intervention: replace the onset step's command."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Plan a controlled re-run from a chosen step; with --execute, actually test it.

    Without --execute this produces a proposal and stops. With it, the replayable steps
    run in a disposable sandbox copy of the current directory — never in the working
    tree, and never any step the plan blocked. An intervention (--skip-onset or
    --substitute) turns the run into a counterfactual experiment: if the downstream
    failures disappear, the suspected onset is upgraded to a replay-supported cause.
    """
    if from_step is None:
        errors.print(
            "Pass --from-step N. 'runopsy diagnose' suggests one for its top candidate.",
            style="red",
        )
        raise typer.Exit(code=2)
    if skip_onset and substitute is not None:
        errors.print(
            "Choose one intervention: --skip-onset or --substitute. Changing two things "
            "at once would leave the result unable to say which change mattered.",
            style="red",
        )
        raise typer.Exit(code=2)

    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)
        context = AnalysisContext.from_events(run_id, events)

        if not any(node.sequence == from_step for node in context.graph.nodes):
            errors.print(f"Run {run_id} has no step {from_step}.", style="red")
            raise typer.Exit(code=2)

        plan = build_plan(context, from_step, intervention=Intervention(model=model))
        console.print(render.replay_plan(plan))

        if not execute:
            return

        runnable = len(plan.replayable) + len(plan.needs_approval)
        if not runnable:
            errors.print("Nothing in this plan is safe to re-run.", style="red")
            raise typer.Exit(code=1)
        if not yes:
            confirmed = typer.confirm(
                f"Run {runnable} command(s) in a sandbox copy of {Path.cwd().name}?"
            )
            if not confirmed:
                raise typer.Exit(code=1)

        suffix = 1
        while collector.store.run(f"{run_id}_replay{suffix}") is not None:
            suffix += 1
        cfg = _config()
        ignores = DEFAULT_SANDBOX_IGNORES + cfg.replay_sandbox_ignore
        # The store must never be copied into the sandbox: it holds the evidence the
        # experiment is judged against, and on Windows its open database is locked,
        # which fails the whole copy. Excluding it by its actual name covers stores
        # that do not use the default .runopsy prefix.
        store_root = collector.paths.root
        if store_root.is_relative_to(Path.cwd()):
            ignores += (store_root.relative_to(Path.cwd()).parts[0],)

        try:
            verdict = execute_plan(
                plan,
                context,
                collector.vault,
                collector,
                replay_run_id=f"{run_id}_replay{suffix}",
                substitute=substitute,
                skip_onset=skip_onset,
                approve_unknown=True,
                sandbox_ignores=ignores,
                timeout_seconds=cfg.replay_timeout_seconds,
            )
        except OSError as error:
            errors.print(
                f"Could not prepare the sandbox copy: {error}\n"
                "If a file in this directory is locked by another process, close it or "
                "exclude its directory via [replay] sandbox_ignore in runopsy.toml.",
                style="red",
            )
            raise typer.Exit(code=1) from error

    console.print(render.replay_verdict(verdict))


@app.command()
def export(
    run: RunArgument = LATEST,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Where to write. Defaults to <run>.html.")
    ] = None,
    store: StoreOption = None,
    include_sensitive: Annotated[
        bool,
        typer.Option(
            "--include-sensitive",
            help="Keep values from steps flagged as containing secrets.",
        ),
    ] = False,
    otlp: Annotated[
        bool,
        typer.Option("--otlp", help="Emit OpenInference-shaped OTLP JSON instead of HTML."),
    ] = False,
) -> None:
    """Write a self-contained HTML report for a run.

    Redaction is on by default rather than opt-in. Export is the sharing path, and a
    default that leaks is a default that eventually leaks in public; asking for the
    unsafe behaviour explicitly costs one flag and prevents the mistake that cannot be
    undone once a file has been sent.
    """
    with Collector.open(store) as collector:
        run_id = _resolve_run(collector, run)
        events = collector.events(run_id)
        if not events:
            errors.print(f"No events recorded for run {run_id}.", style="red")
            raise typer.Exit(code=2)

        context = AnalysisContext.from_events(run_id, events)
        bundle = run_diagnosis(context)
        summary = collector.store.run(run_id)

    if otlp:
        # Same redaction rules as the HTML report: an export is a sharing surface
        # whichever format it takes.
        document = to_otlp_json(context.graph, events, bundle, redact=not include_sensitive)
        destination = output or Path(f"{run_id}.otlp.json")
    else:
        document = render_report(bundle, context.graph, summary, redact=not include_sensitive)
        destination = output or Path(f"{run_id}.html")
    destination.write_text(document, encoding="utf-8")

    console.print(f"Wrote {destination} ({len(document.encode('utf-8')) // 1024} KB).")
    if include_sensitive:
        console.print(
            "Redaction was disabled: this file may contain values from steps flagged "
            "as holding secrets.",
            style="yellow",
        )


@app.command()
def ui(
    store: StoreOption = None,
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8756,
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind. Loopback by default.")
    ] = "127.0.0.1",
) -> None:
    """Serve a local web view of the recorded runs.

    Binds to loopback and has no authentication, because everything it serves is the
    contents of your repository. Binding it to another interface publishes that, so the
    flag exists for people who know they are doing it and warns when they do.

    Replay execution is deliberately absent from the web surface: it stays behind a
    command you type, where the plan can be read first.
    """
    try:
        import uvicorn

        from runopsy_server import create_app
    except ImportError as error:  # pragma: no cover - dependency is declared
        errors.print(f"The server extras are not installed: {error}", style="red")
        raise typer.Exit(code=1) from error

    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print(
            f"Binding to {host} exposes your traces to anything that can reach this "
            "machine. There is no authentication.",
            style="yellow",
        )

    if _port_is_taken(host, port):
        # Uvicorn's own answer here is a WinError 10048 in the operating system's
        # language, which tells somebody who typed one command that a socket bind
        # failed. The fix they need is a different number, so say that.
        errors.print(
            f"Port {port} on {host} is already in use — most often a Runopsy web view "
            f"you have open in another terminal.\n"
            f"Use a different one:  runopsy ui --port {port + 1}",
            style="red",
        )
        raise typer.Exit(code=2)

    console.print(f"Runopsy at http://{host}:{port} — press Ctrl+C to stop.")
    uvicorn.run(create_app(store), host=host, port=port, log_level="warning")


@app.command()
def prune(
    store: StoreOption = None,
    retain_days: Annotated[
        int | None,
        typer.Option("--retain-days", help="Keep runs newer than this. Overrides the config."),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually delete. Without it, only reports.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete recorded runs older than the retention window.

    Nothing expires on its own. A trace is a record of what your repository looked like
    while you worked, so removing one is a decision you make rather than a side effect of
    running a diagnosis — and without ``--apply`` this only tells you what would go.

    Runs with no recorded start are never expired. An unknown age is not an old age, and
    deleting something because its timestamp was missing would turn a recording bug into
    data loss.
    """
    days = retain_days if retain_days is not None else _config().retain_days
    if days <= 0:
        console.print(
            "Retention is off, so nothing expires. Set [privacy] retain_days in "
            "runopsy.toml, or pass --retain-days.",
            style="dim",
        )
        return

    with Collector.open(store) as collector:
        plan = collector.plan_prune(days)
        console.print(render.prune_plan(plan, days))

        if plan.is_empty or not apply:
            if not plan.is_empty:
                console.print("\nRe-run with --apply to delete.", style="dim")
            return

        if not yes and not typer.confirm(f"Delete {len(plan.expiring)} run(s) permanently?"):
            raise typer.Exit(code=1)

        result = collector.prune(plan)

    console.print(
        f"Removed {len(result.removed_runs)} run(s), {result.removed_events} events"
        + (
            f", {result.vault_entries_removed} vault entries"
            if result.vault_entries_removed
            else ""
        )
        + "."
    )


def _integrity_anomalies(collector: Collector) -> list[str]:
    """One line per run whose journal is not a clean, ordered, gapless stream.

    Read from the journals rather than the index, because the index deduplicates by
    event id and would therefore hide exactly the damage this exists to find.
    """
    anomalies: list[str] = []
    for run_id in collector.paths.known_run_ids():
        try:
            report = collector.integrity(run_id)
        except Exception as error:  # a journal that will not parse is itself the finding
            anomalies.append(f"{run_id}: unreadable ({type(error).__name__})")
            continue

        faults: list[str] = []
        if report.duplicate_sequences:
            faults.append(f"{len(report.duplicate_sequences)} duplicate step number(s)")
        if report.missing_sequences and _runopsy_recorded(collector, run_id):
            # Only damage in a run Runopsy numbered itself, where the allocator hands
            # out consecutive values and a gap means a process died holding one. A
            # constructed or trimmed journal is legitimately non-contiguous — the
            # integrity check is documented to allow exactly that — so reporting its
            # gaps as faults would make `doctor` cry wolf over every example trace.
            faults.append(f"{len(report.missing_sequences)} missing step(s)")
        if report.out_of_order:
            faults.append("out of order")
        if report.foreign_run_ids:
            faults.append(f"events from {len(report.foreign_run_ids)} other run(s)")
        if faults:
            anomalies.append(f"{run_id}: {', '.join(faults)}")
    return anomalies


def _runopsy_recorded(collector: Collector, run_id: str) -> bool:
    """Whether this run's step numbers came from Runopsy's allocator.

    The counter file is the evidence. A journal written by an adapter through the
    collector has one; a journal built by hand, imported, or trimmed does not — and the
    difference decides whether a gap in the numbering is a lost event or a deliberate
    window.
    """
    from runopsy_collector.sequence import COUNTER_NAME

    return (collector.paths.run_dir(run_id) / COUNTER_NAME).exists()


def _describe_seals(verdicts: list[SealVerdict]) -> str:
    """Whether the recorded journals are still the ones that were recorded.

    Unsealed runs are counted apart from broken ones on purpose. One means nobody can
    say; the other means somebody can, and the answer is no.
    """
    if not verdicts:
        return "nothing recorded yet"
    broken = sum(1 for verdict in verdicts if verdict.state is SealState.BROKEN)
    unsealed = sum(1 for verdict in verdicts if verdict.state is SealState.UNSEALED)
    if broken:
        return f"{broken} run(s) MODIFIED since recording — run `runopsy verify --all`"
    if unsealed:
        return f"{len(verdicts) - unsealed} sealed, {unsealed} predate sealing"
    return f"all {len(verdicts)} unchanged since recording"


def _describe_index(reindexed: int) -> str:
    """Whether the index had fallen behind the journals, and by how much.

    Recording tolerates a lost index write — DuckDB admits one writing process, so
    parallel subagents will sometimes lose one — and reading repairs it. Saying so is
    the point: a number here is normal, and a large one every time means something is
    holding the database open.
    """
    if reindexed == 0:
        return "in step with the journals"
    return f"was behind; reindexed {reindexed} event(s) from the journals"


def _describe_integrity(anomalies: list[str]) -> str:
    if not anomalies:
        return "no gaps, duplicates or reordering"
    return "\n".join(anomalies)


@app.command()
def verify(
    run_id: RunArgument = LATEST,
    store: StoreOption = None,
    every: Annotated[
        bool, typer.Option("--all", help="Check every recorded run instead of one.")
    ] = False,
) -> None:
    """Check that a recorded run has not been altered since it was recorded.

    Separate from the integrity check, and the pair is the point. Integrity asks whether
    the recorder did its job — no gaps, no duplicates, in order. This asks whether
    anything has happened to the file *since*, which no amount of well-formedness can
    answer: a trace with one line quietly rewritten is perfectly contiguous, and every
    other check in this system would call it intact.

    What it establishes is that a trace handed to somebody else, or read back a week
    later, is byte-for-byte the one that was recorded. It is tamper evidence, not tamper
    proofing: whoever can edit the journal can delete the seal beside it, and a signature
    that survived that would need a key this machine has nowhere safe to keep. Saying so
    plainly is worth more than a claim that does not hold.
    """
    with Collector.open(store) as collector:
        targets = (
            list(collector.paths.known_run_ids()) if every else [_resolve_run(collector, run_id)]
        )
        if not targets:
            errors.print("No runs recorded yet.", style="red")
            raise typer.Exit(code=2)
        verdicts = {target: collector.verify(target) for target in targets}

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("run")
    table.add_column("state")
    table.add_column("detail")
    for target, verdict in verdicts.items():
        colour = {"intact": "green", "broken": "red"}.get(verdict.state.value, "yellow")
        table.add_row(target, Text(verdict.state.value, style=colour), verdict.describe())
    console.print(table)

    broken = [name for name, verdict in verdicts.items() if verdict.state.value == "broken"]
    if broken:
        errors.print(
            f"\n{len(broken)} run(s) no longer match their seal. A diagnosis is an "
            "argument about what happened, and this is the evidence it rests on.",
            style="red",
        )
        raise typer.Exit(code=1)

    unsealed = [name for name, verdict in verdicts.items() if verdict.state.value == "unsealed"]
    if unsealed:
        # Not a failure. An unsealed journal predates sealing or came from another tool,
        # and treating "unknown" as "tampered" would make this check worthless within a
        # week of shipping it.
        console.print(
            f"\n{len(unsealed)} run(s) have no seal — recorded before sealing existed, "
            "or imported. Newly recorded runs are sealed automatically.",
            style="dim",
        )


@app.command()
def doctor(store: StoreOption = None) -> None:
    """Report what is configured, without revealing any secret.

    Credentials are reported by presence and source only. A tool that prints a key into
    a terminal — and from there into scrollback, screenshots and shared logs — has
    leaked it, however briefly it was on screen.
    """
    with Collector.open(store) as collector:
        paths = collector.paths
        run_count = len(collector.runs())
        reindexed = collector.reconcile_all()
        anomalies = _integrity_anomalies(collector)
        versions = collector.store.written_by
        seals = [collector.verify(run) for run in collector.paths.known_run_ids()]

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_row("store", str(paths.root))
    table.add_row("database", "present" if paths.database.exists() else "not created yet")
    table.add_row("runs recorded", str(run_count))
    table.add_row("index", _describe_index(reindexed))
    table.add_row("journals", _describe_integrity(anomalies))
    table.add_row("format", versions.describe())
    table.add_row("seals", _describe_seals(seals))
    table.add_row("detectors", f"{len(default_registry())} deterministic")

    # Reported by presence and source only. A key printed to a terminal is a key in
    # scrollback, screenshots and shared logs.
    table.add_row(API_KEY_VARIABLE, describe_source(resolve()))

    console.print(table)
    console.print(
        "\nDeterministic diagnosis needs no provider key and spends no tokens.",
        style="dim",
    )


@app.command()
def bench(
    verbose: Annotated[
        bool, typer.Option("--verbose", help="List every case and what was predicted.")
    ] = False,
    compare: Annotated[
        bool,
        typer.Option("--compare", help="Score baselines beside the engine."),
    ] = False,
    inject: Annotated[
        bool,
        typer.Option("--inject", help="Score against faults injected into the demo run."),
    ] = False,
    perf: Annotated[
        bool,
        typer.Option("--perf", help="Time ingest, graph build and diagnosis at scale."),
    ] = False,
    write: Annotated[
        Path | None,
        typer.Option("--write", help="Write a Markdown comparison report to this path."),
    ] = None,
    corpus: Annotated[
        Path | None,
        typer.Option("--corpus", help="Score labelled real runs from this directory instead."),
    ] = None,
) -> None:
    """Score the engine against labelled traces.

    The built-in suite is generated, not sampled, so the same code always produces the
    same numbers and a regression is visible immediately.

    ``--corpus`` scores human-labelled *real* runs instead — see ``runopsy label``. That
    is the number that eventually matters: synthetic cases prove the ranking behaves as
    designed, and only real ones show whether it helps.
    """
    if corpus is not None:
        _report_corpus(corpus)
        return

    if perf:
        _report_performance()
        return

    if inject:
        _report_injections()
        return

    if compare or write is not None:
        reports = compare_strategies()
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("strategy")
        table.add_column("top-1", justify="right")
        table.add_column("top-3", justify="right")
        table.add_column("step distance", justify="right")
        table.add_column("false positives", justify="right")
        for scored in reports:
            table.add_row(
                scored.strategy_name,
                f"{scored.top1_accuracy:.1%}",
                f"{scored.top3_recall:.1%}",
                f"{scored.mean_step_distance:.2f}",
                f"{scored.false_positive_rate:.1%}",
            )
        console.print(table)
        console.print(
            "\nThe baseline that matters is last_failure: reading a log bottom-up is "
            "free, so the engine has to beat it to be worth running.",
            style="dim",
        )
        if write is not None:
            write.parent.mkdir(parents=True, exist_ok=True)
            write.write_text(comparison_markdown(reports), encoding="utf-8")
            console.print(f"\nWrote {write}.")
        return

    report = run_benchmark()

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_row("cases", str(len(report.results)))
    table.add_row("scored for accuracy", str(len(report.scored)))
    table.add_row("onset top-1 accuracy", f"{report.top1_accuracy:.1%}")
    table.add_row("onset top-3 recall", f"{report.top3_recall:.1%}")
    table.add_row("mean step distance", f"{report.mean_step_distance:.2f}")
    table.add_row("false positive rate", f"{report.false_positive_rate:.1%}")
    console.print(table)

    if report.blind_spots:
        console.print(
            f"\n{len(report.blind_spots)} case(s) excluded: nothing in the trace is "
            "anomalous at the onset, so structural analysis cannot reach them.",
            style="dim",
        )
        for result in report.blind_spots:
            console.print(f"  {result.case.name} — {result.case.description}", style="dim")

    misses = report.failures()
    if misses:
        console.print("\nMissed the exact onset", style="bold")
        for result in misses:
            console.print(
                f"  {result.case.name}: truth step {result.case.onset_step}, "
                f"ranked {result.predicted_steps[:3]}",
                style="dim",
            )

    if verbose:
        console.print("\nAll cases", style="bold")
        for result in report.results:
            mark = "ok " if result.is_exact or result.case.is_healthy else "miss"
            console.print(f"  {mark}  {result.case.name}", style="dim")


if __name__ == "__main__":  # pragma: no cover
    app()


@app.command(name="config")
def config_command(
    init: Annotated[
        bool, typer.Option("--init", help="Write a commented runopsy.toml here.")
    ] = False,
) -> None:
    """Show the effective configuration, or write a starter file.

    Every key in the file is honored by the tool; unknown keys are reported rather than
    silently ignored, so a typo cannot leave you believing in a setting that never took
    effect.
    """
    target = Path.cwd() / CONFIG_FILENAME
    if init:
        if target.exists():
            errors.print(f"{target} already exists; not overwriting.", style="red")
            raise typer.Exit(code=2)
        target.write_text(example_config(), encoding="utf-8")
        console.print(f"Wrote {target}.")
        return

    loaded = _config()
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_row("config file", str(loaded.source) if loaded.source else "none (defaults)")
    settings = loaded.detector_settings
    table.add_row("retry_threshold", str(settings.retry_threshold))
    table.add_row("loop_threshold", str(settings.loop_threshold))
    table.add_row("stale_memory_hours", f"{settings.stale_memory_seconds / 3600:g}")
    table.add_row("token_budget", str(settings.token_budget) if settings.token_budget else "off")
    table.add_row("cost_budget_usd", f"{settings.cost_budget_usd:g}")
    table.add_row("replay step timeout", f"{loaded.replay_timeout_seconds}s")
    table.add_row("vault", "on" if loaded.vault_enabled else "off (replay execution disabled)")
    console.print(table)
    if loaded.source is None:
        console.print("\nCreate one with: runopsy config --init", style="dim")


def _report_injections() -> None:
    """Score the engine against faults injected into a clean synthetic run.

    A separate measurement from the labelled suite, and a harder one: the fixtures are
    generated by breaking a healthy run rather than written to exercise a detector, so
    the same hand did not author both the question and the answer.
    """
    from runopsy_bench import score_injections
    from runopsy_bench.cases import all_cases

    healthy = next(case for case in all_cases() if case.is_healthy)
    scores = score_injections(healthy.events)

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("injected fault")
    table.add_column("measure")
    table.add_column("hit", justify="right")
    table.add_column("top-3", justify="right")
    table.add_column("cases", justify="right")
    for score in scores:
        # Faults that remove the onset are scored on noticing the gap; showing them
        # under a localization heading would report a meaningless zero.
        top3 = f"{score.top3:.0%}" if score.measure == "onset" else "-"
        table.add_row(
            score.kind.value,
            score.measure,
            f"{score.top1:.0%}",
            top3,
            str(score.scored),
        )
    console.print(table)

    skipped = sum(score.skipped_undetectable for score in scores)
    if skipped:
        console.print(
            f"\n{skipped} injected fault(s) left nothing anomalous in the trace and are "
            "excluded. Reaching those needs semantic analysis or a replay.",
            style="dim",
        )


def _report_corpus(directory: Path) -> None:
    """Score the engine over human-labelled real runs."""
    from runopsy_bench import load_corpus, run_benchmark

    cases = load_corpus(directory)
    if not cases:
        errors.print(
            f"No labelled cases in {directory}.\n"
            "  Record a run, then: runopsy label latest --onset N --by 'Your Name'",
            style="yellow",
        )
        raise typer.Exit(code=2)

    report = run_benchmark(tuple(case.as_case() for case in cases))
    console.print(f"Scored {len(cases)} labelled real run(s) from {directory}.\n", style="dim")

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("onset top-1 accuracy", f"{report.top1_accuracy:.1%}")
    table.add_row("onset top-3 recall", f"{report.top3_recall:.1%}")
    table.add_row("mean step distance", f"{report.mean_step_distance:.2f}")
    table.add_row("false positive rate", f"{report.false_positive_rate:.1%}")
    console.print(table)

    labellers = sorted({case.labelled_by for case in cases if case.labelled_by})
    if labellers:
        console.print(f"\nLabelled by: {', '.join(labellers)}", style="dim")
    console.print(
        "These are human labels on real runs, so the number is the one that counts — "
        "and it is only as good as the labels behind it.",
        style="dim",
    )


def _report_performance() -> None:
    """Time the pipeline at the sizes the design document's quality gate names.

    Wall clock on this machine, so the absolute numbers will differ on yours. The
    durable part is the shape: every stage should stay roughly proportional to the
    number of events.
    """
    from runopsy_bench.performance import STAGES, run_performance_suite

    reports = run_performance_suite()

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("stage")
    for report in reports:
        table.add_column(f"{report.events:,}", justify="right")
    for stage in STAGES:
        row = [stage]
        for report in reports:
            timing = report.stage(stage)
            row.append(f"{timing.milliseconds:,.0f} ms" if timing else "-")
        table.add_row(*row)
    console.print(table)

    slowest = min(
        (
            (report.stage(stage).per_second, stage)  # type: ignore[union-attr]
            for report in reports
            for stage in STAGES
            if report.stage(stage) is not None
        ),
        default=None,
    )
    if slowest is not None:
        console.print(
            f"\nSlowest stage: {slowest[1]} at {slowest[0]:,.0f} events/second.",
            style="dim",
        )
