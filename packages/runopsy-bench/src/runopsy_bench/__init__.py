"""Labelled synthetic traces and localization metrics."""

from runopsy_bench.cases import SyntheticCase, all_cases
from runopsy_bench.metrics import BenchmarkReport, CaseResult, evaluate_case, run_benchmark

__version__ = "0.1.0"

__all__ = [
    "BenchmarkReport",
    "CaseResult",
    "SyntheticCase",
    "__version__",
    "all_cases",
    "evaluate_case",
    "run_benchmark",
]
