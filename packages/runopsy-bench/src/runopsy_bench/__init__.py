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
from runopsy_bench.injection import (
    FaultKind,
    InjectedFault,
    InjectionScore,
    applicable_kinds,
    inject,
    injection_campaign,
    score_injections,
)
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
    "FaultKind",
    "FirstFailure",
    "InjectedFault",
    "InjectionScore",
    "LastFailure",
    "NoDiagnosis",
    "RuleOnly",
    "Strategy",
    "SyntheticCase",
    "__version__",
    "all_cases",
    "all_strategies",
    "applicable_kinds",
    "compare_strategies",
    "comparison_markdown",
    "evaluate_case",
    "inject",
    "injection_campaign",
    "run_benchmark",
    "score_injections",
]
