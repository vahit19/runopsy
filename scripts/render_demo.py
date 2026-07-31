"""Render the README's demo images from real command output.

Every pixel here comes from running the tool. A hand-drawn mock would be easier and
would drift the moment the output changed — and a screenshot of a diagnosis is exactly
the kind of thing that must not be a mock, in a project whose whole argument is that a
confident statement can be checked.

    uv run python scripts/render_demo.py

Writes SVG rather than PNG so the images stay text, diff sensibly in review, and scale
on a high-density screen. GitHub renders both in a README.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from runopsy_cli import __version__, render, welcome
from runopsy_core import AnalysisContext
from runopsy_core import diagnose as run_diagnosis

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "images"
WIDTH = 88


def console() -> Console:
    """A console that records instead of printing, at a width a README can show."""
    # Writing to the null device: the point is `record=True`, and printing the same
    # frames to the terminal while rendering them would only be noise.
    return Console(record=True, width=WIDTH, file=Path(os.devnull).open("w", encoding="utf-8"))


def save(recorder: Console, name: str, title: str) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{name}.svg"
    recorder.save_svg(str(path), title=title)
    recorder.file.close()
    return path


def demo_welcome() -> Path:
    """What someone sees the first time they type `runopsy`."""
    recorder = console()
    recorder.print(
        welcome.screen(
            welcome.Situation(
                version=__version__,
                run_count=3,
                latest_run="20260731_090821",
                latest_state="unfinished",
                runtime_wired=True,
                runtime_recorded=True,
                key_source=None,
                store=".runopsy",
            )
        )
    )
    return save(recorder, "welcome", "runopsy")


def demo_diagnosis() -> Path:
    """A real diagnosis of the worked example from the design document."""
    sys.path.insert(0, str(ROOT / "examples" / "coding_failure"))
    import seed  # type: ignore[import-not-found]

    events = seed.trace()
    context = AnalysisContext.from_events(events[0].run_id, events)
    bundle = run_diagnosis(context)

    recorder = console()
    recorder.print(render.diagnosis(bundle, context.graph, None))
    return save(recorder, "diagnosis", "runopsy diagnose")


def demo_graph() -> Path:
    """The causal view of the same run."""
    sys.path.insert(0, str(ROOT / "examples" / "coding_failure"))
    import seed  # type: ignore[import-not-found]

    events = seed.trace()
    context = AnalysisContext.from_events(events[0].run_id, events)
    bundle = run_diagnosis(context)

    recorder = console()
    recorder.print(render.causal_graph(bundle, context.graph))
    return save(recorder, "graph", "runopsy graph")


def main() -> int:
    for produce in (demo_welcome, demo_diagnosis, demo_graph):
        path = produce()
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
