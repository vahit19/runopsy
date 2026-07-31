"""Driving a runtime so that Runopsy records it, without owning the runtime.

This is what makes ``runopsy run`` possible while keeping the first design principle:
do not fork Hermes, integrate through what it documents. Nothing here imports a Hermes
module or patches its behaviour. It resolves the ``hermes`` executable, points
``RUNOPSY_HOME`` at the store so the already-configured shell hooks write there, and
runs it as a subprocess. If Hermes changes its internals tomorrow this keeps working;
if it changes its command line, this fails loudly rather than recording nothing.

The distinction from ``runopsy record`` matters. ``record`` wraps commands *we* run and
knows every step because it ran it. ``run`` starts an agent that decides its own steps,
and learns what happened only through the hooks — which is the real case the product
exists for, and the one where recording can silently fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchResult:
    """What happened when the runtime was driven."""

    executable: str
    exit_code: int
    run_id: str | None
    recorded_events: int

    @property
    def recorded_anything(self) -> bool:
        return self.recorded_events > 0


def find_executable(name: str = "hermes") -> str | None:
    """Where the runtime lives, or None when it is not installed."""
    return shutil.which(name)


def build_command(
    executable: str,
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    accept_hooks: bool = True,
    extra: tuple[str, ...] = (),
) -> list[str]:
    """The Hermes invocation for a single non-interactive task.

    ``--accept-hooks`` is passed because a run started by Runopsy has already had its
    hooks approved by the person who configured them; stopping to ask again would hang
    a non-interactive command with a prompt nobody can see. It stays a flag rather than
    a hard-coded assumption so an operator can withhold it.
    """
    command = [executable, "--cli", "-z", prompt]
    if accept_hooks:
        command.append("--accept-hooks")
    if model:
        command += ["-m", model]
    if provider:
        command += ["--provider", provider]
    command += list(extra)
    return command


def launch(
    prompt: str,
    *,
    store: Path,
    executable: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
    extra: tuple[str, ...] = (),
) -> LaunchResult:
    """Run one agent task with recording pointed at ``store``.

    The store is passed through ``RUNOPSY_HOME`` rather than by rewriting the user's
    hook configuration. Editing another tool's config to make our own command work is
    how an integration becomes impossible to debug, and it would leave the change behind
    after the command exits.
    """
    resolved = executable or find_executable()
    if resolved is None:
        msg = "no 'hermes' executable on PATH"
        raise FileNotFoundError(msg)

    environment = dict(os.environ)
    environment["RUNOPSY_HOME"] = str(store.resolve())

    # No shell: the argument list is built above from typed parameters, so a prompt
    # containing shell metacharacters is an argument rather than something to execute.
    completed = subprocess.run(
        build_command(resolved, prompt, model=model, provider=provider, extra=extra),
        cwd=str(cwd) if cwd else None,
        env=environment,
        timeout=timeout,
        check=False,
    )
    return LaunchResult(
        executable=resolved,
        exit_code=completed.returncode,
        run_id=None,
        recorded_events=0,
    )
