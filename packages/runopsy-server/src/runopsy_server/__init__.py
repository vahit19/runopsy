"""Local HTTP API over a Runopsy store."""

from typing import TYPE_CHECKING

__version__ = "0.1.7"

__all__ = ["__version__", "create_app", "default_store"]

if TYPE_CHECKING:
    from runopsy_server.app import create_app, default_store
else:

    def __getattr__(name: str) -> object:
        """Import the app lazily so ``runopsy --help`` does not pay for FastAPI.

        The CLI imports this package to offer ``runopsy ui``; loading a web framework
        for a command that only lists runs would make every other command slower.
        """
        if name in {"create_app", "default_store"}:
            from runopsy_server import app as _app

            return getattr(_app, name)
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
