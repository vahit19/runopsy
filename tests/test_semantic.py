"""Semantic layer tests.

The subject here is a boundary, not a capability. A model can explain anything fluently,
so what matters is what it is *not* allowed to do: produce a status, exceed its
confidence band, see more of the trace than it needs, or spend past a ceiling.

No test touches the network. A live smoke check exists in test_semantic_live.py and is
skipped unless explicitly enabled — a suite that costs money on every run is a suite
people turn off.
"""

from __future__ import annotations

import json as _json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import START, run_end, run_start
from runopsy_core import AnalysisContext
from runopsy_core.schema import (
    AnalysisLayer,
    DiagnosisStatus,
    FailureCategory,
    RunOutcome,
    SecurityMetadata,
    Severity,
    ToolCallEvent,
    ToolPayload,
)
from runopsy_semantic import (
    Budget,
    BudgetExceededError,
    Ledger,
    OpenRouterClient,
    ProviderError,
    SemanticVerdict,
    VerdictCache,
    build_packet,
    cache_key,
    estimate_tokens,
    parse_verdict,
    resolve_api_key,
    review_diagnosis,
    review_span,
    to_signal,
    window_around,
)
from runopsy_semantic.provider import MAX_ATTEMPTS

RUN = "run_sem"


def tool(
    sequence: int, *, name: str = "terminal", exit_code: int = 0, secret: bool = False
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
        security=SecurityMetadata(contains_secret=secret),
    )


def context() -> AnalysisContext:
    events = [
        run_start(RUN, task="configure the service"),
        tool(1, name="write_config"),
        tool(2, name="edit_file"),
        tool(3, name="pytest", exit_code=1),
        run_end(4, RUN, outcome=RunOutcome.FAILURE),
    ]
    return AnalysisContext.from_events(RUN, events)


class FakeTransport:
    """Records requests and returns canned responses. Never touches the network."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        self.requests.append({"url": url, "headers": headers, "body": json})
        nxt = self._responses.pop(0) if self._responses else {"finding": False}
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(
            200,
            json={
                "model": "test/model",
                "choices": [{"message": {"content": _json.dumps(nxt)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0001},
            },
            request=httpx.Request("POST", url),
        )


def client(*responses: dict[str, Any] | Exception) -> tuple[OpenRouterClient, FakeTransport]:
    transport = FakeTransport(*responses)
    return OpenRouterClient("test-key", model="test/model", transport=transport), transport


class TestBudget:
    def test_a_ceiling_is_checked_before_the_call_not_after(self) -> None:
        """A limit enforced afterwards is one you discover on an invoice."""
        ledger = Ledger(budget=Budget(max_calls=1))
        ledger.record(input_tokens=10, output_tokens=5, cost_usd=0.01)

        with pytest.raises(BudgetExceededError, match="call ceiling"):
            ledger.check(10)

    def test_an_oversized_payload_is_refused(self) -> None:
        ledger = Ledger(budget=Budget(max_input_tokens=100))

        with pytest.raises(BudgetExceededError, match="exceeds the per-call limit"):
            ledger.check(101)

    def test_a_spent_cost_ceiling_stops_further_calls(self) -> None:
        ledger = Ledger(budget=Budget(max_cost_usd=0.01))
        ledger.record(input_tokens=1, output_tokens=1, cost_usd=0.01)

        with pytest.raises(BudgetExceededError, match="cost ceiling"):
            ledger.check(1)

    def test_zero_calls_disables_the_layer(self) -> None:
        assert Budget(max_calls=0).disabled is True

    def test_can_afford_records_why_it_stopped(self) -> None:
        ledger = Ledger(budget=Budget(max_calls=0))

        assert ledger.can_afford(1) is False
        assert ledger.stopped_because is not None

    def test_token_estimate_is_an_upper_bound(self) -> None:
        assert estimate_tokens("abcd") >= 1
        assert estimate_tokens("a" * 400) >= 100


class TestDataMinimization:
    def test_only_a_window_is_sent_never_the_run(self) -> None:
        """A long run must not be shipped wholesale to answer about one step."""
        events = tuple(tool(index) for index in range(30))

        window = window_around(events, 15)

        assert {event.sequence for event in window} == {13, 14, 15, 16, 17}

    def test_the_window_is_clipped_at_the_start_of_the_run(self) -> None:
        events = tuple(tool(index) for index in range(30))

        assert {event.sequence for event in window_around(events, 0)} == {0, 1, 2}

    def test_a_step_that_is_not_in_the_events_yields_nothing(self) -> None:
        assert window_around(tuple(tool(i) for i in range(5)), 99) == ()

    def test_a_packet_carries_no_raw_hashes_or_ids(self) -> None:
        ctx = context()

        packet = build_packet(RUN, window_around(ctx.events, 3), 3)

        assert "sha256:" not in packet.to_json()

    def test_withholding_is_reported_so_a_weaker_verdict_looks_weaker(self) -> None:
        ctx = context()

        packet = build_packet(RUN, window_around(ctx.events, 1), 1, vault=None)

        assert packet.steps
        # Without a vault there is no command text; the packet says so rather than
        # silently sending less.
        assert isinstance(packet.withheld, tuple)

    def test_a_redacted_payload_is_never_sent(self) -> None:
        class Vault:
            def get(self, digest: str) -> Any:
                class Entry:
                    text = "curl -H 'Authorization: Bearer aaaaaaaaaaaaaaaaaaaa'"
                    executable = False

                return Entry()

        event = ToolCallEvent(
            event_id="evt_9",
            run_id=RUN,
            sequence=9,
            timestamp=START,
            tool=ToolPayload(name="curl", arguments_hash="sha256:" + "a" * 64),
        )

        packet = build_packet(RUN, (event,), 9, vault=Vault())

        assert "Bearer" not in packet.to_json()
        assert any("credential" in item for item in packet.withheld)


class TestVerdictParsing:
    def test_a_well_formed_verdict_is_read(self) -> None:
        verdict = parse_verdict(
            '{"finding": true, "category": "tool_arguments", "summary": "wrote staging", '
            '"confidence": 0.7}'
        )

        assert verdict is not None
        assert verdict.finding is True
        assert verdict.summary == "wrote staging"

    def test_a_fenced_verdict_is_read(self) -> None:
        verdict = parse_verdict('```json\n{"finding": false}\n```')

        assert verdict is not None
        assert verdict.finding is False

    def test_unparseable_output_is_discarded_not_repaired(self) -> None:
        """Coaxing meaning from a malformed response is how a hallucination lands."""
        assert parse_verdict("I think step 9 was probably the problem.") is None
        assert parse_verdict("") is None
        assert parse_verdict("{broken") is None

    def test_a_finding_without_a_summary_is_rejected(self) -> None:
        assert parse_verdict('{"finding": true, "confidence": 0.9}') is None

    def test_confidence_is_clamped(self) -> None:
        verdict = parse_verdict('{"finding": true, "summary": "x", "confidence": 5}')

        assert verdict is not None
        assert verdict.confidence == 1.0


class TestSignalBoundary:
    def test_a_semantic_signal_reports_the_semantic_layer(self) -> None:
        signal = to_signal(SemanticVerdict(True, FailureCategory.STATE, "s", 0.9), "evt_1")

        assert signal is not None
        assert signal.layer is AnalysisLayer.L3_SEMANTIC
        assert signal.detector.startswith("semantic:")

    def test_severity_is_capped_however_certain_the_model_claims_to_be(self) -> None:
        """A model has no way to know something is critical; it has a way to say so."""
        signal = to_signal(SemanticVerdict(True, FailureCategory.STATE, "s", 1.0), "evt_1")

        assert signal is not None
        assert signal.severity is Severity.MEDIUM

    def test_the_summary_says_it_is_unverified(self) -> None:
        signal = to_signal(SemanticVerdict(True, FailureCategory.STATE, "wrote staging", 0.8), "e")

        assert signal is not None
        assert "unverified" in signal.summary

    def test_no_finding_produces_no_signal(self) -> None:
        assert to_signal(SemanticVerdict(False, FailureCategory.STATE, "", 0.1), "evt_1") is None


class TestReviewSpan:
    def test_a_verdict_is_returned_and_the_cost_recorded(self) -> None:
        api, _ = client({"finding": True, "category": "state", "summary": "s", "confidence": 0.8})
        ledger = Ledger()
        packet = build_packet(RUN, window_around(context().events, 1), 1)

        verdict = review_span(packet, api, ledger)

        assert verdict is not None
        assert verdict.finding
        assert ledger.calls == 1
        assert ledger.cost_usd > 0

    def test_a_provider_failure_leaves_the_deterministic_answer_standing(self) -> None:
        """An outage must not turn a working diagnosis into a failed command.

        The fixture raises on every permitted attempt, which is what an outage now means.
        One refused connection is no longer one: the client retries a request that never
        arrived, so a single raise describes a blip it is meant to survive rather than a
        network that is down. The assertion — and the property it protects — is unchanged.
        """
        api, _ = client(*[httpx.ConnectError("no network")] * MAX_ATTEMPTS)
        ledger = Ledger()
        packet = build_packet(RUN, window_around(context().events, 1), 1)

        assert review_span(packet, api, ledger) is None

    def test_an_exhausted_budget_stops_before_calling(self) -> None:
        api, transport = client({"finding": False})
        ledger = Ledger(budget=Budget(max_calls=0))
        packet = build_packet(RUN, window_around(context().events, 1), 1)

        assert review_span(packet, api, ledger) is None
        assert transport.requests == []

    def test_a_second_identical_question_is_free(self, tmp_path: Path) -> None:
        api, transport = client({"finding": False}, {"finding": False})
        cache = VerdictCache(tmp_path / "cache")
        packet = build_packet(RUN, window_around(context().events, 1), 1)

        review_span(packet, api, Ledger(), cache=cache)
        second = Ledger()
        review_span(packet, api, second, cache=cache)

        assert len(transport.requests) == 1
        assert second.calls == 0
        assert second.cached_hits == 1

    def test_the_cache_key_covers_the_model_and_prompt_version(self) -> None:
        packet = build_packet(RUN, window_around(context().events, 1), 1)

        assert cache_key(packet, "model-a") != cache_key(packet, "model-b")


class TestHybridDiagnosis:
    def test_a_semantic_finding_becomes_evidence_not_a_verdict(self) -> None:
        api, _ = client(
            {
                "finding": True,
                "category": "state",
                "summary": "wrote the wrong value",
                "confidence": 0.9,
            }
        )

        result = review_diagnosis(context(), api, budget=Budget(max_calls=3))

        assert result.semantic_findings >= 1
        for candidate in result.bundle.candidates:
            assert candidate.status is not DiagnosisStatus.REPLAY_SUPPORTED
            assert candidate.is_definitive is False

    def test_cost_is_reported_on_the_bundle(self) -> None:
        api, _ = client({"finding": False})

        result = review_diagnosis(context(), api, budget=Budget(max_calls=1))

        assert result.bundle.tokens_spent > 0
        assert result.ledger.calls == 1

    def test_a_disabled_budget_makes_no_calls_at_all(self) -> None:
        api, transport = client({"finding": True, "summary": "x", "confidence": 1.0})

        result = review_diagnosis(context(), api, budget=Budget(max_calls=0))

        assert transport.requests == []
        assert result.bundle.tokens_spent == 0

    def test_review_stops_at_the_call_ceiling(self) -> None:
        api, transport = client(*[{"finding": False}] * 5)

        review_diagnosis(context(), api, budget=Budget(max_calls=1))

        assert len(transport.requests) == 1

    def test_a_healthy_run_is_never_sent_anywhere(self) -> None:
        """No candidates means no question worth paying for."""
        events = [run_start(RUN), tool(1), run_end(2, RUN, outcome=RunOutcome.SUCCESS)]
        api, transport = client({"finding": True, "summary": "x", "confidence": 1.0})

        review_diagnosis(AnalysisContext.from_events(RUN, events), api)

        assert transport.requests == []


class TestKeyHandling:
    def test_a_missing_key_is_reported_as_absent_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No key means the offline diagnosis still answers, which is the whole design."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        assert resolve_api_key() is None

    def test_an_explicit_key_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")

        assert resolve_api_key("explicit") == "explicit"

    def test_the_key_never_appears_in_a_provider_error(self) -> None:
        transport = FakeTransport()

        def failing_post(
            url: str, *, headers: dict[str, str], json: dict[str, Any]
        ) -> httpx.Response:
            return httpx.Response(
                401, json={"error": "bad key"}, request=httpx.Request("POST", url)
            )

        transport.post = failing_post  # type: ignore[method-assign]
        api = OpenRouterClient("super-secret-key", transport=transport)

        with pytest.raises(ProviderError) as caught:
            api.complete("s", "u")

        assert "super-secret-key" not in str(caught.value)

    def test_a_provider_error_body_is_not_echoed(self) -> None:
        """The body can contain request content; only the status is surfaced."""
        transport = FakeTransport()

        def failing_post(
            url: str, *, headers: dict[str, str], json: dict[str, Any]
        ) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": "your prompt contained /home/user/secret.py"},
                request=httpx.Request("POST", url),
            )

        transport.post = failing_post  # type: ignore[method-assign]
        api = OpenRouterClient("k", transport=transport)

        with pytest.raises(ProviderError) as caught:
            api.complete("s", "u")

        assert "secret.py" not in str(caught.value)


class TestRequestShape:
    def test_temperature_is_zero_so_answers_are_cacheable(self) -> None:
        api, transport = client({"finding": False})

        api.complete("system", "user")

        assert transport.requests[0]["body"]["temperature"] == 0

    def test_the_authorization_header_carries_the_users_own_key(self) -> None:
        api, transport = client({"finding": False})

        api.complete("s", "u")

        assert transport.requests[0]["headers"]["Authorization"] == "Bearer test-key"


class TestUnexplainedFailureLookback:
    """The gap live testing exposed: reviewing only candidates is blind by construction.

    A step that succeeds while doing the wrong thing never becomes a deterministic
    candidate — and that is exactly the class the semantic layer was added to catch. When
    the engine can point at a failure but at nothing upstream that explains it, the
    steps before it are the only place an explanation could be.
    """

    def _silent_failure(self) -> AnalysisContext:
        events = [
            run_start(RUN, task="configure and verify"),
            tool(1, name="write_config"),  # succeeds, writes the wrong value
            tool(2, name="rebuild"),
            tool(3, name="verify", exit_code=1),  # the only visible failure
            run_end(4, RUN, outcome=RunOutcome.FAILURE),
        ]
        return AnalysisContext.from_events(RUN, events)

    def test_steps_before_an_unexplained_failure_are_reviewed(self) -> None:
        api, transport = client(*[{"finding": False}] * 5)

        review_diagnosis(self._silent_failure(), api, budget=Budget(max_calls=5))

        reviewed = [
            _json.loads(request["body"]["messages"][1]["content"])["focus_step"]
            for request in transport.requests
        ]
        assert 1 in reviewed, "the silent step was never shown to the model"

    def test_a_finding_on_a_silent_step_becomes_a_candidate(self) -> None:
        api, _ = client(
            {"finding": False},
            {"finding": False},
            {
                "finding": True,
                "category": "tool_arguments",
                "summary": "wrote the wrong endpoint",
                "confidence": 0.8,
            },
        )

        result = review_diagnosis(self._silent_failure(), api, budget=Budget(max_calls=5))

        assert result.semantic_findings == 1
        assert any(c.onset_node_id == "evt_1" for c in result.bundle.candidates)

    def test_an_already_explained_failure_does_not_widen_the_search(self) -> None:
        """Cost discipline: when the engine already has an upstream lead, follow it."""
        events = [
            run_start(RUN),
            tool(1, name="build", exit_code=1),  # a real, detected earlier failure
            tool(2, name="verify", exit_code=1),
            run_end(3, RUN, outcome=RunOutcome.FAILURE),
        ]
        api, transport = client(*[{"finding": False}] * 6)

        review_diagnosis(AnalysisContext.from_events(RUN, events), api, budget=Budget(max_calls=6))

        reviewed = {
            _json.loads(request["body"]["messages"][1]["content"])["focus_step"]
            for request in transport.requests
        }
        assert reviewed <= {1, 2}

    def test_the_lookback_still_respects_the_call_ceiling(self) -> None:
        api, transport = client(*[{"finding": False}] * 10)

        review_diagnosis(self._silent_failure(), api, budget=Budget(max_calls=2))

        assert len(transport.requests) == 2


class TestTheCaseWorthPayingFor:
    """A run that failed while every step reported success.

    Nothing errored, nothing timed out, the answer was simply wrong — the silent failure,
    and the one an agent's user is least able to find by hand. Hybrid mode used to return
    immediately when the deterministic layers produced no candidate, so somebody whose
    agent had failed this way asked for a model's opinion and was told the run was clean.
    Measured on Who&When, an external benchmark of hand-annotated multi-agent failures,
    every single case has that shape.
    """

    def silent_failure(self) -> AnalysisContext:
        events = [
            run_start(RUN, task="answer the question"),
            tool(1, name="search"),
            tool(2, name="read"),
            tool(3, name="summarise"),
            run_end(4, RUN, outcome=RunOutcome.FAILURE),
        ]
        return AnalysisContext.from_events(RUN, events)

    def test_the_deterministic_layers_find_nothing(self) -> None:
        """Correctly: there is nothing structural to find."""
        from runopsy_core import diagnose

        assert not diagnose(self.silent_failure()).candidates

    def test_hybrid_mode_asks_anyway_and_answers(self) -> None:
        api, transport = client(
            {"finding": True, "category": "reasoning", "summary": "wrong source", "confidence": 0.6}
        )

        result = review_diagnosis(self.silent_failure(), api, budget=Budget(max_calls=2))

        assert transport.requests, "hybrid mode spent nothing on a run that failed"
        assert result.bundle.candidates, "the user paid and got no answer"

    def test_a_healthy_run_still_costs_nothing(self) -> None:
        """The other half. Asking about a run nobody said was wrong spends a user's
        money to be told their green pipeline is green."""
        api, transport = client({"finding": False})
        healthy = AnalysisContext.from_events(
            RUN,
            [
                run_start(RUN, task="tidy up"),
                tool(1, name="format"),
                run_end(2, RUN, outcome=RunOutcome.SUCCESS),
            ],
        )

        result = review_diagnosis(healthy, api, budget=Budget(max_calls=2))

        assert transport.requests == []
        assert not result.bundle.candidates

    def test_what_it_finds_stays_below_certainty(self) -> None:
        """A model's opinion on a trace with no structural evidence is the weakest kind
        of finding there is, and must not read as anything more."""
        api, _ = client(
            {"finding": True, "category": "reasoning", "summary": "wrong source", "confidence": 0.9}
        )

        result = review_diagnosis(self.silent_failure(), api, budget=Budget(max_calls=2))

        for candidate in result.bundle.candidates:
            assert candidate.confidence < 0.75
            assert candidate.status is not DiagnosisStatus.REPLAY_SUPPORTED
