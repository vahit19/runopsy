"""Labelled synthetic traces, baseline strategies and localization metrics."""

from runopsy_bench.baselines import (
    DEFAULT_STRATEGY,
    FirstFailure,
    LastFailure,
    NoDiagnosis,
    RuleOnly,
    Strategy,
    all_strategies,
)
from runopsy_bench.cases import SyntheticCase, all_cases
from runopsy_bench.metrics import (
    BenchmarkReport,
    CaseResult,
    compare_strategies,
    evaluate_case,
    run_benchmark,
)
from runopsy_bench.report import comparison_markdown

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_STRATEGY",
    "BenchmarkReport",
    "CaseResult",
    "FirstFailure",
    "LastFailure",
    "NoDiagnosis",
    "RuleOnly",
    "Strategy",
    "SyntheticCase",
    "__version__",
    "all_cases",
    "all_strategies",
    "compare_strategies",
    "comparison_markdown",
    "evaluate_case",
    "run_benchmark",
]
