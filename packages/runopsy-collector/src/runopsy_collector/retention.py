"""Deleting old traces.

Section 16.1 lists ``retain_raw_days`` among the safe-mode defaults, and the reasoning
is privacy rather than disk: a trace is a record of what someone's repository looked
like while they worked, and keeping it forever by default makes the tool a slowly
growing liability on their machine.

Three rules, because this is the only code in Runopsy that destroys evidence:

- **Nothing expires on its own.** Retention runs when a person asks for it. Deleting a
  user's data as a side effect of a diagnosis would be indefensible however clearly the
  policy was documented.
- **A plan comes first.** ``plan_prune`` reports exactly what would go and how old it
  is, and the caller decides. The same shape as replay: propose, then act.
- **Refuse to guess.** A run with no recorded start has no age, so it is never
  considered expired. Deleting something because its timestamp was missing would turn a
  recording bug into data loss.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from runopsy_collector.paths import StorePaths
from runopsy_collector.store import EventStore, RunSummary


@dataclass(frozen=True)
class PrunePlan:
    """What retention would remove, and what it deliberately would not."""

    older_than: datetime
    expiring: tuple[RunSummary, ...] = ()
    kept: tuple[RunSummary, ...] = ()
    undated: tuple[RunSummary, ...] = field(default_factory=tuple)
    """Runs with no recorded start. Never expired — an unknown age is not an old age."""

    @property
    def is_empty(self) -> bool:
        return not self.expiring

    @property
    def expiring_events(self) -> int:
        return sum(run.event_count for run in self.expiring)

    def describe(self) -> str:
        if self.is_empty:
            return "nothing has expired"
        return (
            f"{len(self.expiring)} run(s), {self.expiring_events} events, "
            f"recorded before {self.older_than:%Y-%m-%d}"
        )


@dataclass(frozen=True)
class PruneResult:
    """What was actually removed."""

    removed_runs: tuple[str, ...] = ()
    removed_events: int = 0
    vault_entries_removed: int = 0

    @property
    def removed_anything(self) -> bool:
        return bool(self.removed_runs)


def plan_prune(store: EventStore, retain_days: int, *, now: datetime | None = None) -> PrunePlan:
    """Work out which runs are past the retention window.

    ``now`` is a parameter so the decision is testable and reproducible; retention that
    depends on an ambient clock cannot be verified.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=max(retain_days, 0))

    expiring: list[RunSummary] = []
    kept: list[RunSummary] = []
    undated: list[RunSummary] = []

    for run in store.runs():
        if run.started_at is None:
            undated.append(run)
        elif run.started_at < cutoff:
            expiring.append(run)
        else:
            kept.append(run)

    return PrunePlan(
        older_than=cutoff,
        expiring=tuple(expiring),
        kept=tuple(kept),
        undated=tuple(undated),
    )


def apply_prune(paths: StorePaths, store: EventStore, plan: PrunePlan) -> PruneResult:
    """Remove the runs in ``plan``: journal, index rows and vault payloads.

    Vault entries are removed only when no surviving run still references them, because
    payloads are content-addressed and two runs that issued the same command share one
    entry. Deleting it for the older run would silently break replay for the newer one.
    """
    if plan.is_empty:
        return PruneResult()

    doomed = {run.run_id for run in plan.expiring}
    referenced_after = store.payload_digests(exclude_runs=doomed)
    referenced_before = store.payload_digests(only_runs=doomed)
    orphaned = referenced_before - referenced_after

    removed_events = 0
    for run_id in doomed:
        removed_events += store.delete_run(run_id)
        run_directory = paths.journal(run_id).parent
        if run_directory.exists():
            shutil.rmtree(run_directory, ignore_errors=True)

    vault_removed = 0
    for digest in orphaned:
        path = paths.vault_dir / f"{digest.removeprefix('sha256:')}.json"
        if path.exists():
            path.unlink(missing_ok=True)
            vault_removed += 1

    return PruneResult(
        removed_runs=tuple(sorted(doomed)),
        removed_events=removed_events,
        vault_entries_removed=vault_removed,
    )
