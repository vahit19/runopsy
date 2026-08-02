"""Read Inspect AI eval logs into Runopsy traces."""

from runopsy_inspect.convert import RUNTIME, log_to_runs, run_id_for, sample_to_events

__version__ = "0.1.4"

__all__ = [
    "RUNTIME",
    "__version__",
    "log_to_runs",
    "run_id_for",
    "sample_to_events",
]
