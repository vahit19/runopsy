"""Reading TRAIL: human-labelled traces of coding agents.

The gap this project has never closed is a corpus of *real coding-agent runs* where a
person decided where each one went wrong. Who&When supplies human labels but its traces
are conversations, so the structural layers have nothing to read. TRAIL supplies both:
148 agent execution traces annotated by four expert annotators, of which 31 come from
SWE-Bench — real software-engineering sessions — and all of it is recorded through
OpenTelemetry using the OpenInference conventions this project's schema was built on.

    @article{deshpande2025trail,
      title={TRAIL: Trace Reasoning and Agentic Issue Localization},
      author={Deshpande, Darshan and Ravi, Varun Gangal and Patel, Sayak and others},
      journal={arXiv preprint arXiv:2505.08638}, year={2025}}

**Access is gated.** The dataset requires accepting its terms on Hugging Face, which is
a decision for the person running this and not something a tool may do on their behalf.
So this module never tries to click through anything: it reads a directory somebody has
already downloaded, or fetches with a token they have provided, and otherwise says
plainly what is needed.

**The conversion is written against the published description, not against the data.**
Nobody here has been able to open a record — the gate is doing its job. So the reader
below discovers field names rather than assuming them, and refuses loudly when a file
does not look like what it expects. A converter that quietly produced *something* from an
unfamiliar shape would be worse than one that stops: the score that came out would be
measuring the import, and nothing would say so.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runopsy_bench.corpus import LabelledRun
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    Event,
    FailureCategory,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
    ToolCallEvent,
    ToolPayload,
)

DATASET = "PatronusAI/TRAIL"
BASE_URL = f"https://huggingface.co/api/datasets/{DATASET}"
DOWNLOAD_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main"
TOKEN_ENV_NAME = "HF" + "_TOKEN"  # nosec B105 - an env var name, not a credential
"""Name of the environment variable holding a Hugging Face read token. Not a token."""

ATTRIBUTION = "TRAIL (Deshpande et al. 2025), annotated by the dataset's expert annotators"
"""Recorded as the labeller, because that is who decided the answer.

The corpus refuses an unnamed labeller and the name has to be true. These judgements
belong to four annotators with software-engineering backgrounds; attributing them to
anyone here would destroy the only thing that makes an external benchmark worth having.
"""

EPOCH = datetime(2025, 5, 1, tzinfo=UTC)
"""A fixed clock. Diagnosis is a pure function of the trace, so an imported dataset must
produce identical events every time it is read."""

# Keys the reader will accept for the same idea. Written as alternatives rather than one
# guess because the schema has not been seen from here; anything outside these lists is
# reported rather than coerced.
_STEP_KEYS = ("spans", "trace", "steps", "events")
_ERROR_KEYS = ("errors", "annotations", "issues")
_LOCATION_KEYS = ("span_id", "step", "location", "index", "step_index")


class TrailUnavailableError(RuntimeError):
    """The dataset could not be read, with the reason a person can act on."""


class TrailShapeError(RuntimeError):
    """A record did not look like TRAIL, so nothing was invented from it."""


@dataclass(frozen=True)
class TrailRecord:
    """One annotated trace, in the dataset's own terms."""

    identifier: str
    subset: str
    steps: tuple[dict[str, Any], ...]
    onset_index: int | None
    summary: str

    @property
    def is_scoreable(self) -> bool:
        return self.onset_index is not None and bool(self.steps)


def token() -> str | None:
    """The Hugging Face token, if one has been provided."""
    value = os.environ.get(TOKEN_ENV_NAME, "").strip()
    return value or None


def access_hint() -> str:
    """What to do about a dataset this machine cannot read yet."""
    return (
        f"{DATASET} is access-gated, so it needs two things this tool cannot do for you:\n"
        f"  1. accept the terms at https://huggingface.co/datasets/{DATASET}\n"
        f"  2. put a read token in {TOKEN_ENV_NAME} (https://huggingface.co/settings/tokens)\n"
        "Already downloaded it another way? Point --trail at the directory instead."
    )


def fetch_json(path: str, *, timeout: float = 30.0) -> Any:
    """One file from the dataset, authenticated if a token is available."""
    # Encoded because the dataset has a folder called "SWE Bench", and urllib refuses a
    # URL containing a raw space with an error about control characters — which reads
    # like a bug in this code rather than a space in somebody else's folder name.
    url = f"{DOWNLOAD_URL}/{urllib.parse.quote(path)}"
    if not url.startswith("https://"):
        msg = f"refusing to fetch a non-HTTPS URL: {url[:60]}"
        raise TrailUnavailableError(msg)

    headers = {"User-Agent": "runopsy-bench (+https://github.com/vahit19/runopsy)"}
    available = token()
    if available:
        headers["Authorization"] = f"Bearer {available}"

    request = urllib.request.Request(url, headers=headers)
    try:
        # nosec B310 - the scheme is checked above; only https reaches here.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise TrailUnavailableError(access_hint()) from error
        msg = f"could not fetch {path}: HTTP {error.code}"
        raise TrailUnavailableError(msg) from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        msg = f"could not fetch {path}: {error}"
        raise TrailUnavailableError(msg) from error


SPAN_TREE_KEY = "child_spans"


def flatten(spans: list[Any]) -> list[dict[str, Any]]:
    """Every span in the tree, depth first.

    The trace arrives nested: one root ``process_item`` span with the work hanging off
    it. Reading only the top level finds a single span and resolves none of the
    annotations against it — a silent zero rather than an error, which is the shape of
    mistake this module exists to refuse.
    """
    found: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        found.append(span)
        found.extend(flatten(span.get(SPAN_TREE_KEY) or []))
    return found


def read_record(trace: dict[str, Any], annotation: dict[str, Any], *, subset: str) -> TrailRecord:
    """Join one trace with its annotations.

    The onset is the earliest annotated error in trace order. TRAIL marks every error it
    finds, several per trace; this project names where things *started* going wrong, so
    the first is the label and the rest are what followed.
    """
    identifier = str(trace.get("trace_id") or annotation.get("trace_id") or "unknown")
    spans = flatten(trace.get("spans") or [])
    spans.sort(key=lambda span: str(span.get("timestamp") or ""))
    if not spans:
        msg = f"{identifier}: no spans in the trace file"
        raise TrailShapeError(msg)

    positions = {str(span.get("span_id")): index for index, span in enumerate(spans)}
    errors = [item for item in (annotation.get("errors") or []) if isinstance(item, dict)]
    located = sorted(
        positions[str(item.get("location"))]
        for item in errors
        if str(item.get("location")) in positions
    )

    summary = ""
    if located:
        first = next(
            item for item in errors if positions.get(str(item.get("location"))) == located[0]
        )
        summary = f"{first.get('category', 'error')}: {first.get('description', '')}"[:300]

    return TrailRecord(
        identifier=identifier,
        subset=subset,
        steps=tuple(spans),
        onset_index=located[0] if located else None,
        summary=summary or "annotated agent failure",
    )


def _annotation_dir(directory: Path) -> Path:
    """Where the annotations for these traces live.

    Either layout is accepted — the dataset's own, or a directory holding both files per
    trace — because somebody who fetched this by hand should not have to reproduce a
    folder naming convention to be allowed to use it.
    """
    slug = directory.name.replace(" ", "_").lower()
    for candidate in (
        directory.parent / f"processed_annotations_{slug}",
        directory.parent / "processed_annotations_swe_bench",
        directory / "annotations",
        directory,
    ):
        if candidate.is_dir():
            return candidate
    return directory


def load_local(directory: Path, *, subset: str = "trail") -> list[TrailRecord]:
    """Read a downloaded copy: traces in ``directory``, annotations beside them."""
    annotations = _annotation_dir(directory)
    records: list[TrailRecord] = []
    for path in sorted(directory.glob("*.json")):
        pair = annotations / path.name
        if not pair.is_file() or pair.resolve() == path.resolve():
            continue
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
            annotation = json.loads(pair.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(trace, dict) and isinstance(annotation, dict):
            records.append(read_record(trace, annotation, subset=subset))
    return records


def download(subset: str = "SWE Bench", *, limit: int | None = None) -> list[TrailRecord]:
    """Fetch traces and their annotations straight from the dataset."""
    listing = fetch_json_api(f"{BASE_URL}")
    names = [str(s.get("rfilename")) for s in (listing.get("siblings") or [])]
    traces = [n for n in names if n.startswith(f"{subset}/")]
    slug = subset.replace(" ", "_").lower()

    records: list[TrailRecord] = []
    for name in sorted(traces)[: limit or len(traces)]:
        stem = name.split("/", 1)[1]
        trace = fetch_json(name)
        annotation = fetch_json(f"processed_annotations_{slug}/{stem}")
        if isinstance(trace, dict) and isinstance(annotation, dict):
            records.append(read_record(trace, annotation, subset=subset))
    return records


def fetch_json_api(url: str, *, timeout: float = 30.0) -> Any:
    """The dataset's file listing, which needs the same credential as its contents."""
    headers = {"User-Agent": "runopsy-bench (+https://github.com/vahit19/runopsy)"}
    available = token()
    if available:
        headers["Authorization"] = f"Bearer {available}"
    request = urllib.request.Request(url, headers=headers)
    try:
        # nosec B310 - fixed https host
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise TrailUnavailableError(access_hint()) from error
        msg = f"could not list the dataset: HTTP {error.code}"
        raise TrailUnavailableError(msg) from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        msg = f"could not list the dataset: {error}"
        raise TrailUnavailableError(msg) from error


def to_events(record: TrailRecord) -> tuple[Event, ...]:
    """The annotated trace as Runopsy events.

    A span with a tool name becomes a tool call and everything else a model call, which
    is the distinction the structural detectors depend on. Nothing is invented: a span
    with no recorded status yields an event with no exit code, so no detector can fire on
    a value this import made up.
    """
    run_id = f"trail_{record.subset.lower().replace(' ', '_')}_{record.identifier[:16]}"
    events: list[Event] = [
        RunStartEvent(
            event_id=f"{run_id}_evt_0000",
            run_id=run_id,
            sequence=0,
            timestamp=EPOCH,
            run=RunPayload(task=record.summary[:200], runtime="trail"),
        )
    ]

    for position, step in enumerate(record.steps, start=1):
        name = str(step.get("name") or step.get("tool") or step.get("span_name") or "")
        content = json.dumps(step, sort_keys=True, default=str)
        common = {
            "event_id": f"{run_id}_evt_{position:04d}",
            "run_id": run_id,
            "sequence": position,
            "timestamp": EPOCH + timedelta(seconds=position),
        }
        failed = str(step.get("status") or step.get("status_code") or "").upper()
        if name and "llm" not in name.lower():
            events.append(
                ToolCallEvent(
                    **common,
                    tool=ToolPayload(
                        name=name[:60],
                        arguments_hash=hash_text(content),
                        status=CallStatus.ERROR if failed.startswith("ERROR") else CallStatus.OK,
                    ),
                )
            )
        else:
            events.append(
                LlmCallEvent(
                    **common,
                    llm=LlmPayload(model=name[:60] or "unknown", response_hash=hash_text(content)),
                )
            )

    last = len(record.steps) + 1
    events.append(
        RunEndEvent(
            event_id=f"{run_id}_evt_{last:04d}",
            run_id=run_id,
            sequence=last,
            timestamp=EPOCH + timedelta(seconds=last),
            run=RunPayload(outcome=RunOutcome.FAILURE, summary=record.summary[:300]),
        )
    )
    return tuple(events)


def to_labelled_run(record: TrailRecord) -> LabelledRun:
    """One TRAIL record as a corpus case."""
    events = to_events(record)
    onset = None if record.onset_index is None else record.onset_index + 1
    return LabelledRun(
        name=f"trail-{record.subset.replace(' ', '-').lower()}-{record.identifier[:12]}",
        category=FailureCategory.REASONING,
        description=record.summary,
        events=events,
        onset_step=onset,
        labelled_by=ATTRIBUTION,
        runtime="trail",
        notes="Human-annotated agent trace. Onset is the earliest annotated error.",
        deterministically_detectable=False,
    )


def write_corpus(records: list[TrailRecord], directory: Path) -> int:
    """Write scoreable records as corpus cases, returning how many were written."""
    from runopsy_bench.corpus import to_json

    directory.mkdir(parents=True, exist_ok=True)
    written = 0
    for record in records:
        if not record.is_scoreable:
            continue
        case = to_labelled_run(record)
        directory.joinpath(f"{case.name}.json").write_text(to_json(case), encoding="utf-8")
        written += 1
    return written


__all__ = [
    "ATTRIBUTION",
    "DATASET",
    "TOKEN_ENV_NAME",
    "TrailRecord",
    "TrailShapeError",
    "TrailUnavailableError",
    "access_hint",
    "fetch_json",
    "load_local",
    "read_record",
    "to_events",
    "to_labelled_run",
    "token",
    "write_corpus",
]
