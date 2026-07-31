"""Local storage layout.

Everything Runopsy records lives under a single directory so a user can inspect it,
back it up, or delete it in one action. Knowing exactly where your traces are, and
being able to remove them without hunting through application data directories, is part
of the local-first promise rather than an implementation detail.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

HOME_ENV_VAR: Final = "RUNOPSY_HOME"
DEFAULT_DIRECTORY_NAME: Final = ".runopsy"

_SAFE_RUN_ID: Final = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _require_safe_run_id(run_id: str) -> str:
    """Reject run ids that could escape the store directory.

    Run ids arrive from runtime adapters, which are third-party code. A value like
    ``../../.ssh`` would otherwise turn a trace write into an arbitrary file write.
    """
    if not _SAFE_RUN_ID.match(run_id):
        msg = f"unsafe run id: {run_id!r}"
        raise ValueError(msg)
    return run_id


@dataclass(frozen=True)
class StorePaths:
    """Resolved locations for one Runopsy store."""

    root: Path

    @classmethod
    def resolve(cls, root: str | Path | None = None) -> StorePaths:
        """Pick the store location.

        Explicit argument wins, then ``RUNOPSY_HOME``, then ``.runopsy`` beside the
        project being worked on. The project-local default keeps one repository's traces
        from mixing into another's, which matters because a diagnosis compares runs.

        A string is accepted as well as a ``Path``. The annotation said ``Path`` and
        nothing enforced it at runtime, so ``Collector.open("./runs")`` — the most
        natural thing for a library user to write — died on
        ``'str' object has no attribute 'expanduser'``, several frames from anything
        they wrote. Accepting both costs one call and removes an error message that
        explains nothing.
        """
        if root is not None:
            return cls(root=Path(root).expanduser().resolve())
        from_env = os.environ.get(HOME_ENV_VAR)
        if from_env:
            return cls(root=Path(from_env).expanduser().resolve())
        return cls(root=(Path.cwd() / DEFAULT_DIRECTORY_NAME).resolve())

    @property
    def database(self) -> Path:
        """The DuckDB index. Derived data; safe to delete and rebuild."""
        return self.root / "runopsy.duckdb"

    @property
    def runs_dir(self) -> Path:
        """Per-run append-only journals. The source of truth."""
        return self.root / "runs"

    @property
    def artifacts_dir(self) -> Path:
        """Content-addressed file storage."""
        return self.root / "artifacts"

    @property
    def diagnoses_dir(self) -> Path:
        """Diagnosis bundles as JSON."""
        return self.root / "diagnoses"

    @property
    def vault_dir(self) -> Path:
        """Content-addressed payload text, local-only, never exported."""
        return self.root / "vault"

    def journal(self, run_id: str) -> Path:
        """Path to one run's event journal."""
        return self.runs_dir / _require_safe_run_id(run_id) / "events.jsonl"

    def ensure(self) -> None:
        """Create the directory structure if it is not already present."""
        for directory in (self.root, self.runs_dir, self.artifacts_dir, self.diagnoses_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def known_run_ids(self) -> tuple[str, ...]:
        """Run ids discoverable on disk, independent of the database."""
        if not self.runs_dir.exists():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.runs_dir.iterdir()
                if entry.is_dir() and (entry / "events.jsonl").exists()
            )
        )
