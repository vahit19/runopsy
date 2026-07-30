"""Local, append-only ingest and queryable storage for Runopsy traces."""

from runopsy_collector.collector import Collector
from runopsy_collector.journal import EventJournal, JournalCorruptionError, serialize
from runopsy_collector.paths import StorePaths
from runopsy_collector.store import EventStore, RunSummary

__version__ = "0.1.0"

__all__ = [
    "Collector",
    "EventJournal",
    "EventStore",
    "JournalCorruptionError",
    "RunSummary",
    "StorePaths",
    "__version__",
    "serialize",
]
