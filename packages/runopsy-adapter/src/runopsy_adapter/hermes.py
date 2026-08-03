"""Hermes Agent adapter.

Hermes dispatches hooks as **shell commands**: it pipes a JSON payload to stdin and
reads an optional JSON decision from stdout. Integrating that way rather than as a
Python plugin is deliberate — it couples Runopsy to a documented wire format instead of
to Hermes' internal module layout, so an upgrade that reorganises its packages cannot
break recording.

Verified against hermes-agent 0.19.0, whose ``agent/shell_hooks.py`` documents the
protocol and per-event ``extra`` keys reproduced in the mapping below.

Two rules govern everything here:

**Never break the run being observed.** A diagnostic tool that takes down the agent it
was watching is worse than no tool. Mapping failures return ``None`` and the caller
exits cleanly; an unrecognised event is ignored rather than guessed at.

**Observe, do not intervene.** Every hook Runopsy registers is a recorder. ``pre_tool_call``
*can* block in Hermes, and Runopsy deliberately does not use that power in this release:
blocking is a policy decision that belongs to an explicit, reviewed configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from runopsy_adapter.hermes_plugin import EXECUTABLE_FILE as PLUGIN_EXECUTABLE_FILE
from runopsy_adapter.recorder import PayloadStore
from runopsy_adapter.secrets import scan
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    Event,
    HandoffEvent,
    HandoffPayload,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    SecurityMetadata,
    TokenUsage,
    ToolCallEvent,
    ToolPayload,
)

RUNTIME: Final = "hermes"

RECORDED_EVENTS: Final = (
    "on_session_start",
    "post_tool_call",
    "subagent_stop",
    "on_session_end",
)
"""Shell-hook events Runopsy registers for. All are observational.

This is the set hermes-agent 0.19.0 actually dispatches to *shell* hooks, per the
event table in its own ``agent/shell_hooks.py``: emitted from ``model_tools.py``,
``conversation_loop.py``, ``turn_finalizer.py`` and ``delegate_tool.py``.

``post_llm_call`` and ``on_session_finalize`` are deliberately absent. They exist in
Hermes, but only on the **plugin** path — ``hermes_cli.plugins.invoke_hook`` — and a
shell hook configured for them never fires during a real session. They are easy to
believe in because ``hermes hooks test post_llm_call`` calls the shell dispatcher
directly with a synthetic payload and reports success; the first live session recorded
33 events and not one of them was an LLM call. Registering for an event that cannot
arrive is worse than not registering: it promises token and cost data the trace will
never contain, and nothing anywhere reports the omission.

The consequence is a real limitation, not a preference. Through shell hooks alone a
Hermes trace carries no token counts, cost or model latency, so the budget detector has
nothing to work with. Closing that needs a Hermes *plugin*, which the design already
anticipates — and even then the current ``post_llm_call`` payload carries a model name
and the messages, but no usage figures.

``PLUGIN_ONLY_EVENTS`` keeps the two names so the handler still maps them if a future
version routes them to shell hooks, or if a plugin forwards them by hand.
"""

PLUGIN_ONLY_EVENTS: Final = (
    "post_llm_call",
    "on_session_finalize",
)
"""Understood when received, but never configured — Hermes 0.19.0 cannot deliver them."""

_STATUS: Final = {
    "ok": CallStatus.OK,
    "error": CallStatus.ERROR,
    "blocked": CallStatus.BLOCKED,
    "timeout": CallStatus.TIMEOUT,
}

_MAX_ID_LENGTH: Final = 128


def _identifier(value: object, fallback: str) -> str:
    """Coerce a runtime-supplied id into one the schema will accept.

    Hermes ids are opaque strings from a third party, so they are sanitised rather than
    trusted: an id is used to build filesystem paths, and one containing a traversal
    would turn recording into an arbitrary write.
    """
    text = str(value or "").strip()
    safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in text)
    return (safe or fallback)[:_MAX_ID_LENGTH]


def run_id_for(payload: dict[str, Any]) -> str:
    """The run a payload belongs to. Hermes sessions map one-to-one onto runs."""
    return _identifier(payload.get("session_id"), "hermes_session")


def _text(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else repr(value)


def _flagged(*texts: str | None) -> SecurityMetadata:
    found = [kind for text in texts if text for kind in scan(text).kinds]
    return SecurityMetadata(redacted=bool(found), contains_secret=bool(found))


def _preserve(vault: PayloadStore | None, *texts: str | None) -> None:
    """Keep payload text in the local vault, redacted, so later layers can read it.

    The trace itself keeps hashes and nothing else. Without this the hashes refer to
    text that was never stored anywhere, and every layer that needs to *read* a step
    degrades to nothing: ``--mode hybrid`` sent a model twenty steps whose command and
    output were both withheld as "not in the local store", and paid for the privilege.

    Redaction happens first and the redacted form is what lands on disk. The vault is
    local, but a secret written anywhere outlives the scan that found it.
    """
    if vault is None:
        return
    for text in texts:
        if text:
            found = scan(text)
            vault.put(text, stored_text=found.redacted if found.found else None)


def _result_exit_code(output: str | None) -> int | None:
    """The exit code a tool result reports about the command it ran, if it reports one.

    Only a JSON object with an integer ``exit_code`` counts. Anything else returns None
    and the runtime's own status stands: guessing a failure out of unstructured text
    would manufacture findings, which is the one thing this project must not do.
    """
    if not output:
        return None
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    code = parsed.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def map_payload(
    payload: dict[str, Any],
    *,
    sequence: int,
    timestamp: datetime | None = None,
    vault: PayloadStore | None = None,
) -> Event | None:
    """Translate one Hermes hook payload into a Runopsy event.

    Returns ``None`` for events Runopsy does not record, so an unfamiliar hook is
    ignored rather than turned into a fabricated step.

    When a ``vault`` is given, command and output text are preserved locally alongside
    the hashes that go into the trace.
    """
    event_name = str(payload.get("hook_event_name") or "")
    if event_name not in RECORDED_EVENTS and event_name not in PLUGIN_ONLY_EVENTS:
        return None

    run_id = run_id_for(payload)
    moment = timestamp or datetime.now(UTC)
    extra: dict[str, Any] = payload.get("extra") or {}
    common = {
        "event_id": f"{run_id}_evt_{sequence:04d}",
        "run_id": run_id,
        "sequence": sequence,
        "timestamp": moment,
    }

    if event_name == "on_session_start":
        return RunStartEvent(
            **common,
            run=RunPayload(
                task=str(extra.get("task") or payload.get("cwd") or "hermes session"),
                repo=_text(payload.get("cwd")),
                runtime=RUNTIME,
                provider=_text(extra.get("platform")),
                model=_text(extra.get("model")),
            ),
        )

    if event_name in {"on_session_end", "on_session_finalize"}:
        completed = bool(extra.get("completed"))
        interrupted = bool(extra.get("interrupted"))
        outcome = (
            RunOutcome.CANCELLED
            if interrupted
            else RunOutcome.SUCCESS
            if completed
            else RunOutcome.UNKNOWN
        )
        return RunEndEvent(
            **common,
            run=RunPayload(outcome=outcome, summary=_text(extra.get("turn_id"))),
        )

    if event_name == "post_tool_call":
        arguments = _text(payload.get("tool_input") or payload.get("args"))
        output = _text(extra.get("result"))
        error_type = _text(extra.get("error_type"))
        status = _STATUS.get(str(extra.get("status") or "ok"), CallStatus.OK)
        exit_code = 1 if status is CallStatus.ERROR else 0

        # Hermes reports whether the *tool* ran, not whether the command it ran
        # succeeded. Its terminal tool returns {"output": ..., "exit_code": N} and
        # reports status "ok" whenever the shell was invoked at all — so a test suite
        # failing every time looked, to the detectors, like twenty-one successful steps.
        # On the first real trace measured that was exactly the count.
        inner = _result_exit_code(output)
        if inner is not None and status is CallStatus.OK:
            exit_code = inner
            if inner != 0:
                status = CallStatus.ERROR

        _preserve(vault, arguments, output)
        return ToolCallEvent(
            **common,
            parent_id=_identifier(extra.get("turn_id"), "") or None,
            tool=ToolPayload(
                name=_text(payload.get("tool_name")) or "tool",
                arguments_hash=hash_text(arguments) if arguments else None,
                output_hash=hash_text(output) if output else None,
                exit_code=exit_code,
                duration_ms=max(int(extra.get("duration_ms") or 0), 0),
                status=status,
                error_type=error_type,
                blocked_reason=_text(extra.get("error_message"))
                if status is CallStatus.BLOCKED
                else None,
            ),
            security=_flagged(arguments, output),
        )

    if event_name == "post_llm_call":
        # Shell hooks never deliver this event — Hermes routes it to plugins only — so
        # in practice these payloads come from the bundled Runopsy plugin, which fills
        # `extra` with the usage summary Hermes hands to post_api_request. Everything is
        # read defensively all the same: a hand-fed or future-version payload without
        # token fields must still record a valid, merely sparser, model call.
        response = _text(extra.get("response"))
        cost = extra.get("cost_usd")
        _preserve(vault, response)
        return LlmCallEvent(
            **common,
            llm=LlmPayload(
                model=_text(extra.get("model")) or _text(payload.get("model")) or "unknown",
                provider=_text(extra.get("provider")) or _text(extra.get("platform")),
                response_hash=hash_text(response) if response else None,
                tokens=TokenUsage(
                    input_tokens=max(int(extra.get("input_tokens") or 0), 0),
                    output_tokens=max(int(extra.get("output_tokens") or 0), 0),
                    cached_input_tokens=max(int(extra.get("cached_input_tokens") or 0), 0),
                ),
                latency_ms=max(int(extra.get("duration_ms") or 0), 0),
                finish_reason=_text(extra.get("finish_reason")),
                cost_usd=float(cost) if isinstance(cost, (int, float)) and cost >= 0 else None,
            ),
            security=_flagged(response),
        )

    child_status = str(extra.get("child_status") or "")
    summary = _text(extra.get("child_summary"))
    return HandoffEvent(
        **common,
        handoff=HandoffPayload(
            from_agent_id=_identifier(payload.get("session_id"), "main"),
            to_agent_id=_identifier(extra.get("child_session_id"), "subagent"),
            context_hash=hash_text(summary) if summary else None,
            missing_fields=() if summary else ("child_summary",),
        ),
        security=_flagged(summary, child_status),
    )


CONFIG_LOCATIONS: Final = (
    ("LOCALAPPDATA", "hermes/config.yaml"),
    (None, ".hermes/config.yaml"),
    (None, ".config/hermes/config.yaml"),
)
"""Where hermes-agent keeps its config, in the order it is worth looking.

Windows reports ``%LOCALAPPDATA%/hermes/config.yaml``; the POSIX builds use a dotfile.
``hermes config path`` is authoritative, but shelling out to another tool to find out
whether that tool is configured fails exactly when the answer matters most.
"""


@dataclass(frozen=True)
class AdapterStatus:
    """What `runopsy adapter hermes status` found."""

    config_path: Path | None
    parse_error: str | None
    configured: tuple[str, ...]
    missing: tuple[str, ...]
    never_fires: tuple[str, ...]

    @property
    def is_wired(self) -> bool:
        return self.config_path is not None and not self.parse_error and not self.missing


def _config_candidates() -> list[Path]:
    paths: list[Path] = []
    for variable, suffix in CONFIG_LOCATIONS:
        root = os.environ.get(variable) if variable else None
        base = Path(root) if root else Path.home()
        paths.append(base / suffix)
    return paths


def plugin_source_dir() -> Path:
    """Where the bundled Hermes plugin files live inside this package."""
    return Path(__file__).parent / "hermes_plugin"


def plugin_install_dir(config_path: Path | None = None) -> Path:
    """Where Hermes looks for user plugins: ``<hermes_home>/plugins/runopsy``.

    Derived from wherever the config was found, since Hermes anchors both to the same
    home directory; falls back to the platform default when no config exists yet.
    """
    found = config_path or next((path for path in _config_candidates() if path.is_file()), None)
    home = found.parent if found is not None else _config_candidates()[0].parent
    return home / "plugins" / "runopsy"


def install_plugin(target: Path | None = None) -> Path:
    """Copy the plugin into Hermes' user-plugin directory, returning where it went.

    This writes into a directory that belongs to the plugin — creating
    ``plugins/runopsy/`` is claiming our own name, not editing anyone's file. Enabling
    it is different: ``plugins.enabled`` lives in the user's config.yaml, so that step
    stays theirs and the CLI prints the line to paste instead of adding it.
    """
    destination = target or plugin_install_dir()
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "plugin.yaml"):
        destination.joinpath(name).write_text(
            plugin_source_dir().joinpath(name).read_text(encoding="utf-8"), encoding="utf-8"
        )

    # Where this Runopsy lives, recorded now because the plugin cannot work it out
    # later. Hermes runs in its own virtualenv — these instructions say to install it
    # that way — so  is not on the PATH the plugin inherits, and resolving by
    # PATH alone meant it found nothing and recorded nothing. A trace with tool calls and
    # no model calls is indistinguishable from a runtime that does not report them, which
    # is exactly the confusion this project already spent a day on.
    executable = _this_runopsy()
    if executable is not None:
        destination.joinpath(PLUGIN_EXECUTABLE_FILE).write_text(executable, encoding="utf-8")
    return destination


def _this_runopsy() -> str | None:
    """The absolute path of the running Runopsy CLI, if it can be identified."""
    scripts = Path(sys.executable).parent
    for name in ("runopsy.exe", "runopsy"):
        candidate = scripts / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("runopsy")


def adapter_status(config_path: Path | None = None) -> AdapterStatus:
    """Report whether Hermes is actually wired to Runopsy.

    Written because the failure this detects is silent. An invalid hook block makes
    Hermes discard its whole config and run with defaults, and a hook registered for an
    event Hermes only sends to plugins never fires. Both produce a session that behaves
    normally and records nothing, with no error anywhere to notice.
    """
    candidates = [config_path] if config_path is not None else _config_candidates()
    found = next((path for path in candidates if path.is_file()), None)
    if found is None:
        return AdapterStatus(None, None, (), RECORDED_EVENTS, ())

    try:
        import yaml

        parsed = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
    except Exception as error:  # any read, parse or import failure *is* the answer here
        return AdapterStatus(found, f"{type(error).__name__}: {error}", (), RECORDED_EVENTS, ())

    raw = parsed.get("hooks") if isinstance(parsed, dict) else None
    hooks: dict[str, Any] = raw if isinstance(raw, dict) else {}
    ours = tuple(
        event
        for event in hooks
        if any(
            "runopsy" in str(entry.get("command", ""))
            for entry in hooks.get(event) or []
            if isinstance(entry, dict)
        )
    )
    return AdapterStatus(
        config_path=found,
        parse_error=None,
        configured=ours,
        missing=tuple(event for event in RECORDED_EVENTS if event not in ours),
        never_fires=tuple(event for event in ours if event in PLUGIN_ONLY_EVENTS),
    )


def hooks_config_block(command: str) -> str:
    """The YAML to add to ``cli-config.yaml`` so Hermes calls Runopsy.

    Emitted for the user to paste rather than written automatically. Editing another
    tool's configuration behind its owner's back is how integrations become impossible
    to debug, and the file may hold settings we know nothing about.

    The whole ``<command> <event>`` string is one YAML scalar, and it is quoted as one
    when the path contains a space. Quoting only the path — ``command: "C:/x y/runopsy"
    hook post_tool_call`` — is invalid YAML, and Hermes responds by discarding the entire
    config and running with defaults. The session then looks completely normal and
    records nothing at all, which cost an afternoon the first time.
    """
    lines = ["hooks:"]
    for event in RECORDED_EVENTS:
        invocation = f"{command} {event}".replace("'", "''")
        lines.append(f"  {event}:")
        lines.append(f"    - command: '{invocation}'")
        lines.append("      timeout: 10")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class HookInstall:
    """What ``install_hooks`` actually did, so the caller can report it honestly."""

    config_path: Path
    backup_path: Path | None
    created_config: bool
    rewrote_file: bool
    added: tuple[str, ...]
    already_present: tuple[str, ...]


def _backup_beside(path: Path) -> Path:
    """Copy ``path`` aside under a name that never overwrites an earlier backup.

    Numbered rather than timestamped so the same input always produces the same name,
    which keeps this testable without freezing a clock.
    """
    candidate = path.with_suffix(path.suffix + ".runopsy-backup")
    counter = 1
    while candidate.exists():
        candidate = path.with_suffix(f"{path.suffix}.runopsy-backup.{counter}")
        counter += 1
    shutil.copy2(path, candidate)
    return candidate


def install_hooks(command: str, config_path: Path | None = None) -> HookInstall:
    """Write the hook block into Hermes' config, when the user explicitly asks.

    ``hooks_config_block`` deliberately leaves the paste to the user, and the reason
    still holds: editing another tool's configuration unasked is how an integration
    becomes impossible to debug. But asking a newcomer to hand-merge YAML into a file
    they have to go and find is the step that loses them, and the paste has its own
    documented way of going wrong. So this performs the same edit *on request*, and pays
    what consent costs:

    - a config we cannot parse is refused, never overwritten — that file is somebody's
      working setup and a parse error means we do not understand it;
    - the previous contents are copied aside first;
    - when Hermes has no ``hooks:`` section at all — the ordinary case — the block is
      appended as text, so comments, ordering and formatting elsewhere survive
      byte-for-byte. Round-tripping through the parser would silently reformat a file we
      were only meant to add four lines to;
    - only when a ``hooks:`` section already exists is the document rewritten, because
      appending a second ``hooks:`` key would produce a config where one half silently
      wins.

    Raises ``ValueError`` if the existing config cannot be parsed.
    """
    if config_path is not None:
        target = config_path
    else:
        existing = next((path for path in _config_candidates() if path.is_file()), None)
        target = existing if existing is not None else _config_candidates()[0]

    created = not target.exists()
    if created:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    text = target.read_text(encoding="utf-8")
    try:
        import yaml

        parsed = yaml.safe_load(text) or {}
    except Exception as error:
        raise ValueError(f"Hermes config at {target} is not parseable: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"Hermes config at {target} is not a mapping; refusing to edit it.")

    raw = parsed.get("hooks")
    hooks: dict[str, Any] = raw if isinstance(raw, dict) else {}

    def ours(event: str) -> bool:
        return any(
            "runopsy" in str(entry.get("command", ""))
            for entry in hooks.get(event) or []
            if isinstance(entry, dict)
        )

    already = tuple(event for event in RECORDED_EVENTS if ours(event))
    missing = tuple(event for event in RECORDED_EVENTS if not ours(event))
    if not missing:
        return HookInstall(target, None, created, False, (), already)

    backup = None if created else _backup_beside(target)

    if not hooks:
        # The ordinary case: no hooks section, so the block can simply be added and every
        # other line in the file is left exactly as its owner wrote it.
        prefix = "" if not text or text.endswith("\n") else "\n"
        target.write_text(text + prefix + hooks_config_block(command), encoding="utf-8")
        return HookInstall(target, backup, created, False, missing, already)

    import yaml

    for event in missing:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} in {target} is not a list; refusing to edit it.")
        entries.append({"command": f"{command} {event}", "timeout": 10})
    parsed["hooks"] = hooks
    target.write_text(yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return HookInstall(target, backup, created, True, missing, already)


def enable_plugin(config_path: Path | None = None, name: str = "runopsy") -> bool:
    """Name the Runopsy plugin in Hermes' ``plugins.enabled`` list.

    Installing the plugin files is not enough — Hermes loads a user plugin only when its
    config names it, so a half-done setup records tool calls and no model calls, which
    looks exactly like a runtime that does not report them. Same rules as
    ``install_hooks``: refuse a config we cannot parse, append as text when there is no
    ``plugins:`` section so nothing else in the file moves, and only rewrite when a merge
    is genuinely required.

    Returns True if the file was changed.
    """
    if config_path is not None:
        target = config_path
    else:
        existing = next((path for path in _config_candidates() if path.is_file()), None)
        target = existing if existing is not None else _config_candidates()[0]

    target.parent.mkdir(parents=True, exist_ok=True)
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    try:
        import yaml

        parsed = yaml.safe_load(text) or {}
    except Exception as error:
        raise ValueError(f"Hermes config at {target} is not parseable: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"Hermes config at {target} is not a mapping; refusing to edit it.")

    plugins = parsed.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if isinstance(enabled, list) and name in enabled:
        return False

    if plugins is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        target.write_text(f"{text}{prefix}plugins:\n  enabled:\n    - {name}\n", encoding="utf-8")
        return True

    import yaml

    if not isinstance(plugins, dict):
        raise ValueError(f"plugins in {target} is not a mapping; refusing to edit it.")
    entries = plugins.setdefault("enabled", [])
    if not isinstance(entries, list):
        raise ValueError(f"plugins.enabled in {target} is not a list; refusing to edit it.")
    entries.append(name)
    parsed["plugins"] = plugins
    target.write_text(yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return True
