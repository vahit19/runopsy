"""Runopsy: find where an AI agent run started going wrong.

This distribution is what ``pip install runopsy`` gets you — the CLI, the engine, the
collector, replay, the adapters and the local web view, all working offline with no
provider key. It is a thin package on purpose: the code lives in ``runopsy-core``,
``runopsy-cli`` and the rest, so a library user can depend on exactly the piece they
need without dragging in a web server.

Importing this module gives you the pieces most people want without having to remember
which distribution each one lives in::

    from runopsy import AnalysisContext, Collector, diagnose

    with Collector.open(".runopsy") as collector:
        events = collector.events("run_0042")
        bundle = diagnose(AnalysisContext.from_events("run_0042", events))
        print(bundle.primary.summary if bundle.primary else "nothing detectable")
"""

from runopsy_cli import __version__ as _cli_version
from runopsy_collector import Collector
from runopsy_core import AnalysisContext, build_graph, diagnose, rank_candidates
from runopsy_core.schema import (
    DiagnosisBundle,
    DiagnosisCandidate,
    Event,
    TraceGraph,
)

__version__ = _cli_version

__all__ = [
    "AnalysisContext",
    "Collector",
    "DiagnosisBundle",
    "DiagnosisCandidate",
    "Event",
    "TraceGraph",
    "__version__",
    "build_graph",
    "diagnose",
    "rank_candidates",
]
