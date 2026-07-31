"""Runtime adapter toolkit: record well-formed traces from any runtime."""

from runopsy_adapter import hermes, launch
from runopsy_adapter.contract import (
    ContractViolationError,
    assert_adapter_contract,
    describe_contract,
    warn_about_state_keys,
)
from runopsy_adapter.launch import LaunchResult, build_command, find_executable
from runopsy_adapter.recorder import EventSink, ListSink, PayloadStore, RunRecorder
from runopsy_adapter.secrets import ScanResult, contains_secret, scan
from runopsy_adapter.shell import ADAPTER_NAME, StepOutcome, record_steps

__version__ = "0.1.0"

__all__ = [
    "ADAPTER_NAME",
    "ContractViolationError",
    "EventSink",
    "LaunchResult",
    "ListSink",
    "PayloadStore",
    "RunRecorder",
    "ScanResult",
    "StepOutcome",
    "__version__",
    "assert_adapter_contract",
    "build_command",
    "contains_secret",
    "describe_contract",
    "find_executable",
    "hermes",
    "launch",
    "record_steps",
    "scan",
    "warn_about_state_keys",
]
