"""Finding credentials in captured output.

Nothing else in Runopsy sets ``contains_secret``; this is where the flag comes from. It
matters at the point of capture rather than at export, because a secret that reaches the
journal is already on disk, and every later control — redaction, payload minimization —
is then trying to unring a bell.

Patterns cover the credential shapes that actually leak: provider keys, cloud
credentials, tokens and private key blocks. Detection is best effort by construction, so
the scanner is one layer among several and never the reason it is safe to write
something down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("openai_or_openrouter", r"sk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_\-]{20,}"),
    ("github", r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"),
    ("huggingface", r"\bhf_[A-Za-z0-9]{20,}"),
    ("pypi", r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{10,}"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("slack", r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("google_api", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("bearer_header", r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{16,}"),
    ("assignment", r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[=:]\s*\S{8,}"),
)

_COMPILED: Final = tuple((name, re.compile(pattern)) for name, pattern in _PATTERNS)

PLACEHOLDER: Final = "[REDACTED]"


@dataclass(frozen=True)
class ScanResult:
    """What a scan found, and the text with findings removed."""

    redacted: str
    kinds: tuple[str, ...]

    @property
    def found(self) -> bool:
        return bool(self.kinds)


def scan(text: str) -> ScanResult:
    """Redact anything credential-shaped, reporting which kinds matched.

    Redaction happens even when the caller intends to discard the text, so a value
    cannot survive by being passed somewhere unexpected later.
    """
    kinds: list[str] = []
    redacted = text
    for name, pattern in _COMPILED:
        redacted, count = pattern.subn(PLACEHOLDER, redacted)
        if count:
            kinds.append(name)
    return ScanResult(redacted=redacted, kinds=tuple(kinds))


def contains_secret(text: str) -> bool:
    """Whether the text looks like it carries a credential."""
    return any(pattern.search(text) for _, pattern in _COMPILED)
