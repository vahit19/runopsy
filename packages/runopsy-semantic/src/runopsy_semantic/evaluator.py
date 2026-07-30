"""The semantic layer: a paid second opinion, bounded in what it may conclude.

A model can see what structural analysis cannot — that a command wrote the wrong value,
that a claim is not supported by what was actually observed. It can also invent all of
that with total fluency, which is why the boundary matters more than the capability:

- **It cannot produce a status.** Every signal it returns is L3 and feeds evidence and
  the ranking's evaluator term. Nothing here can reach ``replay_supported``; only an
  experiment can, and that path does not run through this module.
- **Its confidence is capped and separate.** A model saying "definitely" is a token
  sequence, not a measurement, so the caller treats a semantic finding as one weak
  signal beside the deterministic ones rather than as an answer.
- **Unparseable output is discarded, not repaired.** Coaxing meaning out of a malformed
  response is how a hallucination becomes a finding.
- **It only sees suspicious spans.** The deterministic layers choose where to look; this
  layer never gets to roam the trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from runopsy_core.hashing import hash_text
from runopsy_core.schema import AnalysisLayer, FailureCategory, FailureSignal, Severity
from runopsy_semantic.budget import Ledger, estimate_tokens
from runopsy_semantic.cache import VerdictCache
from runopsy_semantic.payload import EvidencePacket
from runopsy_semantic.provider import Completion, OpenRouterClient, ProviderError

PROMPT_VERSION: Final = "1"
"""Bumped whenever the prompt changes, so cached verdicts from an older prompt are not
reused as though they answered the current question."""

DETECTOR_NAME: Final = "semantic:span_review"

MAX_SEMANTIC_CONFIDENCE: Final = 0.5
"""Ceiling on a model-only finding.

Below the deterministic engine's own cap, because a fluent explanation is the cheapest
thing a language model produces and the most expensive thing for a user to disbelieve.
"""

_SYSTEM: Final = """\
You review one step of a recorded software-agent run and judge whether that step did \
something semantically wrong — something a status code cannot show.

Examples of what counts: a command that succeeded while writing the wrong value; a \
tool chosen that cannot accomplish the stated goal; a claim contradicted by the \
observed output; context omitted from a handoff.

Rules:
- Judge only the focus step. Neighbouring steps are context.
- If nothing is clearly wrong, say so. "No finding" is the correct and common answer.
- Never speculate about code you were not shown. You see a redacted excerpt, not a repo.
- Base every judgement on the provided fields alone.

Reply with JSON only, no prose, no code fence:
{"finding": true|false, "category": "<one of: goal_input, planning, retrieval, \
tool_selection, tool_arguments, state, memory, handoff, reasoning, validation, \
outcome>", "summary": "<one sentence, under 25 words>", "confidence": <0.0-1.0>}
"""

_CATEGORIES: Final = {
    "goal_input": FailureCategory.GOAL_INPUT,
    "planning": FailureCategory.PLANNING,
    "retrieval": FailureCategory.RETRIEVAL,
    "tool_selection": FailureCategory.TOOL_SELECTION,
    "tool_arguments": FailureCategory.TOOL_ARGUMENTS,
    "state": FailureCategory.STATE,
    "memory": FailureCategory.MEMORY,
    "handoff": FailureCategory.HANDOFF,
    "reasoning": FailureCategory.REASONING,
    "validation": FailureCategory.VALIDATION,
    "outcome": FailureCategory.OUTCOME,
}


@dataclass(frozen=True)
class SemanticVerdict:
    """One model judgement, already normalised and bounded."""

    finding: bool
    category: FailureCategory
    summary: str
    confidence: float
    model: str = ""
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding,
            "category": self.category.value,
            "summary": self.summary,
            "confidence": self.confidence,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, from_cache: bool = False) -> SemanticVerdict:
        return cls(
            finding=bool(data.get("finding")),
            category=_CATEGORIES.get(str(data.get("category")), FailureCategory.REASONING),
            summary=str(data.get("summary") or ""),
            confidence=float(data.get("confidence") or 0.0),
            model=str(data.get("model") or ""),
            from_cache=from_cache,
        )


def parse_verdict(text: str, *, model: str = "") -> SemanticVerdict | None:
    """Read a verdict, or return ``None``.

    Anything unparseable is dropped rather than salvaged. A response that did not follow
    a schema this simple is not one to reconstruct meaning from.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[stripped.find("{") :] if "{" in stripped else stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "finding" not in data:
        return None

    verdict = SemanticVerdict.from_dict({**data, "model": model})
    if verdict.finding and not verdict.summary:
        return None
    return SemanticVerdict(
        finding=verdict.finding,
        category=verdict.category,
        summary=verdict.summary[:200],
        confidence=min(max(verdict.confidence, 0.0), 1.0),
        model=model,
    )


def cache_key(packet: EvidencePacket, model: str) -> str:
    """Identity of a question: this evidence, this model, this prompt version."""
    return hash_text(f"{PROMPT_VERSION}\n{model}\n{packet.to_json()}")


def review_span(
    packet: EvidencePacket,
    client: OpenRouterClient,
    ledger: Ledger,
    *,
    cache: VerdictCache | None = None,
) -> SemanticVerdict | None:
    """Ask about one step, respecting the budget and the cache.

    Returns ``None`` when the budget refuses the call, the provider fails, or the model
    declines to find anything — three outcomes that all mean "the deterministic answer
    stands", and none of which are errors.
    """
    key = cache_key(packet, client.model)
    if cache is not None:
        stored = cache.get(key)
        if stored is not None:
            ledger.record_cache_hit()
            return SemanticVerdict.from_dict(stored, from_cache=True)

    user = packet.to_json()
    if not ledger.can_afford(estimate_tokens(_SYSTEM) + estimate_tokens(user)):
        return None

    try:
        completion: Completion = client.complete(_SYSTEM, user)
    except ProviderError:
        # Recorded as a cost of zero and swallowed: a provider outage must not turn a
        # working deterministic diagnosis into a failed command.
        return None

    ledger.record(
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        cost_usd=completion.cost_usd,
    )

    verdict = parse_verdict(completion.text, model=completion.model)
    if verdict is not None and cache is not None:
        cache.put(key, verdict.to_dict())
    return verdict


def to_signal(verdict: SemanticVerdict, node_id: str) -> FailureSignal | None:
    """Turn a finding into a signal the engine can rank.

    Severity is capped at ``MEDIUM`` however sure the model claims to be. A model has no
    way to know that something is critical; it has a way to say so.
    """
    if not verdict.finding:
        return None
    return FailureSignal(
        signal_id=f"{DETECTOR_NAME}:{node_id}",
        node_id=node_id,
        category=verdict.category,
        severity=Severity.MEDIUM if verdict.confidence >= 0.6 else Severity.LOW,
        layer=AnalysisLayer.L3_SEMANTIC,
        detector=DETECTOR_NAME,
        summary=f"{verdict.summary} (model judgement, unverified)",
    )


def bounded_confidence(raw: float) -> float:
    """Clamp a model's self-reported confidence into the band it is allowed to occupy."""
    return min(max(raw, 0.0), 1.0) * MAX_SEMANTIC_CONFIDENCE
