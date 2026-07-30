"""The ``runopsy`` command line."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table

from runopsy_bench import run_benchmark
from runopsy_cli import render
from runopsy_collector import Collector
from runopsy_core import AnalysisContext
from runopsy_core import diagnose as run_diagnosis
from runopsy_core.detectors import default_registry

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
) -> None:
    """Score the engine against labelled synthetic traces.

    Reproducible and offline: the suite is generated, not sampled, so the same code
    always produces the same numbers and a regression is visible immediately.
    """
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
