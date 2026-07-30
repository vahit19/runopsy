"""Exporting a trace as OpenInference-shaped OpenTelemetry spans.

The design document chose OpenInference semantic conventions and OTLP so that Runopsy
would be framework-agnostic. Until now that claim rested on our own schema being
*modelled* on them, which is not the same thing: a format nobody else can read is not
interoperable, however well it was designed.

This closes that gap in the direction that costs least and delivers most — export. A run
recorded by Runopsy can be opened in Phoenix, Langfuse, or anything else that speaks
OpenInference, and the localized onset travels with it as span attributes.

Import is deliberately not attempted. Reading somebody else's spans means guessing which
of their attributes mean what, and a wrong guess produces a confident diagnosis of a
trace we misunderstood. Export makes a claim we can verify; import would make one we
could not.

The privacy rules do not relax here. Content is referenced by hash, exactly as in the
trace, and steps flagged as carrying a credential are exported with their payload
withheld — an export is a sharing surface like any other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from runopsy_core.schema import (
    ClaimEvent,
    DiagnosisBundle,
    Event,
    HandoffEvent,
    LlmCallEvent,
    MemoryOpEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    TraceGraph,
)

SPAN_KIND: Final = "openinference.span.kind"
"""OpenInference's discriminator: AGENT, LLM, TOOL, CHAIN, RETRIEVER."""

REDACTED: Final = "[redacted]"

_NANOS_PER_SECOND: Final = 1_000_000_000


@dataclass(frozen=True)
class Span:
    """One OpenTelemetry span in the shape OTLP/JSON expects."""

    span_id: str
    parent_span_id: str | None
    name: str
    start_unix_nano: int
    end_unix_nano: int
    attributes: dict[str, Any]
    status: str = "UNSET"

    def to_otlp(self, trace_id: str) -> dict[str, Any]:
        return {
            "traceId": trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_unix_nano),
            "endTimeUnixNano": str(self.end_unix_nano),
            "attributes": [
                {"key": key, "value": _otlp_value(value)}
                for key, value in sorted(self.attributes.items())
            ],
            "status": {"code": {"OK": 1, "ERROR": 2}.get(self.status, 0)},
        }


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _nanos(event: Event) -> int:
    return int(event.timestamp.timestamp() * _NANOS_PER_SECOND)


def _span_id(event: Event) -> str:
    """A stable 16-hex-character id derived from the event id.

    Derived rather than random so the same trace exports identically every time, which
    is what lets an export be diffed or re-sent without creating a second copy.
    """
    from runopsy_core.hashing import hash_text

    return hash_text(event.event_id).removeprefix("sha256:")[:16]


def _kind_and_attributes(event: Event, *, redact: bool) -> tuple[str, str, dict[str, Any]]:
    """OpenInference span kind, span name, and attributes for one event."""
    withheld = redact and event.security.contains_secret
    attributes: dict[str, Any] = {
        "runopsy.event_id": event.event_id,
        "runopsy.sequence": event.sequence,
        "runopsy.agent_id": event.agent_id,
    }
    if event.security.contains_secret:
        attributes["runopsy.contains_secret"] = True

    match event:
        case RunStartEvent() | RunEndEvent():
            attributes["runopsy.runtime"] = event.run.runtime
            if event.run.model:
                attributes["llm.model_name"] = event.run.model
            return "AGENT", event.run.task or event.run_id, attributes

        case ToolCallEvent():
            attributes["tool.name"] = event.tool.name
            attributes["runopsy.status"] = event.tool.status.value
            if event.tool.exit_code is not None:
                attributes["runopsy.exit_code"] = event.tool.exit_code
            if event.tool.duration_ms:
                attributes["runopsy.duration_ms"] = event.tool.duration_ms
            if event.tool.arguments_hash:
                # The hash, never the arguments: the same rule the trace itself follows.
                attributes["input.value"] = REDACTED if withheld else event.tool.arguments_hash
            if event.tool.output_hash:
                attributes["output.value"] = REDACTED if withheld else event.tool.output_hash
            return "TOOL", event.tool.name, attributes

        case LlmCallEvent():
            attributes["llm.model_name"] = event.llm.model
            if event.llm.provider:
                attributes["llm.provider"] = event.llm.provider
            if event.llm.tokens.input_tokens:
                attributes["llm.token_count.prompt"] = event.llm.tokens.input_tokens
            if event.llm.tokens.output_tokens:
                attributes["llm.token_count.completion"] = event.llm.tokens.output_tokens
            if event.llm.tokens.total:
                attributes["llm.token_count.total"] = event.llm.tokens.total
            if event.llm.prompt_hash:
                attributes["input.value"] = REDACTED if withheld else event.llm.prompt_hash
            return "LLM", event.llm.model, attributes

        case MemoryOpEvent():
            attributes["runopsy.memory.operation"] = event.memory.operation.value
            attributes["runopsy.memory.key"] = REDACTED if withheld else event.memory.key
            return "RETRIEVER", f"memory {event.memory.operation.value}", attributes

        case ClaimEvent():
            attributes["runopsy.claim.support"] = event.claim.support_status.value
            return "CHAIN", "claim", attributes

        case HandoffEvent():
            attributes["runopsy.handoff.to"] = event.handoff.to_agent_id
            if event.handoff.missing_fields:
                attributes["runopsy.handoff.missing"] = ", ".join(event.handoff.missing_fields)
            return "AGENT", f"handoff to {event.handoff.to_agent_id}", attributes

        case _:
            return "CHAIN", event.kind.value, attributes


def to_spans(
    graph: TraceGraph,
    events: tuple[Event, ...],
    bundle: DiagnosisBundle | None = None,
    *,
    redact: bool = True,
) -> tuple[Span, ...]:
    """Turn a run into spans, annotating the diagnosis onto the steps it concerns.

    The diagnosis travels as attributes rather than as a separate document, so opening
    the trace in another tool shows *where Runopsy thinks it broke* instead of just what
    happened — which is the only reason exporting is worth doing at all.
    """
    ordered = sorted(events, key=lambda event: event.sequence)
    if not ordered:
        return ()

    run_event = next((e for e in ordered if isinstance(e, RunStartEvent)), None)
    root_id = _span_id(run_event) if run_event else None
    end = max(_nanos(event) for event in ordered)

    onset_id = bundle.primary.onset_node_id if bundle and bundle.primary else None
    affected = set(bundle.primary.affected_node_ids) if bundle and bundle.primary else set()

    spans: list[Span] = []
    for event in ordered:
        kind, name, attributes = _kind_and_attributes(event, redact=redact)
        attributes[SPAN_KIND] = kind

        if bundle is not None:
            if event.event_id == bundle.observed_failure_node_id:
                attributes["runopsy.diagnosis.role"] = "observed_failure"
            elif event.event_id == onset_id and bundle.primary is not None:
                attributes["runopsy.diagnosis.role"] = "suspected_onset"
                attributes["runopsy.diagnosis.status"] = bundle.primary.status.value
                attributes["runopsy.diagnosis.confidence"] = bundle.primary.confidence
            elif event.event_id in affected:
                attributes["runopsy.diagnosis.role"] = "possibly_affected"

        failed = attributes.get("runopsy.status") in {"error", "timeout"} or bool(
            attributes.get("runopsy.exit_code")
        )
        is_root = run_event is not None and event.event_id == run_event.event_id
        spans.append(
            Span(
                span_id=_span_id(event),
                parent_span_id=None if is_root else root_id,
                name=name,
                start_unix_nano=_nanos(event),
                end_unix_nano=end if is_root else _nanos(event),
                attributes=attributes,
                status="ERROR" if failed else "OK",
            )
        )
    return tuple(spans)


def to_otlp_json(
    graph: TraceGraph,
    events: tuple[Event, ...],
    bundle: DiagnosisBundle | None = None,
    *,
    redact: bool = True,
    service_name: str = "runopsy",
) -> str:
    """A complete OTLP/JSON document for one run, ready to POST or to open in a viewer."""
    from runopsy_core.hashing import hash_text

    spans = to_spans(graph, events, bundle, redact=redact)
    trace_id = hash_text(graph.run_id).removeprefix("sha256:")[:32]
    document = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "runopsy.run_id", "value": {"stringValue": graph.run_id}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "runopsy", "version": "0.1.0"},
                        "spans": [span.to_otlp(trace_id) for span in spans],
                    }
                ],
            }
        ]
    }
    return json.dumps(document, indent=2, sort_keys=True)
