"""Local payload vault: the text behind the hashes.

The trace stores prompts, arguments and outputs as digests so it can be shared without
carrying source code or secrets. But a replay has to *re-run* a command, and a hash
cannot be executed. The vault squares that: payload text is kept content-addressed on
the user's own machine, keyed by the same digest the trace carries, and nothing here is
ever included in an export.

Two rules keep it safe:

- **Secrets never enter, even locally.** Text is scanned before storage and the redacted
  form is what gets written. An entry that lost content to redaction is marked, and the
  replay executor refuses to run it rather than executing a command with ``[REDACTED]``
  spliced into it.
- **The key is the digest of the original text**, not of the redacted form, so a lookup
  from a trace event always finds its payload — while the raw secret itself exists
  nowhere on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from runopsy_core.hashing import DIGEST_PATTERN, hash_text


class PayloadEntry:
    """One stored payload."""

    __slots__ = ("redacted", "text")

    def __init__(self, text: str, *, redacted: bool) -> None:
        self.text = text
        self.redacted = redacted

    @property
    def executable(self) -> bool:
        """Whether the stored text is the real payload rather than a censored one."""
        return not self.redacted


class PayloadLookup(Protocol):
    """What the replay executor needs: digest in, payload out."""

    def get(self, digest: str) -> PayloadEntry | None: ...


class PayloadVault:
    """Content-addressed local storage for payload text."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        if not DIGEST_PATTERN.match(digest):
            msg = f"not a digest: {digest!r}"
            raise ValueError(msg)
        return self.root / f"{digest.removeprefix('sha256:')}.json"

    def put(self, original_text: str, *, stored_text: str | None = None) -> str:
        """Store a payload, returning the digest of the original text.

        ``stored_text`` is what actually lands on disk — the caller passes the redacted
        form when a scan found something. Writing is idempotent: the same original text
        always produces the same file.
        """
        digest = hash_text(original_text)
        content = original_text if stored_text is None else stored_text
        path = self._path(digest)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"text": content, "redacted": content != original_text}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return digest

    def get(self, digest: str) -> PayloadEntry | None:
        """Fetch a payload by digest, or ``None`` when it was never stored."""
        path = self._path(digest)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return PayloadEntry(str(data.get("text", "")), redacted=bool(data.get("redacted")))

    def __contains__(self, digest: str) -> bool:
        return DIGEST_PATTERN.match(digest) is not None and self._path(digest).exists()
