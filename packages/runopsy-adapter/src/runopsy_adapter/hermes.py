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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

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
        return LlmCallEvent(
            **common,
            llm=LlmPayload(
                model=_text(extra.get("model")) or _text(payload.get("model")) or "unknown",
                provider=_text(extra.get("platform")),
                latency_ms=max(int(extra.get("duration_ms") or 0), 0),
                finish_reason=_text(extra.get("finish_reason")),
            ),
            security=_flagged(_text(extra.get("response"))),
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
