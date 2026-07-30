"""Where a provider key comes from, and where it is kept.

Resolution order, first match wins: an explicit flag, the process environment, the OS
keyring, then a developer ``.env``. Each source is reported by *name* so ``runopsy
doctor`` can tell a user where their key came from without ever showing it.

The keyring is the intended home for an end user. A key in a file is a key in backups,
in a synced folder, and eventually in a screenshot; the OS credential store is the one
place designed to hold it. But a keyring is not always available — headless Linux
without a Secret Service, a locked container — so an absent backend degrades to the
other sources instead of failing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SERVICE_NAME: Final = "runopsy"
OPENROUTER_ACCOUNT: Final = "openrouter"
API_KEY_VARIABLE: Final = "OPENROUTER_API_KEY"

DOTENV_FILENAME: Final = ".env"


@dataclass(frozen=True)
class ResolvedKey:
    """A credential and the name of where it was found. Never rendered with the value."""

    key: str
    source: str


class KeyringUnavailableError(RuntimeError):
    """No OS credential store could be reached."""


def _keyring() -> Any:
    """The keyring module, imported lazily.

    Deferred because importing it probes the OS credential backend, and a command that
    never needs a key should not pay that cost — or fail on a machine that has none.
    """
    try:
        import keyring
    except ImportError as error:  # pragma: no cover - dependency is declared
        msg = "the keyring package is not installed"
        raise KeyringUnavailableError(msg) from error
    return keyring


def read_keyring(account: str = OPENROUTER_ACCOUNT) -> str | None:
    """Read a key from the OS credential store, or ``None`` if there is none.

    A missing or broken backend reads as "no key" rather than raising: the caller's next
    move is the same either way, and a diagnosis should not fail because a container has
    no Secret Service.
    """
    try:
        value = _keyring().get_password(SERVICE_NAME, account)
    except Exception:
        return None
    return str(value) if value else None


def write_keyring(key: str, account: str = OPENROUTER_ACCOUNT) -> None:
    """Store a key in the OS credential store."""
    try:
        _keyring().set_password(SERVICE_NAME, account, key)
    except KeyringUnavailableError:
        raise
    except Exception as error:
        msg = f"the OS credential store refused the write: {error}"
        raise KeyringUnavailableError(msg) from error


def delete_keyring(account: str = OPENROUTER_ACCOUNT) -> bool:
    """Remove a stored key, reporting whether one was there."""
    try:
        keyring = _keyring()
        if keyring.get_password(SERVICE_NAME, account) is None:
            return False
        keyring.delete_password(SERVICE_NAME, account)
    except Exception:
        return False
    return True


def _read_dotenv(directory: Path) -> str | None:
    """Read the variable from a local ``.env``, without importing a parser.

    Deliberately minimal: this is a developer convenience, and a full dotenv
    implementation would invite people to treat it as the supported path.
    """
    path = directory / DOTENV_FILENAME
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().removeprefix("export ").strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if name.strip() == API_KEY_VARIABLE:
                cleaned = value.strip().strip("\"'")
                return cleaned or None
    except OSError:
        return None
    return None


def resolve(explicit: str | None = None, *, cwd: Path | None = None) -> ResolvedKey | None:
    """Find a key and say where it came from, or return ``None``.

    ``None`` is a normal outcome, not an error: every deterministic feature works without
    a provider, and that is the point of the offline-first design.
    """
    if explicit:
        return ResolvedKey(explicit, "command line")

    from_env = os.environ.get(API_KEY_VARIABLE)
    if from_env:
        return ResolvedKey(from_env, "environment")

    from_keyring = read_keyring()
    if from_keyring:
        return ResolvedKey(from_keyring, "OS keyring")

    from_file = _read_dotenv(cwd or Path.cwd())
    if from_file:
        return ResolvedKey(from_file, ".env file")

    return None


def describe_source(resolved: ResolvedKey | None) -> str:
    """What ``runopsy doctor`` prints. Contains no part of the key."""
    if resolved is None:
        return "not set (offline modes still work)"
    warning = " — prefer 'runopsy setup'" if resolved.source == ".env file" else ""
    return f"found via {resolved.source}{warning}"
