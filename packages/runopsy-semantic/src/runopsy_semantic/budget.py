"""Spending limits, enforced before the call rather than after.

Checking a budget after the request has already gone out is how people discover a
ceiling by reading an invoice. Every limit here is tested before a call is made, and the
ledger records what was actually spent so the diagnosis can report its own cost.

Defaults come from section 12.1 of the design document. They are deliberately small: the
deterministic engine already answered the question, and this layer is a paid second
opinion on a handful of steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

MAX_DIAGNOSTIC_CALLS: Final = 2
MAX_INPUT_TOKENS: Final = 6_000
MAX_COST_USD: Final = 0.10


class BudgetExceededError(RuntimeError):
    """Raised when a call would cross a ceiling. Never raised after one has been made."""


@dataclass(frozen=True)
class Budget:
    """What this diagnosis is allowed to spend."""

    max_calls: int = MAX_DIAGNOSTIC_CALLS
    max_input_tokens: int = MAX_INPUT_TOKENS
    max_cost_usd: float = MAX_COST_USD

    @property
    def disabled(self) -> bool:
        """A zero call ceiling means the semantic layer is off entirely."""
        return self.max_calls <= 0


@dataclass
class Ledger:
    """What has been spent so far, and whether more is permitted."""

    budget: Budget = field(default_factory=Budget)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached_hits: int = 0
    stopped_because: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check(self, planned_input_tokens: int) -> None:
        """Refuse a call that would exceed any ceiling.

        The token estimate is checked against the per-call limit rather than a running
        total, because a single oversized payload is the failure this guards: the point
        of data minimization is that no one request carries the whole trace.
        """
        if self.budget.disabled:
            msg = "semantic analysis is disabled (max_calls is 0)"
            raise BudgetExceededError(msg)
        if self.calls >= self.budget.max_calls:
            msg = f"call ceiling reached ({self.budget.max_calls})"
            raise BudgetExceededError(msg)
        if planned_input_tokens > self.budget.max_input_tokens:
            msg = (
                f"payload of ~{planned_input_tokens} tokens exceeds the per-call limit "
                f"of {self.budget.max_input_tokens}"
            )
            raise BudgetExceededError(msg)
        if self.cost_usd >= self.budget.max_cost_usd:
            msg = f"cost ceiling reached (${self.budget.max_cost_usd:.4f})"
            raise BudgetExceededError(msg)

    def can_afford(self, planned_input_tokens: int) -> bool:
        """Whether ``check`` would allow the call, without raising."""
        try:
            self.check(planned_input_tokens)
        except BudgetExceededError as error:
            self.stopped_because = str(error)
            return False
        return True

    def record(self, *, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        """Register what a completed call actually cost."""
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd

    def record_cache_hit(self) -> None:
        """A repeated question answered from disk, spending nothing."""
        self.cached_hits += 1

    def describe(self) -> str:
        """One line for the CLI footer."""
        if not self.calls and not self.cached_hits:
            return "no model calls"
        parts = [f"{self.calls} model call(s)", f"{self.total_tokens} tokens"]
        if self.cost_usd:
            parts.append(f"${self.cost_usd:.4f}")
        if self.cached_hits:
            parts.append(f"{self.cached_hits} from cache")
        return ", ".join(parts)


def estimate_tokens(text: str) -> int:
    """A deliberately rough upper bound on a payload's size.

    Roughly four characters per token, rounded up. Precision is not the goal — the
    budget check needs to refuse an oversized payload before sending it, and no tokenizer
    is worth a dependency for a limit that exists to catch order-of-magnitude mistakes.
    """
    return (len(text) + 3) // 4
