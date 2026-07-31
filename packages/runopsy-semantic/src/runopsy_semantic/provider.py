"""Talking to a provider, with the user's own key.

OpenRouter's OpenAI-compatible endpoint. Nothing here is bundled or proxied: the key
comes from the caller, which got it from the environment or the user's keyring, and a
missing key is a reason to stay offline rather than an error.

The transport is injectable so tests never touch the network. A live check exists and is
skipped unless explicitly enabled — a test suite that costs money on every run is a test
suite people disable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_VARIABLE = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "openai/gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 60.0

RETRY_BACKOFF_SECONDS: Final = (0.1, 0.2, 0.4)
"""How long to wait before each retry. Its length is how many retries there are."""

MAX_ATTEMPTS: Final = len(RETRY_BACKOFF_SECONDS) + 1

RETRYABLE = (httpx.TimeoutException, httpx.ConnectError)
"""The only two failures worth trying again.

Deliberately narrow, and the narrowness is the design rather than caution. Both mean the
request did not arrive: a timeout waiting for the first byte, or a connection that was
never established. Nothing was computed, nothing was billed, and the same request sent
again is the same request.

Everything else is excluded for a reason. An HTTP error means the provider answered, and
a 4xx says the request is wrong — sending it three more times cannot make it right, and a
429 answered with an immediate retry storm is how a rate limit becomes a ban. A 5xx may
have run the model before failing, so a retry can be billed twice. A malformed response
body is a bug, not weather. The user's money and the provider's patience are both finite,
and this is the boundary that respects them.
"""


class ProviderError(RuntimeError):
    """The provider could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class Completion:
    """One model response, with what it cost."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


class Transport(Protocol):
    """The one network operation this package performs."""

    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> httpx.Response: ...


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Find a key, or report that there is none.

    Delegates to the credential resolver so every caller sees the same order: flag,
    environment, OS keyring, then a developer .env. Returning ``None`` rather than
    raising is deliberate — no key means the deterministic diagnosis still runs and
    still answers, which is the whole point of the offline-first design.
    """
    from runopsy_semantic.credentials import resolve

    found = resolve(explicit)
    return found.key if found else None


class OpenRouterClient:
    """A minimal client for one endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        transport: Transport | None = None,
        referer: str = "https://github.com/vahit19/runopsy",
        base_url: str = "",
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._transport = transport
        self._referer = referer
        # Any OpenAI-compatible chat-completions endpoint. Empty means OpenRouter.
        #
        # This is what makes "local models require no key at all" true rather than a
        # claim: pointed at Ollama or llama.cpp the semantic layer runs with nothing
        # leaving the machine, which is the promise the rest of the design already keeps
        # and this one package could not.
        self.base_url = base_url.strip() or OPENROUTER_URL

    @property
    def is_local(self) -> bool:
        """Whether the configured endpoint keeps the request on this machine."""
        return any(host in self.base_url for host in ("localhost", "127.0.0.1", "[::1]"))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution; neither carries trace content.
            "HTTP-Referer": self._referer,
            "X-Title": "Runopsy",
        }

    def _send(self, body: dict[str, Any]) -> httpx.Response:
        if self._transport is not None:
            return self._transport.post(self.base_url, headers=self._headers(), json=body)
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            return client.post(self.base_url, headers=self._headers(), json=body)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """One request, tried again only when it never arrived.

        There was no retry here at all, which made the whole hybrid diagnosis a coin
        toss against one TCP connection: a single dropped packet on a home network threw
        away a run the user had already recorded, already analysed deterministically, and
        already agreed to spend money on. The failure it protects against is not exotic —
        it is the ordinary shape of a laptop's network.

        Backoff is 0.1s, 0.2s, 0.4s: four attempts spanning 0.7 seconds of waiting. Short
        enough that nobody watching the terminal decides it has hung, doubling so a
        provider that is briefly overwhelmed is not hit at a fixed rhythm, and finite
        because a diagnosis that never returns is worse than one that says it could not
        reach the provider.
        """
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._send(body)
            except RETRYABLE as error:
                last = error
                if attempt < len(RETRY_BACKOFF_SECONDS):
                    time.sleep(RETRY_BACKOFF_SECONDS[attempt])
            except httpx.HTTPError as error:
                # Reached the provider, or failed in a way repeating cannot mend.
                msg = f"could not reach the provider: {error}"
                raise ProviderError(msg) from error

        msg = f"could not reach the provider after {MAX_ATTEMPTS} attempts: {last}"
        raise ProviderError(msg) from last

    def complete(self, system: str, user: str, *, max_output_tokens: int = 700) -> Completion:
        """Send one prompt and return the response.

        ``temperature`` is zero because a diagnosis that changes between identical runs
        cannot be cached, compared, or trusted — the same property the deterministic
        layers hold themselves to.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "usage": {"include": True},
        }

        response = self._post(body)

        if response.status_code >= 400:
            # The body can echo request content; only the status is surfaced, so a
            # provider error cannot become a second path for trace data to escape.
            msg = f"provider returned HTTP {response.status_code}"
            raise ProviderError(msg)

        try:
            payload = response.json()
            choice = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            msg = "provider response had an unexpected shape"
            raise ProviderError(msg) from error

        usage = payload.get("usage") or {}
        return Completion(
            text=str(choice or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
            model=str(payload.get("model") or self.model),
        )
