"""Calibrated wording.

Every phrase a user reads about causality is decided here rather than at each call site,
so the rule "never claim a cause without validation" is enforced in one auditable place
instead of depending on whoever writes the next template string.
"""

from __future__ import annotations

from typing import Final

from runopsy_core.schema import DiagnosisCandidate, DiagnosisStatus

FORBIDDEN_WITHOUT_VALIDATION: Final = ("root cause", "caused by", "the cause is")
"""Phrases that assert causation. Only a validated candidate may use them."""

_STATUS_HEADING: Final = {
    DiagnosisStatus.OBSERVED_FAILURE: "Observed failure",
    DiagnosisStatus.SUSPECTED_ONSET: "Suspected onset",
    DiagnosisStatus.CORRELATED_CAUSE: "Correlated step",
    DiagnosisStatus.REPLAY_SUPPORTED: "Cause, supported by replay",
    DiagnosisStatus.HUMAN_VERIFIED: "Cause, verified by a person",
    DiagnosisStatus.UNKNOWN: "Not established",
}

_STATUS_GLOSS: Final = {
    DiagnosisStatus.OBSERVED_FAILURE: "what the run visibly got wrong",
    DiagnosisStatus.SUSPECTED_ONSET: "where it may have started going wrong, unverified",
    DiagnosisStatus.CORRELATED_CAUSE: "related in time, not shown to be causal",
    DiagnosisStatus.REPLAY_SUPPORTED: "changing this step changed the outcome",
    DiagnosisStatus.HUMAN_VERIFIED: "confirmed by review",
    DiagnosisStatus.UNKNOWN: "not enough evidence to say",
}

_STATUS_STYLE: Final = {
    DiagnosisStatus.OBSERVED_FAILURE: "bold red",
    DiagnosisStatus.SUSPECTED_ONSET: "bold yellow",
    DiagnosisStatus.CORRELATED_CAUSE: "dim",
    DiagnosisStatus.REPLAY_SUPPORTED: "bold green",
    DiagnosisStatus.HUMAN_VERIFIED: "bold green",
    DiagnosisStatus.UNKNOWN: "dim",
}


def heading(status: DiagnosisStatus) -> str:
    """Section title for a status."""
    return _STATUS_HEADING[status]


def gloss(status: DiagnosisStatus) -> str:
    """Plain-language explanation of what the status means."""
    return _STATUS_GLOSS[status]


def style(status: DiagnosisStatus) -> str:
    """Rich style for a status."""
    return _STATUS_STYLE[status]


def confidence_phrase(candidate: DiagnosisCandidate) -> str:
    """How sure we are, worded so it cannot be misread as certainty.

    Percentages invite over-reading, so an unvalidated candidate always carries the word
    "unverified" beside the number rather than the number alone.
    """
    percent = round(candidate.confidence * 100)
    if candidate.is_definitive:
        return f"{percent}% confidence, validated"
    return f"{percent}% confidence, unverified"


def next_step_hint(run_id: str, candidate: DiagnosisCandidate, step: int | None) -> str:
    """Tell the user how to turn a suspicion into something confirmed.

    The goal is not to sound certain but to make certainty cheap, so an unvalidated
    finding always ends with the command that would test it.
    """
    if candidate.is_definitive:
        return ""
    # Planning is what `replay` does by default; executing takes an explicit --execute.
    # This once suggested a --dry-run flag that does not exist, so the one command the
    # output offers to make certainty cheap failed with a usage error.
    target = f" --from-step {step}" if step is not None else ""
    return (
        "No cause has been confirmed. To test this candidate, replay from it:\n"
        f"  runopsy replay {run_id}{target}"
    )


def asserts_causation(text: str) -> bool:
    """Whether a rendered string claims causation.

    Used by the tests to keep unvalidated output honest as the wording evolves.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in FORBIDDEN_WITHOUT_VALIDATION)
