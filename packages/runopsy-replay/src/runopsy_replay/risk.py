"""Classifying what a tool call would do if it ran again.

All the classifier has is the tool's name: arguments are stored as hashes, by design, so
nothing here can read what a command actually did. That makes this a heuristic, and the
only safe way to build a heuristic gate is **fail closed** — a tool nobody has classified
is treated as needing human approval, never as safe.

The cost of being wrong is asymmetric and that asymmetry decides the default. A
false alarm costs one keystroke of confirmation. A missed classification re-sends an
email, re-charges a card, or deletes a branch a second time, and no amount of good
diagnosis afterwards undoes it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final


class SideEffect(StrEnum):
    """What re-running a step would do to the world."""

    READ_ONLY = "read_only"
    """Observes without changing anything. Safe to repeat."""

    LOCAL_WRITE = "local_write"
    """Changes files in the working tree. Safe inside a fork or worktree."""

    EXTERNAL = "external"
    """Reaches something outside the machine: deploys, sends, publishes, pays."""

    DESTRUCTIVE = "destructive"
    """Removes or overwrites something that may not be recoverable."""

    UNKNOWN = "unknown"
    """Unclassified. Treated as unsafe until a person says otherwise."""


_DESTRUCTIVE: Final = (
    r"\b(rm|rmdir|delete|destroy|drop|truncate|purge|wipe|format|erase|prune|revoke)\b",
    r"force[-_ ]?push",
    r"reset[-_ ]?hard",
)

_EXTERNAL: Final = (
    r"\b(email|mail|send|notify|sms|page|slack|discord|tweet|post)\b",
    r"\b(pay|payment|charge|refund|invoice|billing|checkout|transfer)\b",
    r"\b(deploy|release|publish|upload|push|promote|rollout|provision)\b",
    r"\b(http|https|curl|wget|fetch|request|webhook|api[-_ ]?call)\b",
)

_LOCAL_WRITE: Final = (
    r"\b(write|edit|create|touch|mkdir|move|rename|copy|patch|apply|format)\b",
    r"\b(commit|stage|checkout|branch|merge|rebase|stash)\b",
    r"\b(install|migrate|generate|scaffold|build|compile|bundle)\b",
)

_READ_ONLY: Final = (
    r"\b(read|cat|head|tail|open|view|show|print|inspect)\b",
    r"\b(ls|list|dir|find|grep|search|rg|glob|locate)\b",
    r"\b(status|diff|log|blame|describe)\b",
    r"\b(test|pytest|lint|typecheck|check|verify|analyse|analyze)\b",
)

# Order matters. A tool called "delete_deployment" is destructive first, and a name that
# matches both a write and a removal must resolve to the more dangerous reading.
_RULES: Final = (
    (SideEffect.DESTRUCTIVE, _DESTRUCTIVE),
    (SideEffect.EXTERNAL, _EXTERNAL),
    (SideEffect.LOCAL_WRITE, _LOCAL_WRITE),
    (SideEffect.READ_ONLY, _READ_ONLY),
)

_COMPILED: Final = tuple(
    (effect, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for effect, patterns in _RULES
)

REPEATABLE: Final = frozenset({SideEffect.READ_ONLY, SideEffect.LOCAL_WRITE})
"""Classes a replay may re-run on its own, given a sandbox."""

NEVER_AUTOMATIC: Final = frozenset({SideEffect.EXTERNAL, SideEffect.DESTRUCTIVE})
"""Classes excluded from automatic replay outright, per design document 10.2."""


def _to_words(tool_name: str) -> str:
    """Split a tool name into space-separated words.

    Underscores are word characters to a regular expression, so ``\\bread\\b`` does not
    match ``read_file``. Separators therefore become spaces rather than underscores, and
    camelCase is split too, so ``sendEmail`` and ``send_email`` classify alike.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tool_name)
    return re.sub(r"[^A-Za-z0-9]+", " ", spaced).strip().lower()


def classify(tool_name: str) -> SideEffect:
    """Classify a tool by name, resolving to the most dangerous plausible reading."""
    normalized = _to_words(tool_name)
    for effect, patterns in _COMPILED:
        if any(pattern.search(normalized) for pattern in patterns):
            return effect
    return SideEffect.UNKNOWN


def is_repeatable(tool_name: str) -> bool:
    """Whether a replay may re-run this tool without asking."""
    return classify(tool_name) in REPEATABLE
