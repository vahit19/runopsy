"""Deterministic failure detectors.

The default registry is the token-free floor of the product: it runs on every trace,
offline, with no provider configured. Semantic detectors are added on top only when a
user opts in and a budget allows it.
"""

from runopsy_core.detectors.base import (
    AnalysisContext,
    Detector,
    DetectorRegistry,
    DetectorSettings,
    signal_id,
)
from runopsy_core.detectors.behavioral import (
    BudgetDetector,
    IncompleteHandoffDetector,
    RetryStormDetector,
    StaleMemoryDetector,
    StateFlappingDetector,
    ToolLoopDetector,
    UnsupportedClaimDetector,
)
from runopsy_core.detectors.structural import (
    BlockedActionDetector,
    IncompleteRunDetector,
    MissingRunStartDetector,
    ModelCallDetector,
    OutcomeMismatchDetector,
    TimeoutDetector,
    ToolExecutionDetector,
    TraceIntegrityDetector,
)

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    ToolExecutionDetector(),
    TimeoutDetector(),
    ModelCallDetector(),
    BlockedActionDetector(),
    TraceIntegrityDetector(),
    IncompleteRunDetector(),
    MissingRunStartDetector(),
    OutcomeMismatchDetector(),
    RetryStormDetector(),
    ToolLoopDetector(),
    StateFlappingDetector(),
    StaleMemoryDetector(),
    IncompleteHandoffDetector(),
    UnsupportedClaimDetector(),
    BudgetDetector(),
)


def default_registry() -> DetectorRegistry:
    """A registry holding every deterministic detector."""
    return DetectorRegistry(DEFAULT_DETECTORS)


__all__ = [
    "DEFAULT_DETECTORS",
    "AnalysisContext",
    "BlockedActionDetector",
    "BudgetDetector",
    "Detector",
    "DetectorRegistry",
    "DetectorSettings",
    "IncompleteHandoffDetector",
    "IncompleteRunDetector",
    "MissingRunStartDetector",
    "ModelCallDetector",
    "OutcomeMismatchDetector",
    "RetryStormDetector",
    "StaleMemoryDetector",
    "StateFlappingDetector",
    "TimeoutDetector",
    "ToolExecutionDetector",
    "ToolLoopDetector",
    "TraceIntegrityDetector",
    "UnsupportedClaimDetector",
    "default_registry",
    "signal_id",
]
