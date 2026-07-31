"""What someone sees the first time they type ``runopsy``.

Typer's default is a wall of seventeen commands in declaration order, and the first one
listed was ``hook`` — whose own help says it is called by the runtime, not by hand. A
new reader's first impression was a machine-facing command they must never run.

So this screen answers the three questions someone actually has, in order: what is this,
what state am I in, and what do I type next. The third answer changes with the second —
telling somebody to diagnose a run when they have recorded none is how a tool gets
closed and not reopened.

Pure ASCII throughout, for the reason learned twice already: a Windows console on a
legacy code page raises rather than substituting, and a welcome screen that crashes is
a first impression there is no recovering from.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.text import Text

TAGLINE = "Find where an agent run started going wrong - not just where it stopped."

LOGO = r"""
  _____
 |  __ \                                Runopsy
 | |__) |_   _ _ __   ___  _ __  ___ _   _
 |  _  /| | | | '_ \ / _ \| '_ \/ __| | | |    causal failure analysis
 | | \ \| |_| | | | | (_) | |_) \__ \ |_| |    for AI agent runs
 |_|  \_\\__,_|_| |_|\___/| .__/|___/\__, |
                          | |         __/ |
                          |_|        |___/
"""


@dataclass(frozen=True)
class Situation:
    """Everything the welcome screen needs to know about this machine."""

    version: str
    run_count: int
    latest_run: str | None
    latest_state: str | None
    runtime_wired: bool
    runtime_recorded: bool
    key_source: str | None
    store: str


def _status(situation: Situation) -> Text:
    body = Text()
    body.append("  Status\n", style="bold")

    if situation.run_count:
        body.append(f"    {situation.run_count} run(s) recorded", style="green")
        if situation.latest_run:
            body.append(f"   latest {situation.latest_run}", style="dim")
            if situation.latest_state:
                body.append(f" ({situation.latest_state})", style="dim")
        body.append("\n")
    else:
        body.append("    no runs recorded yet\n", style="yellow")

    if situation.runtime_recorded:
        body.append("    Hermes wired and recording\n", style="green")
    elif situation.runtime_wired:
        body.append("    Hermes wired, nothing recorded yet\n", style="yellow")
    else:
        body.append("    no agent runtime connected", style="dim")
        body.append("   (not needed to wrap a pipeline)\n", style="dim")

    if situation.key_source:
        body.append(f"    provider key from {situation.key_source}\n", style="green")
    else:
        # Said plainly and without alarm: the product works entirely without one, and a
        # tool that nags for a credential it does not need trains people to ignore it.
        body.append("    no provider key", style="dim")
        body.append("   (only --mode hybrid needs one)\n", style="dim")

    return body


def _next_steps(situation: Situation) -> Text:
    """The one or two commands that make sense from where this user actually is."""
    steps = Text()
    steps.append("\n  Start here\n", style="bold")

    def step(command: str, gloss: str) -> None:
        steps.append(f"    {command:<44}", style="cyan")
        steps.append(f"{gloss}\n", style="dim")

    if situation.run_count == 0:
        step('runopsy record -s "make" -s "pytest"', "wrap a pipeline you already have")
        if situation.runtime_wired:
            step('runopsy run "fix the failing test"', "drive an agent and diagnose it")
        else:
            step("runopsy adapter hermes", "connect an agent runtime")
        return steps

    latest = situation.latest_run or "latest"
    step("runopsy diagnose latest", f"where {latest} went wrong")
    step("runopsy ui", "the timeline and failure map in a browser")
    if not situation.runtime_wired:
        step("runopsy adapter hermes", "connect an agent runtime")
    return steps


def screen(situation: Situation) -> RenderableType:
    """The whole welcome view."""
    header = Text(LOGO, style="bold cyan")
    header.append(f"  {TAGLINE}\n", style="")
    header.append(f"  version {situation.version}   ", style="dim")
    header.append("local-first - nothing leaves this machine\n\n", style="dim")

    footer = Text()
    footer.append("\n  All commands       ", style="dim")
    footer.append("runopsy --help\n", style="cyan")
    footer.append("  What it is doing   ", style="dim")
    footer.append("runopsy doctor\n", style="cyan")
    footer.append("  Documentation      ", style="dim")
    footer.append("https://github.com/vahit19/runopsy\n", style="cyan")

    return Group(header, _status(situation), _next_steps(situation), footer)
