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
from runopsy_bench.corpus import (
    CORPUS_VERSION,
    LabelError,
    LabelledRun,
    carries_payload_text,
    from_json,
    label_run,
    load_corpus,
    to_json,
)
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
from runopsy_bench.performance import (
    PerformanceReport,
    Timing,
    measure,
    run_performance_suite,
    scaling_factor,
    synthetic_trace,
)
from runopsy_bench.report import comparison_markdown

__version__ = "0.1.4"

__all__ = [
    "CORPUS_VERSION",
    "DEFAULT_STRATEGY",
    "BenchmarkReport",
    "CaseResult",
    "FaultKind",
    "FirstFailure",
    "InjectedFault",
    "InjectionScore",
    "LabelError",
    "LabelledRun",
    "LastFailure",
    "NoDiagnosis",
    "PerformanceReport",
    "RuleOnly",
    "Strategy",
    "SyntheticCase",
    "Timing",
    "__version__",
    "all_cases",
    "all_strategies",
    "applicable_kinds",
    "carries_payload_text",
    "compare_strategies",
    "comparison_markdown",
    "evaluate_case",
    "from_json",
    "inject",
    "injection_campaign",
    "label_run",
    "load_corpus",
    "measure",
    "run_benchmark",
    "run_performance_suite",
    "scaling_factor",
    "score_injections",
    "synthetic_trace",
    "to_json",
]
