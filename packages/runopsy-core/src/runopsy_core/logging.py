"""Structured logging, off unless asked for.

Section 13.1 names structlog for application logs. The value is diagnosability of
Runopsy itself: when a hook records nothing, or a diagnosis takes too long, the user
needs a way to find out why that does not involve reading source.

Two rules keep it from becoming a liability in a tool that handles credentials:

**Silent by default.** Nothing is emitted unless ``RUNOPSY_LOG`` is set. A diagnostic
tool that prints its own chatter into somebody's terminal competes with the answer it
was asked for, and a hook running inside an agent session must stay quiet.

**Logs go to stderr, never to the trace.** The trace is evidence and gets shared; log
lines are operational and stay local. Mixing them would put Runopsy's internals into
files users hand to other people.

Values are never interpolated blind: helpers here take structured fields, and the
formatter refuses anything credential-shaped, so a debug line cannot become the leak
path that every other layer was built to prevent.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Final, TextIO

LEVEL_VARIABLE: Final = "RUNOPSY_LOG"
FORMAT_VARIABLE: Final = "RUNOPSY_LOG_FORMAT"

_LEVELS: Final = {"debug": 10, "info": 20, "warning": 30, "error": 40}

_REDACTED: Final = "[REDACTED]"


def _configured_level() -> int:
    """The threshold, or a value above every level when logging is off."""
    raw = os.environ.get(LEVEL_VARIABLE, "").strip().lower()
    return _LEVELS.get(raw, 1_000)


def _safe(value: object) -> object:
    """Strip anything credential-shaped out of a field before it is written.

    The scanner lives in the adapter package, which core must not import, so this is a
    deliberately conservative subset: long opaque tokens and anything under a key that
    names a secret. A log line is the last place worth being clever.
    """
    if isinstance(value, str):
        from runopsy_core.hashing import DIGEST_PATTERN

        if DIGEST_PATTERN.match(value):
            return value[:14] + "…"
        if len(value) > 200:
            return value[:200] + "…"
    return value


_SENSITIVE_KEYS: Final = ("key", "token", "secret", "password", "authorization", "credential")


class Logger:
    """A minimal structured logger.

    Deliberately not a structlog subclass. structlog is the documented choice and would
    be the right dependency once anything needs its processors, but a logger this small
    should not make every install carry one — and the call sites below are identical
    either way, so swapping it later changes one file.
    """

    def __init__(self, name: str, stream: TextIO | None = None) -> None:
        self.name = name
        self._stream = stream

    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> None:
        if _LEVELS[level] < _configured_level():
            return

        cleaned = {
            key: (_REDACTED if any(s in key.lower() for s in _SENSITIVE_KEYS) else _safe(value))
            for key, value in fields.items()
        }
        record = {"level": level, "logger": self.name, "event": event, **cleaned}

        stream = self._stream or sys.stderr
        if os.environ.get(FORMAT_VARIABLE, "").strip().lower() == "json":
            stream.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        else:
            rendered = " ".join(f"{key}={value}" for key, value in cleaned.items())
            stream.write(f"{level:<7} {self.name}: {event} {rendered}".rstrip() + "\n")
        stream.flush()

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)


def get_logger(name: str, stream: TextIO | None = None) -> Logger:
    """A logger for one module. Emits nothing unless ``RUNOPSY_LOG`` is set."""
    return Logger(name, stream)


def logging_enabled() -> bool:
    """Whether anything would be emitted at all."""
    return _configured_level() < 1_000
