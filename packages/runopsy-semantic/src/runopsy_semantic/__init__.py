"""Optional, budget-bounded semantic analysis."""

from runopsy_semantic.budget import Budget, BudgetExceededError, Ledger, estimate_tokens
from runopsy_semantic.cache import VerdictCache
from runopsy_semantic.evaluator import (
    MAX_SEMANTIC_CONFIDENCE,
    PROMPT_VERSION,
    SemanticVerdict,
    cache_key,
    parse_verdict,
    review_span,
    to_signal,
)
from runopsy_semantic.payload import EvidencePacket, build_packet, window_around
from runopsy_semantic.provider import (
    API_KEY_VARIABLE,
    DEFAULT_MODEL,
    Completion,
    OpenRouterClient,
    ProviderError,
    resolve_api_key,
)
from runopsy_semantic.review import HybridResult, review_diagnosis

__version__ = "0.1.0"

__all__ = [
    "API_KEY_VARIABLE",
    "DEFAULT_MODEL",
    "MAX_SEMANTIC_CONFIDENCE",
    "PROMPT_VERSION",
    "Budget",
    "BudgetExceededError",
    "Completion",
    "EvidencePacket",
    "HybridResult",
    "Ledger",
    "OpenRouterClient",
    "ProviderError",
    "SemanticVerdict",
    "VerdictCache",
    "__version__",
    "build_packet",
    "cache_key",
    "estimate_tokens",
    "parse_verdict",
    "resolve_api_key",
    "review_diagnosis",
    "review_span",
    "to_signal",
    "window_around",
]
