"""Runopsy core: normalized trace schema, graph and causal failure analysis."""

from runopsy_core.detectors import (
    AnalysisContext,
    DetectorRegistry,
    DetectorSettings,
    default_registry,
)
from runopsy_core.hashing import hash_bytes, hash_file, hash_text, is_digest
from runopsy_core.integrity import IntegrityReport, check_integrity
from runopsy_core.normalize import build_graph
from runopsy_core.schema import SCHEMA_VERSION

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisContext",
    "DetectorRegistry",
    "DetectorSettings",
    "IntegrityReport",
    "__version__",
    "build_graph",
    "check_integrity",
    "default_registry",
    "hash_bytes",
    "hash_file",
    "hash_text",
    "is_digest",
]
