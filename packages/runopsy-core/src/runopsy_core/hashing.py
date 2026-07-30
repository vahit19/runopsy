"""Content-addressed references.

Runopsy stores hashes of prompts, tool arguments, claims and file contents instead of
the content itself. That is what lets a trace be inspected, shared or sent to a
diagnostic model without carrying source code or secrets with it, and it lets the
engine tell "the same call repeated" from "a different call" without reading either.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Final

_ALGORITHM: Final = "sha256"
_CHUNK_BYTES: Final = 1 << 20

DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

__all__ = ["DIGEST_PATTERN", "hash_bytes", "hash_file", "hash_text", "is_digest"]


def _format(digest: str) -> str:
    return f"{_ALGORITHM}:{digest}"


def hash_bytes(payload: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of ``payload``."""
    return _format(hashlib.sha256(payload).hexdigest())


def hash_text(payload: str) -> str:
    """Return the digest of ``payload`` encoded as UTF-8.

    Text is hashed without normalization: differing whitespace is a real difference for
    prompts and tool arguments, and silently collapsing it would hide replay divergence.
    """
    return hash_bytes(payload.encode("utf-8"))


def hash_file(path: Path) -> str:
    """Return the digest of a file, read incrementally so large artifacts stay cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return _format(digest.hexdigest())


def is_digest(value: str) -> bool:
    """Return whether ``value`` is a well-formed digest reference."""
    return DIGEST_PATTERN.match(value) is not None
