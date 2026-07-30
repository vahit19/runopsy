"""The ``runopsy`` command line."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table

from runopsy_adapter import hermes, record_steps
from runopsy_bench import compare_strategies, comparison_markdown, run_benchmark
from runopsy_cli import render
from runopsy_cli.report import render_report
from runopsy_collector import Collector
from runopsy_core import AnalysisContext
from runopsy_core import diagnose as run_diagnosis
from runopsy_core.detectors import default_registry
from runopsy_replay import Intervention, build_plan

app = typer.Typer(
    name="runopsy",
    help="Diagnose AI agent runs: find where a run started failing and what it affected.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
errors = Console(stderr=True)

LATEST: Final = "latest"

PROVIDER_VARIABLES: Final = ("OPENROUTER_API_KEY",)

StoreOption = Annotated[
    Path | None,
    typer.Option("--store", help="Store directory. Defaults to .runopsy in the project."),
]
RunArgument = Annotated[str, typer.Argument(help="Run id, or 'latest' for the most recent.")]


def _resolve_run(collector: Collector, run_id: str) -> str:
    """Turn ``latest`` into a concrete run id, or exit with a usable message."""
    if run_id != LATEST:
        return run_id
    resolved = collector.latest_run_id()
    if resolved is None:
        errors.print("No runs recorded yet.", style="red")
        raise typer.Exit(code=2)
    return resolved


@app.command(name="hook")
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
        with Collector.open(store) as collector:
            mapped = hermes.map_payload(payload, sequence=collector.store.next_sequence(run))
            if mapped is not None:
                collector.record(mapped)
    except Exception:
        pass

    typer.echo(decision)


@app.command()
def adapter(
    runtime: Annotated[str, typer.Argument(help="Runtime to configure. Only 'hermes' today.")],
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

    target = Path(store).resolve() if store else None
    command = "runopsy hook" + (f" --store {target}" if target else "")

    console.print("Add this to your Hermes cli-config.yaml:\n", style="bold")
    console.print(hermes.hooks_config_block(command))
    console.print(
        "Then run Hermes once and approve the hooks when prompted, or start it with\n"
        "--accept-hooks. Every hook above only observes; none can block a tool call.\n"
        "Afterwards: runopsy runs",
        style="dim",
    )


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

    with Collector.open(store) as collector:
        outcomes = record_steps(
            step,
            run_id=identifier,
            task=task,
            sink=collector,
            stop_on_failure=stop_on_failure,
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
) -> None:
    """Analyse a run and report where it started going wrong.

    Deterministic only: this spends no tokens, needs no provider, and produces the same
    answer every time it is given the same trace.
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

    if as_json:
        typer.echo(bundle.model_dump_json(indent=2))
    else:
        console.print(render.diagnosis(bundle, context.graph, summary))

    if fail_on_finding and bundle.candidates:
        raise typer.Exit(code=1)


@app.command()
def evidence(
    run: RunArgument = LATEST,
    step: Annotated[
        int | None, typer.Option("--step", help="Sequence number of the step to inspect.")
    ] = None,
    store: StoreOption = None,
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

    node = next((n for n in context.graph.nodes if n.sequence == step), None)
    if node is None:
        errors.print(f"Run {run_id} has no step {step}.", style="red")
        raise typer.Exit(code=2)

    console.print(render.evidence(bundle, context.graph, node.node_id))


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
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run", help="Kept for symmetry; always a plan.")
    ] = True,
) -> None:
    """Plan a controlled re-run from a chosen step.

    This produces a proposal and stops. Nothing is executed, no file is touched, and no
    tool is called again. Execution belongs to a runtime adapter and is deliberately a
    separate step, because a replay is the only thing here that can change the world and
    that decision belongs to a person reading a written plan.
    """
    if from_step is None:
        errors.print(
            "Pass --from-step N. 'runopsy diagnose' suggests one for its top candidate.",
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

    if not dry_run:
        console.print(
            "\nThis release plans replays but does not run them. Execution arrives with "
            "the runtime adapter.",
            style="yellow",
        )


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
def doctor(store: StoreOption = None) -> None:
    """Report what is configured, without revealing any secret.

    Credentials are reported by presence and source only. A tool that prints a key into
    a terminal — and from there into scrollback, screenshots and shared logs — has
    leaked it, however briefly it was on screen.
    """
    with Collector.open(store) as collector:
        paths = collector.paths
        run_count = len(collector.runs())

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_row("store", str(paths.root))
    table.add_row("database", "present" if paths.database.exists() else "not created yet")
    table.add_row("runs recorded", str(run_count))
    table.add_row("detectors", f"{len(default_registry())} deterministic")

    for variable in PROVIDER_VARIABLES:
        configured = bool(os.environ.get(variable))
        table.add_row(
            variable,
            "set in environment" if configured else "not set (offline modes still work)",
        )

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
    write: Annotated[
        Path | None,
        typer.Option("--write", help="Write a Markdown comparison report to this path."),
    ] = None,
) -> None:
    """Score the engine against labelled synthetic traces.

    Reproducible and offline: the suite is generated, not sampled, so the same code
    always produces the same numbers and a regression is visible immediately.
    """
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
