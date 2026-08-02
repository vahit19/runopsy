"""Local, append-only ingest and queryable storage for Runopsy traces."""

from runopsy_collector.collector import Collector
from runopsy_collector.journal import EventJournal, JournalCorruptionError, serialize
from runopsy_collector.paths import StorePaths
from runopsy_collector.retention import PrunePlan, PruneResult, apply_prune, plan_prune
from runopsy_collector.seal import Seal, SealState, SealVerdict
from runopsy_collector.sequence import SequenceAllocator
from runopsy_collector.store import EventStore, RunSummary, StoreFromTheFutureError, StoreVersions
from runopsy_collector.vault import PayloadEntry, PayloadLookup, PayloadVault

__version__ = "0.1.6"

__all__ = [
    "Collector",
    "EventJournal",
    "EventStore",
    "JournalCorruptionError",
    "PayloadEntry",
    "PayloadLookup",
    "PayloadVault",
    "PrunePlan",
    "PruneResult",
    "RunSummary",
    "Seal",
    "SealState",
    "SealVerdict",
    "SequenceAllocator",
    "StoreFromTheFutureError",
    "StorePaths",
    "StoreVersions",
    "__version__",
    "apply_prune",
    "plan_prune",
    "serialize",
]
