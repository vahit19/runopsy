"""Retrying a provider call, and — more importantly — not retrying one.

There was no retry at all, which made a hybrid diagnosis a coin toss against one TCP
connection: a dropped packet threw away a run the user had already recorded, already
analysed, and already agreed to pay for.

What matters at least as much is the second half. The retryable set is exactly two
transport failures, because both mean the request never arrived — nothing computed,
nothing billed, and sending it again sends the same request. An HTTP status means the
provider answered: a 4xx cannot be fixed by repetition, a 429 met with an immediate retry
storm is how a rate limit becomes a ban, and a 5xx may have run the model before failing,
so a retry can be billed twice. Spending someone else's money three extra times is a
worse defect than the one the retry was added to fix.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from runopsy_semantic.provider import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    OpenRouterClient,
    ProviderError,
)

ANSWER = {
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0001},
    "model": "test/model",
}


class Flaky:
    """A transport that fails a set number of times, then answers."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.remaining = failures
        self.error = error
        self.attempts = 0

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.error
        return httpx.Response(200, json=ANSWER, request=httpx.Request("POST", url))


class Answering:
    """A transport that always returns one status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.attempts = 0

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        self.attempts += 1
        body = ANSWER if self.status < 400 else {"error": "no"}
        return httpx.Response(self.status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits instead of taking them, so the schedule is testable at speed."""
    waits: list[float] = []
    monkeypatch.setattr(
        "runopsy_semantic.provider.time.sleep", lambda seconds: waits.append(seconds)
    )
    return waits


def client(transport: object) -> OpenRouterClient:
    return OpenRouterClient("test-key", transport=transport)  # type: ignore[arg-type]


class TestWhatIsRetried:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.TimeoutException("timed out"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectError("refused"),
        ],
    )
    def test_a_request_that_never_arrived_is_sent_again(
        self, error: Exception, slept: list[float]
    ) -> None:
        transport = Flaky(failures=1, error=error)

        result = client(transport).complete("s", "u")

        assert result.text == "ok"
        assert transport.attempts == 2

    def test_it_recovers_at_the_last_permitted_attempt(self, slept: list[float]) -> None:
        transport = Flaky(failures=MAX_ATTEMPTS - 1, error=httpx.ConnectError("refused"))

        result = client(transport).complete("s", "u")

        assert result.text == "ok"
        assert transport.attempts == MAX_ATTEMPTS


class TestWhatIsNotRetried:
    @pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 503])
    def test_an_answer_from_the_provider_is_never_sent_again(
        self, status: int, slept: list[float]
    ) -> None:
        """A status means the provider replied. Repeating cannot mend it, and a 5xx may
        already have run — and billed for — the model."""
        transport = Answering(status)

        with pytest.raises(ProviderError, match="HTTP"):
            client(transport).complete("s", "u")

        assert transport.attempts == 1
        assert slept == []

    def test_a_protocol_error_is_a_bug_rather_than_weather(self, slept: list[float]) -> None:
        transport = Flaky(failures=1, error=httpx.TooManyRedirects("looping"))

        with pytest.raises(ProviderError, match="could not reach"):
            client(transport).complete("s", "u")

        assert transport.attempts == 1
        assert slept == []


class TestTheScheduleAndItsLimit:
    def test_the_waits_are_a_tenth_a_fifth_and_two_fifths_of_a_second(
        self, slept: list[float]
    ) -> None:
        with pytest.raises(ProviderError):
            client(Flaky(failures=99, error=httpx.ConnectError("refused"))).complete("s", "u")

        assert slept == [0.1, 0.2, 0.4]
        assert tuple(slept) == RETRY_BACKOFF_SECONDS

    def test_it_stops_at_the_maximum_and_says_how_many_it_tried(self, slept: list[float]) -> None:
        transport = Flaky(failures=99, error=httpx.ConnectError("refused"))

        with pytest.raises(ProviderError, match=f"after {MAX_ATTEMPTS} attempts"):
            client(transport).complete("s", "u")

        assert transport.attempts == MAX_ATTEMPTS

    def test_it_never_waits_after_the_final_attempt(self, slept: list[float]) -> None:
        """A wait nobody is going to use is a wait charged to the user for nothing."""
        with pytest.raises(ProviderError):
            client(Flaky(failures=99, error=httpx.ConnectError("refused"))).complete("s", "u")

        assert len(slept) == MAX_ATTEMPTS - 1

    def test_a_request_that_works_first_time_waits_for_nothing(self, slept: list[float]) -> None:
        transport = Flaky(failures=0, error=httpx.ConnectError("unused"))

        client(transport).complete("s", "u")

        assert transport.attempts == 1
        assert slept == []


class TestTheOfflinePromiseIsUnaffected:
    def test_the_engine_cannot_reach_this_code_at_all(self) -> None:
        """The retry lives inside the paid path, and the deterministic engine has no way in.

        That the layers spend no tokens is already pinned by the detector and graph tests.
        What this adds is the structural reason it stays true: ``runopsy-core`` does not
        depend on the package the network lives in, so no future edit can quietly make a
        deterministic layer wait 0.7 seconds on a socket.
        """
        import tomllib
        from pathlib import Path

        manifest = Path("packages/runopsy-core/pyproject.toml").read_bytes()
        dependencies = tomllib.loads(manifest.decode("utf-8"))["project"]["dependencies"]

        assert not [name for name in dependencies if "semantic" in name or "httpx" in name]


class TestALocalEndpoint:
    """What makes "local models need no key" true rather than a claim in a README."""

    def test_requests_go_where_they_are_pointed(self) -> None:
        transport = Flaky(failures=0, error=httpx.ConnectError("unused"))
        local = "http://localhost:11434/v1/chat/completions"

        OpenRouterClient("local", transport=transport, base_url=local).complete("s", "u")

        assert transport.attempts == 1

    def test_an_empty_setting_still_means_openrouter(self) -> None:
        from runopsy_semantic.provider import OPENROUTER_URL

        assert OpenRouterClient("k", base_url="").base_url == OPENROUTER_URL
        assert OpenRouterClient("k").is_local is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434/v1/chat/completions",
            "http://127.0.0.1:8080/v1/chat/completions",
        ],
    )
    def test_a_loopback_endpoint_is_recognised_as_local(self, url: str) -> None:
        """The check that lets the CLI stop demanding a credential it does not need."""
        from runopsy_cli.main import _is_local_endpoint

        assert OpenRouterClient("k", base_url=url).is_local
        assert _is_local_endpoint(url)
