"""Runopsy core: normalized trace schema, graph and causal failure analysis."""

from runopsy_core.detectors import (
    AnalysisContext,
    DetectorRegistry,
    DetectorSettings,
    default_registry,
)
from runopsy_core.diagnose import diagnose, trace_fingerprint
from runopsy_core.hashing import hash_bytes, hash_file, hash_text, is_digest
from runopsy_core.impact import affected_nodes, infer_affects
from runopsy_core.integrity import IntegrityReport, check_integrity
from runopsy_core.normalize import build_graph
from runopsy_core.ranking import MAX_UNVALIDATED_CONFIDENCE, RankingWeights, rank_candidates
from runopsy_core.schema import SCHEMA_VERSION

__version__ = "0.1.0"

__all__ = [
    "MAX_UNVALIDATED_CONFIDENCE",
    "SCHEMA_VERSION",
    "AnalysisContext",
    "DetectorRegistry",
    "DetectorSettings",
    "IntegrityReport",
    "RankingWeights",
    "__version__",
    "affected_nodes",
    "build_graph",
    "check_integrity",
    "default_registry",
    "diagnose",
    "hash_bytes",
    "hash_file",
    "hash_text",
    "infer_affects",
    "is_digest",
    "rank_candidates",
    "trace_fingerprint",
]
