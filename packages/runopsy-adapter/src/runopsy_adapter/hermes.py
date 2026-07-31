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

from datetime import UTC, datetime
from typing import Any, Final

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


def map_payload(
    payload: dict[str, Any], *, sequence: int, timestamp: datetime | None = None
) -> Event | None:
    """Translate one Hermes hook payload into a Runopsy event.

    Returns ``None`` for events Runopsy does not record, so an unfamiliar hook is
    ignored rather than turned into a fabricated step.
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
        return ToolCallEvent(
            **common,
            parent_id=_identifier(extra.get("turn_id"), "") or None,
            tool=ToolPayload(
                name=_text(payload.get("tool_name")) or "tool",
                arguments_hash=hash_text(arguments) if arguments else None,
                output_hash=hash_text(output) if output else None,
                exit_code=1 if status is CallStatus.ERROR else 0,
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
