"""Local storage layout.

Everything Runopsy records lives under a single directory so a user can inspect it,
back it up, or delete it in one action. Knowing exactly where your traces are, and
being able to remove them without hunting through application data directories, is part
of the local-first promise rather than an implementation detail.
"""

from __future__ import annotations

import contextlib
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

    def run_dir(self, run_id: str) -> Path:
        """Everything belonging to one run: its journal, and its sequence counter."""
        return self.runs_dir / _require_safe_run_id(run_id)

    def journal(self, run_id: str) -> Path:
        """Path to one run's event journal."""
        return self.run_dir(run_id) / "events.jsonl"

    def ensure(self) -> None:
        """Create the directory structure if it is not already present."""
        for directory in (self.root, self.runs_dir, self.artifacts_dir, self.diagnoses_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._exclude_from_version_control()

    def _exclude_from_version_control(self) -> None:
        """Make the store invisible to the repository it sits inside.

        The default store is ``.runopsy`` beside the project, which puts it inside the
        working tree of the repository the agent is editing. That is not merely untidy.
        Measured while recording a real run: the agent's own ``git add -A`` swept the
        store in and then failed with exit 128, because DuckDB held the index open and
        git could not read it. Runopsy had changed the outcome of the run it was
        observing, which is the one thing an observer must never do.

        A ``.gitignore`` containing ``*`` inside the store excludes it and everything
        under it without touching the user's own ignore file — their repository is not
        ours to edit. Written once, never overwritten: if somebody has deliberately
        changed it, that decision stands.
        """
        marker = self.root / ".gitignore"
        if marker.exists():
            return
        with contextlib.suppress(OSError):
            marker.write_text(
                "# Runopsy's local store. Excluded so the repository being recorded\n"
                "# does not sweep it into a commit, and so recording cannot alter the\n"
                "# run it is observing.\n"
                "*\n",
                encoding="utf-8",
            )

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
