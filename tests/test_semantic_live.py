"""A live check against the real provider.

Skipped unless ``RUNOPSY_LIVE_TEST=1`` and a key are both present. A suite that spends
money on every run is a suite people disable, and a disabled suite protects nothing —
but a semantic layer that has never met the real API is a layer nobody should trust
either. So the check exists, costs a fraction of a cent, and is run deliberately.
"""

from __future__ import annotations

import os

import pytest

from runopsy_semantic import (
    Budget,
    Ledger,
    OpenRouterClient,
    build_packet,
    resolve_api_key,
    review_span,
)
from runopsy_semantic.provider import DEFAULT_MODEL

pytestmark = pytest.mark.skipif(
    os.environ.get("RUNOPSY_LIVE_TEST") != "1" or resolve_api_key() is None,
    reason="live provider check: set RUNOPSY_LIVE_TEST=1 with an API key to run",
)


def test_the_real_provider_answers_in_the_expected_shape() -> None:
    """One real call, checking the contract this package depends on."""
    from datetime import UTC, datetime

    from runopsy_core.schema import ToolCallEvent, ToolPayload

    now = datetime(2026, 7, 30, tzinfo=UTC)
    events = (
        ToolCallEvent(
            event_id="evt_1",
            run_id="live",
            sequence=1,
            timestamp=now,
            tool=ToolPayload(name="write_config", exit_code=0),
        ),
        ToolCallEvent(
            event_id="evt_2",
            run_id="live",
            sequence=2,
            timestamp=now,
            tool=ToolPayload(name="pytest", exit_code=1, error_type="AssertionError"),
        ),
    )

    key = resolve_api_key()
    assert key is not None
    client = OpenRouterClient(key, model=os.environ.get("RUNOPSY_LIVE_MODEL", DEFAULT_MODEL))
    ledger = Ledger(budget=Budget(max_calls=1, max_cost_usd=0.05))

    verdict = review_span(build_packet("live", events, 1), client, ledger)

    assert verdict is not None, "the provider returned nothing parseable"
    assert isinstance(verdict.finding, bool)
    assert 0.0 <= verdict.confidence <= 1.0
    assert ledger.calls == 1
    assert ledger.input_tokens > 0
