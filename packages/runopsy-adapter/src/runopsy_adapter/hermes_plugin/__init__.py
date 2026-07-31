"""Runopsy's Hermes plugin: the model-call data shell hooks cannot carry.

Hermes dispatches ``post_api_request`` — the one hook that includes token usage — only
to Python plugins, never to shell hooks. This plugin exists solely to bridge that gap:
it receives the usage summary inside the Hermes process and forwards it to the same
``runopsy hook`` subprocess the shell hooks already use, shaped as a ``post_llm_call``
payload the Runopsy mapper understands.

It deliberately stays a bridge rather than a recorder. All the actual work — vault,
redaction, sequencing, the store — lives in Runopsy, reached through the same wire
format as every other event, so this file has no dependency on Runopsy's internals and
Runopsy has none on Hermes'. The plugin is installed by ``runopsy adapter hermes
plugin`` and runs inside Hermes, which is why it imports nothing but the standard
library at module level.

The one rule it inherits from the shell hooks: never break the run being observed.
Every failure path swallows the error. A diagnostic bridge that crashes the agent it
was watching is worse than the missing token counts it was built to recover.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

HOOK_TIMEOUT_SECONDS = 10.0


def _runopsy_executable() -> str | None:
    """Where the runopsy CLI lives, or None when it cannot be found.

    ``RUNOPSY_EXECUTABLE`` wins so a virtualenv install that is not on PATH still
    works; otherwise PATH decides, exactly as it does for the shell hooks.
    """
    override = os.environ.get("RUNOPSY_EXECUTABLE")
    if override and Path(override).is_file():
        return override
    return shutil.which("runopsy")


def _cost_usd(model: str, usage: dict[str, Any], provider: str, base_url: str) -> float | None:
    """Best-effort cost from Hermes' own pricing tables.

    Guarded because it reaches into Hermes internals, which is exactly the coupling the
    rest of this integration avoids: if the module moves in a later version, the price
    goes missing and everything else keeps working.
    """
    try:
        # Inside the Hermes process this resolves; everywhere else the guard eats it.
        from agent.usage_pricing import (  # type: ignore[import-not-found]
            CanonicalUsage,
            estimate_usage_cost,
        )

        canonical = CanonicalUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        )
        cost = estimate_usage_cost(model, canonical, provider=provider, base_url=base_url)
        value = getattr(cost, "total_cost", None) if cost is not None else None
        if value is None and isinstance(cost, (int, float)):
            value = cost
        return float(value) if isinstance(value, (int, float)) and value >= 0 else None
    except Exception:
        return None


def _on_post_api_request(**kwargs: Any) -> None:
    """Forward one model call to Runopsy. Failures are silent by design."""
    try:
        usage = kwargs.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        model = str(kwargs.get("response_model") or kwargs.get("model") or "")
        provider = str(kwargs.get("provider") or "")
        duration = kwargs.get("api_duration")
        cached = int(usage.get("cache_read_tokens") or 0) + int(
            usage.get("cache_write_tokens") or 0
        )

        payload = {
            "hook_event_name": "post_llm_call",
            "session_id": kwargs.get("session_id") or "",
            "extra": {
                "model": model or None,
                "provider": provider or None,
                "finish_reason": kwargs.get("finish_reason"),
                "duration_ms": int(float(duration) * 1000) if duration else 0,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": cached,
                "cost_usd": _cost_usd(model, usage, provider, str(kwargs.get("base_url") or "")),
            },
        }

        executable = _runopsy_executable()
        if executable is None:
            return

        subprocess.run(
            [executable, "hook", "post_llm_call"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        # Deliberate: no logging channel here is guaranteed safe, and the run must
        # survive its recorder. The gap this leaves is visible in the trace itself —
        # a session with tool calls and no model calls — which `adapter hermes status`
        # already teaches people to look for.
        return


def register(ctx: Any) -> None:
    """Hermes entry point."""
    ctx.register_hook("post_api_request", _on_post_api_request)
