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
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_VARIABLE = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "openai/gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 60.0


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

    Returning ``None`` rather than raising is deliberate: no key means the deterministic
    diagnosis still runs and still answers, which is the whole point of the offline-first
    design.
    """
    return explicit or os.environ.get(API_KEY_VARIABLE) or None


class OpenRouterClient:
    """A minimal client for one endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        transport: Transport | None = None,
        referer: str = "https://github.com/vahit19/runopsy",
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._transport = transport
        self._referer = referer

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution; neither carries trace content.
            "HTTP-Referer": self._referer,
            "X-Title": "Runopsy",
        }

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

        try:
            if self._transport is not None:
                response = self._transport.post(OPENROUTER_URL, headers=self._headers(), json=body)
            else:
                with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = client.post(OPENROUTER_URL, headers=self._headers(), json=body)
        except httpx.HTTPError as error:
            msg = f"could not reach the provider: {error}"
            raise ProviderError(msg) from error

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
