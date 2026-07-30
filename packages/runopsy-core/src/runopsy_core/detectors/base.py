"""Detector contract and registry.

Every detector here is deterministic: same trace in, same signals out, no model call,
no network, no clock. That is not a performance choice. It means basic diagnosis costs
nothing, works offline, and can be re-run by someone else to get the identical answer —
which is what lets a signal be treated as evidence rather than as an opinion.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from runopsy_core.integrity import IntegrityReport, check_integrity
from runopsy_core.normalize import build_graph
from runopsy_core.schema import AnalysisLayer, Event, FailureSignal, TraceGraph


@dataclass(frozen=True)
class DetectorSettings:
    """Thresholds shared by the behavioral detectors.

    Defaults are deliberately conservative. A detector that fires on ordinary work
    trains users to ignore it, and an ignored warning is worse than a missing one
    because it also discredits the signals that matter.
    """

    retry_threshold: int = 3
    """Repeats of the same tool before a retry storm is reported."""

    loop_threshold: int = 3
    """Identical tool calls — same name and arguments — before a loop is reported."""

    stale_memory_seconds: float = 86_400.0
    """Age at which a memory read is worth flagging."""

    token_budget: int = 0
    """Total tokens above which spend is reported. Zero disables the check."""

    cost_budget_usd: float = 0.0
    """Spend above which cost is reported. Zero disables the check."""

    def with_overrides(self, **changes: object) -> DetectorSettings:
        """Return a copy with selected thresholds replaced."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True)
class AnalysisContext:
    """Everything a deterministic detector is allowed to look at."""

    run_id: str
    events: tuple[Event, ...]
    graph: TraceGraph
    integrity: IntegrityReport
    settings: DetectorSettings = field(default_factory=DetectorSettings)

    @classmethod
    def from_events(
        cls, run_id: str, events: Iterable[Event], settings: DetectorSettings | None = None
    ) -> AnalysisContext:
        """Build a context by normalizing an event stream."""
        materialized = tuple(
            sorted(
                (event for event in events if event.run_id == run_id),
                key=lambda event: (event.sequence, event.event_id),
            )
        )
        return cls(
            run_id=run_id,
            events=materialized,
            graph=build_graph(run_id, materialized),
            integrity=check_integrity(run_id, materialized),
            settings=settings or DetectorSettings(),
        )

    def of_kind[T](self, kind: type[T]) -> Iterator[T]:
        """Iterate events of one concrete type, in execution order."""
        for event in self.events:
            if isinstance(event, kind):
                yield event


@runtime_checkable
class Detector(Protocol):
    """A deterministic check over one run."""

    name: str
    layer: AnalysisLayer

    def detect(self, context: AnalysisContext) -> Iterable[FailureSignal]:
        """Yield a signal for everything wrong that this detector can see."""
        ...


class DetectorRegistry:
    """The set of detectors a diagnosis runs."""

    def __init__(self, detectors: Iterable[Detector] = ()) -> None:
        self._detectors: list[Detector] = []
        for detector in detectors:
            self.register(detector)

    def register(self, detector: Detector) -> None:
        """Add a detector, rejecting a duplicate name.

        Names appear in signal ids and in the evidence a user reads, so two detectors
        sharing one name would make a finding impossible to trace back to its source.
        """
        if any(existing.name == detector.name for existing in self._detectors):
            msg = f"detector name already registered: {detector.name!r}"
            raise ValueError(msg)
        self._detectors.append(detector)

    @property
    def detectors(self) -> tuple[Detector, ...]:
        return tuple(self._detectors)

    def __len__(self) -> int:
        return len(self._detectors)

    def run(self, context: AnalysisContext) -> tuple[FailureSignal, ...]:
        """Run every detector and return signals in a stable order.

        Ordering is by execution position first so a reader sees the earliest problem
        first, which is the whole point of onset localization; ties fall back to the
        signal id so repeated runs produce byte-identical output.
        """
        position = {node.node_id: node.sequence for node in context.graph.nodes}
        signals = [signal for detector in self._detectors for signal in detector.detect(context)]
        return tuple(sorted(signals, key=lambda s: (position.get(s.node_id, 1 << 62), s.signal_id)))


def signal_id(detector_name: str, node_id: str, discriminator: str = "") -> str:
    """Build a deterministic signal id.

    Stable ids let two runs of the same analysis be compared, and let a cached diagnosis
    be matched to the trace it came from.
    """
    suffix = f":{discriminator}" if discriminator else ""
    return f"{detector_name}:{node_id}{suffix}"
