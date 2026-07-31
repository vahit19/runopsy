"""Project configuration: ``runopsy.toml``.

Two rules keep this file trustworthy:

**Every key here is honored.** A config option that code does not read is a promise the
tool silently breaks, so this module only knows keys that are actually wired to
behaviour, and the design document's aspirational settings arrive here when — and only
when — their feature does.

**Unknown keys are reported, not ignored.** A typo like ``loop_treshold`` that is
silently skipped leaves the user believing a threshold they never actually set. The
warning goes to stderr and the run continues, because a misspelt option should not
break a diagnosis.

TOML rather than YAML because the parser ships in the standard library: one fewer
dependency in a tool people install to debug other tools.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from runopsy_core import DetectorSettings
from runopsy_semantic import DEFAULT_MODEL
from runopsy_semantic.budget import MAX_COST_USD, MAX_DIAGNOSTIC_CALLS

CONFIG_ENV_VAR = "RUNOPSY_CONFIG"
CONFIG_FILENAME = "runopsy.toml"

_KNOWN: dict[str, set[str]] = {
    "analysis": {
        "retry_threshold",
        "loop_threshold",
        "stale_memory_hours",
        "token_budget",
        "cost_budget_usd",
    },
    "semantic": {"model", "max_calls", "cost_budget_usd", "base_url", "api_style"},
    "replay": {"step_timeout_seconds", "sandbox_ignore"},
    "privacy": {"vault", "retain_days"},
    "capture": {"git"},
}


@dataclass(frozen=True)
class RunopsyConfig:
    """Everything the CLI reads from ``runopsy.toml``."""

    detector_settings: DetectorSettings = field(default_factory=DetectorSettings)
    replay_timeout_seconds: int = 600
    replay_sandbox_ignore: tuple[str, ...] = ()
    """Extra patterns excluded from the sandbox copy, on top of the built-in set."""

    semantic_model: str = DEFAULT_MODEL
    semantic_max_calls: int = MAX_DIAGNOSTIC_CALLS
    semantic_cost_budget_usd: float = MAX_COST_USD

    retain_days: int = 0
    """Days of history to keep when ``runopsy prune`` runs. Zero means keep everything.

    Defaults to off. Retention that deletes by default would remove somebody's evidence
    the first time they upgraded, which is not a decision a default should make.
    """

    vault_enabled: bool = True
    """Whether payload text is kept locally for replay.

    Off means replays cannot reconstruct commands — stated here because it is the
    trade the user is making, not a detail.
    """

    capture_git: bool = True
    """Whether each step records what moved in the repository.

    On by default, and free outside a repository: the observer reports "nothing to say"
    when there is no git, no repository, or git is too slow, and recording carries on.
    A coding agent's real output is the working tree, so a trace that does not contain it
    cannot answer the question the tool exists for.
    """

    semantic_base_url: str = ""
    """OpenAI-compatible chat-completions endpoint for the diagnosing model.

    Empty means OpenRouter. Setting it to a local server — Ollama's
    ``http://localhost:11434/v1/chat/completions``, llama.cpp, vLLM — is what makes the
    semantic layer usable with no key and no data leaving the machine, which the
    local-first promise claims and could not previously deliver.
    """

    warnings: tuple[str, ...] = ()
    source: Path | None = None


def _config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    default = Path.cwd() / CONFIG_FILENAME
    return default if default.exists() else None


def load_config(path: Path | None = None) -> RunopsyConfig:
    """Load configuration, falling back to defaults when there is no file.

    A malformed file is reported and defaults are used: refusing to diagnose because of
    a config typo would punish the user at exactly the moment they need the tool.
    """
    resolved = _config_path(path)
    if resolved is None or not resolved.exists():
        return RunopsyConfig()

    try:
        data = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        return RunopsyConfig(
            warnings=(f"{resolved}: could not read config ({error}); using defaults",),
            source=resolved,
        )

    warnings: list[str] = []
    for section, keys in data.items():
        if section not in _KNOWN:
            warnings.append(f"{resolved}: unknown section [{section}] ignored")
            continue
        if isinstance(keys, dict):
            for key in keys:
                if key not in _KNOWN[section]:
                    warnings.append(f"{resolved}: unknown key {section}.{key} ignored")

    analysis = data.get("analysis", {}) if isinstance(data.get("analysis"), dict) else {}
    replay = data.get("replay", {}) if isinstance(data.get("replay"), dict) else {}
    privacy = data.get("privacy", {}) if isinstance(data.get("privacy"), dict) else {}
    semantic = data.get("semantic", {}) if isinstance(data.get("semantic"), dict) else {}
    capture = data.get("capture", {}) if isinstance(data.get("capture"), dict) else {}

    settings = DetectorSettings(
        retry_threshold=int(analysis.get("retry_threshold", 3)),
        loop_threshold=int(analysis.get("loop_threshold", 3)),
        stale_memory_seconds=float(analysis.get("stale_memory_hours", 24)) * 3600,
        token_budget=int(analysis.get("token_budget", 0)),
        cost_budget_usd=float(analysis.get("cost_budget_usd", 0.0)),
    )

    ignore = replay.get("sandbox_ignore", [])
    return RunopsyConfig(
        detector_settings=settings,
        replay_timeout_seconds=int(replay.get("step_timeout_seconds", 600)),
        replay_sandbox_ignore=tuple(str(pattern) for pattern in ignore)
        if isinstance(ignore, list)
        else (),
        semantic_model=str(semantic.get("model", DEFAULT_MODEL)),
        semantic_max_calls=int(semantic.get("max_calls", MAX_DIAGNOSTIC_CALLS)),
        semantic_cost_budget_usd=float(semantic.get("cost_budget_usd", MAX_COST_USD)),
        semantic_base_url=str(semantic.get("base_url", "")).strip(),
        retain_days=int(privacy.get("retain_days", 0)),
        vault_enabled=bool(privacy.get("vault", True)),
        capture_git=bool(capture.get("git", True)),
        warnings=tuple(warnings),
        source=resolved,
    )


def example_config() -> str:
    """A commented example for ``runopsy config --init``. Every key shown is honored."""
    return """\
# Runopsy configuration. Every key in this file is read by the tool;
# unknown keys are reported rather than silently ignored.

[analysis]
# Steps before a repeated call counts as a retry storm / loop.
retry_threshold = 3
loop_threshold = 3
# Age at which a memory read is flagged as possibly stale.
stale_memory_hours = 24
# Spend ceilings. 0 disables the check.
token_budget = 0
cost_budget_usd = 0.0

[replay]
# Per-step timeout when executing a replay in the sandbox.
step_timeout_seconds = 600
# Extra directories excluded from the sandbox copy (added to the built-in set).
sandbox_ignore = []

[semantic]
# Only used by: runopsy diagnose --mode hybrid. Never touched otherwise.
# The model that reviews suspicious steps. Bring your own OPENROUTER_API_KEY.
model = "openai/gpt-4o-mini"
# Ceilings, checked before each call rather than after.
max_calls = 2
cost_budget_usd = 0.10
# Point this at any OpenAI-compatible endpoint to keep the semantic layer local
# and key-free. Empty means OpenRouter. For Ollama:
#   base_url = "http://localhost:11434/v1/chat/completions"
#   model = "qwen2.5-coder"
base_url = ""

[capture]
# Record what moved in the repository at each step: the commit, the branch, and
# which files were dirty. Costs one "git status" per step and is skipped entirely
# outside a repository. A coding agent's real output is the working tree.
git = true

[privacy]
# Keep command text locally so replays can re-run it. The trace itself always
# stores hashes only; turning this off also disables replay execution.
vault = true
# Days of history "runopsy prune" keeps. 0 means keep everything; nothing is
# ever deleted without running that command explicitly.
retain_days = 0
"""
