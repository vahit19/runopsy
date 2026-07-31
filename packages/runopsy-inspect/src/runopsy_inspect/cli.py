"""``runopsy-inspect import`` — read an Inspect eval log into a Runopsy store.

A separate entry point rather than a subcommand of ``runopsy``, because it is the only
thing here that needs inspect-ai installed. Folding it into the main CLI would make the
core tool refuse to start when an optional benchmark dependency is missing, which is the
opposite of the promise that Runopsy works with nothing configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from runopsy_collector import Collector
from runopsy_inspect.convert import log_to_runs

app = typer.Typer(add_completion=False, help="Import Inspect AI eval logs into Runopsy.")


@app.command("import")
def import_log(
    log_file: Annotated[Path, typer.Argument(help="An .eval or .json log written by Inspect.")],
    store: Annotated[
        Path | None, typer.Option("--store", help="Store directory. Defaults to .runopsy.")
    ] = None,
    vault: Annotated[
        bool, typer.Option("--vault/--no-vault", help="Keep payload text locally.")
    ] = True,
) -> None:
    """Import every sample in the log as its own run."""
    from inspect_ai.log import read_eval_log

    if not log_file.exists():
        typer.secho(f"No such log: {log_file}", fg="red", err=True)
        raise typer.Exit(code=2)

    log = read_eval_log(str(log_file))
    with Collector.open(store) as collector:
        runs = log_to_runs(log, vault=collector.vault if vault else None)
        for run_id, events in runs.items():
            recorded = collector.record_all(events)
            typer.echo(f"{run_id}: {recorded} event(s)")

    if not runs:
        typer.secho("The log contained no samples.", fg="yellow")
        return
    typer.echo(f"\nImported {len(runs)} run(s). Next: runopsy diagnose latest")


def main() -> None:
    app()
