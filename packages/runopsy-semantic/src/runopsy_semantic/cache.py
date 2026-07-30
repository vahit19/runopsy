"""Caching verdicts so the same question is never paid for twice.

Section 12.1 lists ``cache_by_trace_hash`` among the budget controls, and it is the one
that matters most in practice: people re-run ``diagnose`` while reading its output. The
key covers the evidence, the model and the prompt version, so a cached answer always
answered exactly the question being asked now.

Stored beside the traces, on the user's own machine, holding only what the model
returned — never the payload that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class VerdictCache:
    """Content-addressed storage for model verdicts."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / f"{key.removeprefix('sha256:')}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """Return a stored verdict, or ``None``.

        A corrupt entry reads as a miss. Re-asking costs a fraction of a cent; guessing
        at a damaged verdict costs credibility.
        """
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, verdict: dict[str, Any]) -> None:
        """Store a verdict, ignoring write failures.

        An uncacheable answer is a performance problem, not a correctness one, and it
        must not break a diagnosis that has already succeeded.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(
                json.dumps(verdict, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            return

    def __contains__(self, key: str) -> bool:
        return self._path(key).exists()
