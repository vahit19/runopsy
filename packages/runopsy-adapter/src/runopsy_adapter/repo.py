"""What the repository looked like at each step.

A coding agent's real output is the working tree, and until now nothing recorded it.
The trace held what the agent *ran* and what came back, so "which step broke the repo"
could only be inferred from tool arguments — and an argument hash cannot see a file. The
first real sessions made that concrete: the loop detector fired on an edit-verify cycle
precisely because the command never changed while the file on disk did.

This observes the repository around each recorded step and reports what moved. Three
rules shape it.

**It never breaks the run it observes.** Every failure path returns "nothing to say":
no repository, no git binary, a command that hangs, output in a format this does not
recognise. A diagnosis tool that stops an agent because `git` was missing has done more
damage than the failure it was watching for.

**Two subprocesses per step.** ``git status --porcelain=v2 --branch`` carries the commit,
the branch and every changed path in one call, which is why that format is used rather
than a ``rev-parse`` beside a plain status. ``git diff --numstat`` is the second, and it
earns its place: status says *which* files are dirty and never changes while an agent
edits the same file five times, so without line counts a trace cannot tell "this step
changed the code" from "this step only ran the tests".

**Only identity-like values become state deltas.** ``git.head`` and ``git.branch`` go
into ``state_delta``, where the flapping detector reads them, and they are emitted only
when they actually change. The set of dirty files deliberately does not: an agent that
edits, tests, reverts and edits again returns the tree to the same modified state
repeatedly, which is ordinary behaviour and would have the flapping detector reporting it
as disagreement. The zero-false-positive invariant is worth more than the extra signal.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from runopsy_core.schema import StateChange

# Long enough for a cold index on a large repository, short enough that a wedged git
# cannot hold up the agent. Exceeding it is treated as "nothing to say", not an error.
STATUS_TIMEOUT_SECONDS = 10.0

# A ceiling on how many paths one step records. A checkout or a generated-file sweep can
# touch thousands, and a trace is meant to stay readable; the count is kept in full, so
# nothing is silently understated.
MAX_RECORDED_PATHS = 50

_CHANGE_PREFIXES = ("1 ", "2 ", "u ")


@dataclass(frozen=True)
class RepositoryState:
    """The repository as it stood at one moment."""

    head: str | None = None
    branch: str | None = None
    changed_paths: tuple[str, ...] = ()
    changed_count: int = 0
    untracked_count: int = 0
    edits: dict[str, tuple[int, int]] = field(default_factory=dict)
    """Lines added and removed per path, against the last commit."""

    @property
    def dirty(self) -> bool:
        return bool(self.changed_count or self.untracked_count)

    def values(self) -> dict[str, object]:
        """The snapshot as recorded on a ``state_snapshot`` event."""
        recorded: dict[str, object] = {
            "git.head": self.head,
            "git.branch": self.branch,
            "git.dirty": self.dirty,
            "git.changed_count": self.changed_count,
            "git.untracked_count": self.untracked_count,
        }
        if self.changed_paths:
            recorded["git.changed_paths"] = list(self.changed_paths)
        if self.edits:
            # "+12 -3 in src/api.py" is the sentence a reader wants; a bare path list
            # cannot say whether a step rewrote a file or merely touched it.
            recorded["git.edits"] = {
                path: {"added": added, "removed": removed}
                for path, (added, removed) in sorted(self.edits.items())
            }
        if self.changed_count > len(self.changed_paths):
            # Say so rather than let a truncated list read as the whole story.
            recorded["git.paths_truncated"] = True
        return recorded


@dataclass
class _Cursor:
    """The last values this run reported, so only changes are emitted."""

    head: str | None = None
    branch: str | None = None
    seen: bool = False
    extra: dict[str, str] = field(default_factory=dict)


def read_repository(cwd: Path | None = None) -> RepositoryState | None:
    """Observe the repository containing ``cwd``, or ``None`` when there is not one.

    ``None`` is the answer for every unhappy path — outside a repository, git absent,
    git too slow, output unparseable — because each of them means the same thing to a
    caller: there is nothing to record here, carry on.
    """
    completed = _git("status", "--porcelain=v2", "--branch", cwd=cwd)
    if completed is None or completed.returncode != 0:
        # Most often "not a git repository", which is a normal way to run an agent.
        return None

    state = _parse_status(completed.stdout)
    return replace(state, edits=_read_edits(cwd))


def _git(*arguments: str, cwd: Path | None) -> subprocess.CompletedProcess[str] | None:
    """Run one git command, or report that it could not be run.

    The executable is resolved through ``PATH`` once, explicitly, rather than left for
    the process launcher to find. On Windows that also settles *which* git answers when
    several are installed, which is the difference between observing the repository the
    agent is working in and observing a different one.
    """
    executable = shutil.which("git")
    if executable is None:
        return None
    try:
        return subprocess.run(
            [executable, *arguments],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _read_edits(cwd: Path | None) -> dict[str, tuple[int, int]]:
    """Lines added and removed per file, against the last commit.

    A second subprocess, and worth it. ``git status`` reports *which* files are dirty but
    not how dirty, so an agent that edits the same file five times produces five
    identical statuses — and the trace could not distinguish "this step changed the code"
    from "this step only ran the tests". Line counts move every time the content does,
    which is what makes each edit visible as its own event.

    Binary files report ``-`` for both counts in this format; they are recorded as zero
    rather than skipped, so the path still appears as touched.
    """
    completed = _git("diff", "--numstat", "HEAD", cwd=cwd)
    if completed is None:
        return {}
    if completed.returncode != 0:
        # No commit yet, most often. There is nothing to diff against, which is not an
        # error worth propagating — the status output still describes the tree.
        return {}

    edits: dict[str, tuple[int, int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        added, removed, path = fields[0], fields[1], fields[-1]
        edits[path.strip()] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    return edits


def capture_patch(cwd: Path | None = None) -> str | None:
    """The working tree's uncommitted changes as a patch, or ``None`` if there are none.

    This is what makes a checkpoint restorable rather than merely labelled. A commit sha
    says where the run was; the patch says what the agent had done on top of it, which
    for a coding agent is most of what there is to restore.

    Taken as a patch rather than by writing into the user's repository. ``git stash
    create`` would be shorter and would leave objects in a repository Runopsy was asked
    to watch, not to write to — the same rule that made the store exclude itself from the
    agent's commits. A patch goes into Runopsy's own vault, is scanned for secrets like
    every other payload, and can be thrown away without touching anything of theirs.

    Binary changes are deliberately included (``--binary``): a checkpoint that silently
    dropped them would restore a tree that looks right and is not.
    """
    completed = _git("diff", "--binary", "HEAD", cwd=cwd)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout or None


def _parse_status(output: str) -> RepositoryState:
    """Read ``--porcelain=v2 --branch`` into a state.

    Version 2 of the format is used because it carries the commit and branch as header
    lines, which is what removes the need for a second ``git rev-parse`` call. Header
    values git reports as unknown — ``(initial)`` before the first commit, ``(detached)``
    on a detached HEAD — are recorded as absent rather than as those literal strings.
    """
    head: str | None = None
    branch: str | None = None
    paths: list[str] = []
    changed = 0
    untracked = 0

    for line in output.splitlines():
        if line.startswith("# branch.oid "):
            value = line[len("# branch.oid ") :].strip()
            head = None if value.startswith("(") else value
        elif line.startswith("# branch.head "):
            value = line[len("# branch.head ") :].strip()
            branch = None if value.startswith("(") else value
        elif line.startswith("? "):
            untracked += 1
            if len(paths) < MAX_RECORDED_PATHS:
                paths.append(line[2:].strip())
        elif line.startswith(_CHANGE_PREFIXES):
            changed += 1
            path = _path_from_change(line)
            if path and len(paths) < MAX_RECORDED_PATHS:
                paths.append(path)

    return RepositoryState(
        head=head,
        branch=branch,
        changed_paths=tuple(paths),
        changed_count=changed,
        untracked_count=untracked,
    )


CURSOR_NAME = ".gitcursor"


@dataclass(frozen=True)
class Observation:
    """What one look at the repository is worth recording."""

    state: RepositoryState
    deltas: dict[str, StateChange]
    worth_a_snapshot: bool


class RepositoryWatch:
    """Follows one run's repository across the processes that record it.

    A hook-based adapter is a fresh process per event, so "has this changed since the
    last step?" cannot be answered from memory. Given a ``run_dir``, the previous
    observation is kept in a small file there — the same place the sequence counter
    lives, and for the same reason: derived data about one run, cheap to lose and cheap
    to rebuild by simply observing again.

    Without one, the cursor stays in memory. That is the right answer for an adapter
    that records a whole run inside a single process, and it keeps this class usable
    without a store on disk at all.
    """

    def __init__(self, run_dir: Path | None = None) -> None:
        self.cursor_path = run_dir / CURSOR_NAME if run_dir is not None else None
        self._remembered = _Cursor()

    def observe(self, cwd: Path | None = None) -> Observation | None:
        """Look at the repository and work out what has moved since the last look."""
        state = read_repository(cwd)
        if state is None:
            return None

        previous = self._read_cursor()
        deltas: dict[str, StateChange] = {}
        # Only on change. Emitting `git.head` on every step would give the flapping
        # detector a long run of one value followed by a long run of another, which is
        # what an ordinary mid-run commit looks like — and it would report it.
        if state.head != previous.head:
            deltas["git.head"] = StateChange(before=previous.head, after=state.head)
        if state.branch != previous.branch:
            deltas["git.branch"] = StateChange(before=previous.branch, after=state.branch)

        fingerprint = _fingerprint(state)
        worth_a_snapshot = (
            not previous.seen or bool(deltas) or fingerprint != previous.extra.get("tree")
        )

        self._write_cursor(state, fingerprint)
        return Observation(state=state, deltas=deltas, worth_a_snapshot=worth_a_snapshot)

    def _read_cursor(self) -> _Cursor:
        if self.cursor_path is None:
            return self._remembered
        try:
            stored = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _Cursor()
        if not isinstance(stored, dict):
            return _Cursor()
        return _Cursor(
            head=stored.get("head"),
            branch=stored.get("branch"),
            seen=True,
            extra={"tree": str(stored.get("tree") or "")},
        )

    def _write_cursor(self, state: RepositoryState, fingerprint: str) -> None:
        """Best effort. Losing the cursor costs one redundant snapshot, never a step."""
        self._remembered = _Cursor(
            head=state.head, branch=state.branch, seen=True, extra={"tree": fingerprint}
        )
        if self.cursor_path is None:
            return
        with contextlib.suppress(OSError):
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_path.write_text(
                json.dumps({"head": state.head, "branch": state.branch, "tree": fingerprint}),
                encoding="utf-8",
            )


def _fingerprint(state: RepositoryState) -> str:
    """A short digest of which paths are dirty, for "did anything move?".

    Compared rather than stored in the trace, so a digest is enough and the paths
    themselves stay out of a file that is not the journal.
    """
    material = "\n".join(state.changed_paths) + f"|{state.changed_count}|{state.untracked_count}"
    # The line counts are part of it, so editing the same file twice reads as two
    # separate changes rather than one unchanging "app.py is dirty".
    material += "|" + ";".join(
        f"{path}:{added}:{removed}" for path, (added, removed) in sorted(state.edits.items())
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _path_from_change(line: str) -> str | None:
    """The path out of an ordinary, renamed or unmerged entry.

    A rename entry puts both paths in one field separated by a tab; the new path comes
    first and is the one worth recording, since it is where the content now lives.
    """
    fields = line.split(" ")
    marker = fields[0]
    # Ordinary entries carry 8 leading fields before the path, renames 9, unmerged 10.
    offset = {"1": 8, "2": 9, "u": 10}.get(marker)
    if offset is None or len(fields) <= offset:
        return None
    path = " ".join(fields[offset:]).strip()
    return path.split("\t", 1)[0] or None
