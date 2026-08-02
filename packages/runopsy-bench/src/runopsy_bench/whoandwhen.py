"""Scoring against somebody else's labels.

Every number this project reports comes from traces it wrote itself, which is the
weakest thing about its evidence: a benchmark whose author also wrote the engine
measures agreement with its own assumptions. Who&When is the first external answer to
that. It is 184 failure logs from real multi-agent systems, each annotated by hand with
*the decisive error step* — defined by its authors as the earliest mistake whose
correction would turn the failure into a success.

That definition is worth reading twice, because it is Runopsy's definition of a failure
onset, arrived at independently, and it carries the same counterfactual criterion the
replay engine tests. Being scored on it is the difference between "our ranking behaves
as we designed it" and "our ranking agrees with an outside expert about where a run went
wrong".

**The data is downloaded, never vendored.** The dataset card states no licence — the
project's own repository is MIT, but the two are not the same document — so redistributing
copies inside this repository would be claiming a permission nobody granted. Fetching it
at use time also keeps this repository small and the data authoritative.

**These are conversations, not command traces, and that changes what is being measured.**
Every entry is an agent turn: prose, no exit code, no tool status, no repository. The
deterministic layers have nothing to look at, so cases import with
``deterministically_detectable=False`` and a low L0-L2 score on them is the honest and
expected result rather than a regression. What Who&When can genuinely measure here is the
semantic layer, and the import exists so that measurement is possible at all.

    @article{zhang2025agent,
      title={Which Agent Causes Task Failures and When? On Automated Failure
             Attribution of LLM Multi-Agent Systems},
      author={Zhang, Shaokun and Yin, Ming and Zhang, Jieyu and Liu, Jiale and
              Han, Zhiguang and Zhang, Jingyang and Li, Beibin and Wang, Chi and
              Wang, Huazheng and Chen, Yiran and others},
      journal={arXiv preprint arXiv:2505.00212}, year={2025}}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runopsy_bench.corpus import LabelledRun
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    Event,
    FailureCategory,
    LlmCallEvent,
    LlmPayload,
    RunEndEvent,
    RunOutcome,
    RunPayload,
    RunStartEvent,
)

DATASET = "Kevin355/Who_and_When"
SUBSETS = ("Hand-Crafted", "Algorithm-Generated")
BASE_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main"

ATTRIBUTION = "Who&When (Zhang et al. 2025), annotated by the dataset's authors"
"""Recorded as the labeller.

The corpus refuses a case with no named labeller, and the name has to be true. These
labels are not this project's judgement and must never be attributable to it — the whole
value of an external benchmark is that somebody else decided the answer.
"""

# The trace is a conversation, so the clock is a fiction. It has to be *some* fixed
# fiction: diagnosis is a pure function of the trace, and a wall clock would make the
# same dataset produce a different bundle on every import.
EPOCH = datetime(2025, 5, 1, tzinfo=UTC)

USER_AGENT = "runopsy-bench (+https://github.com/vahit19/runopsy)"


class DatasetUnavailableError(RuntimeError):
    """The dataset could not be fetched, with the reason a person can act on."""


@dataclass(frozen=True)
class WhoAndWhenRecord:
    """One annotated failure log, in the dataset's own terms."""

    identifier: str
    subset: str
    question: str
    history: tuple[dict[str, Any], ...]
    mistake_agent: str
    mistake_step: int
    mistake_reason: str

    @classmethod
    def from_json(
        cls, document: dict[str, Any], *, identifier: str, subset: str
    ) -> WhoAndWhenRecord:
        history = document.get("history") or []
        return cls(
            identifier=identifier,
            subset=subset,
            question=str(document.get("question") or ""),
            history=tuple(entry for entry in history if isinstance(entry, dict)),
            mistake_agent=str(document.get("mistake_agent") or ""),
            mistake_step=int(document.get("mistake_step") or 0),
            mistake_reason=str(document.get("mistake_reason") or ""),
        )


def fetch(subset: str, index: int, *, timeout: float = 30.0) -> WhoAndWhenRecord | None:
    """One record, or ``None`` when that index does not exist.

    Absence is not an error: the subsets are numbered from 1 with no published count, so
    walking until nothing comes back is how their size is discovered.
    """
    url = f"{BASE_URL}/Who%26When/{subset}/{index}.json"
    document = _read_json(url, subset=subset, index=index, timeout=timeout)
    if document is None or not isinstance(document, dict):
        return None
    return WhoAndWhenRecord.from_json(document, identifier=f"{subset}-{index}", subset=subset)


RETRY_BACKOFF_SECONDS = (0.5, 1.5, 3.0)
"""Waits before re-fetching a record whose request never arrived.

A content delivery network drops connections under a burst of small requests, and
walking 184 files one at a time is exactly that burst — measured here, resetting on the
second file. Retried only for transport failures, for the same reason the provider client
is: an HTTP status means the server answered, and a 404 in particular is how the end of a
subset is discovered. Retrying that would turn "there are 58 records" into an infinite
loop.
"""


def _read_json(url: str, *, subset: str, index: int, timeout: float) -> Any | None:
    """Fetch and parse one file, retrying a connection that never landed."""
    import time

    # Identified rather than anonymous. urllib's default agent string is refused by the
    # host — the connection is reset before any status arrives, which looks like a
    # network fault and is a policy decision. Saying who is calling is also the courteous
    # thing when walking someone's dataset file by file.
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )

    last: Exception | None = None
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None  # the subset ends here; not a failure
            msg = f"could not fetch {subset}/{index}: HTTP {error.code}"
            raise DatasetUnavailableError(msg) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last = error
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])

    msg = (
        f"could not fetch {subset}/{index} after {len(RETRY_BACKOFF_SECONDS) + 1} attempts: {last}"
    )
    raise DatasetUnavailableError(msg) from last


def download(
    *, limit: int | None = None, subsets: tuple[str, ...] = SUBSETS
) -> list[WhoAndWhenRecord]:
    """Walk each subset from 1 until it runs out, or until ``limit`` records are held."""
    collected: list[WhoAndWhenRecord] = []
    for subset in subsets:
        index = 1
        while limit is None or len(collected) < limit:
            record = fetch(subset, index)
            if record is None:
                break
            collected.append(record)
            index += 1
    return collected


def load_local(directory: Path, *, subset: str = "local") -> list[WhoAndWhenRecord]:
    """Read records from a copy already on disk, in a stable order.

    The network path is a convenience, not the contract. Corporate proxies reset Python's
    TLS while leaving other clients alone — measured on the machine this was written on —
    and a benchmark that can only be run by people with unfiltered egress is not a
    benchmark anybody can reproduce. Anyone can fetch the dataset with the tool of their
    choice, point this at the directory, and get the same cases.

    Files are read in numeric order where their names allow it, so a corpus built twice
    is the same corpus.
    """

    def ordering(path: Path) -> tuple[int, str]:
        return (int(path.stem) if path.stem.isdigit() else 1 << 30, path.stem)

    records: list[WhoAndWhenRecord] = []
    for path in sorted(directory.glob("*.json"), key=ordering):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("history"):
            records.append(
                WhoAndWhenRecord.from_json(document, identifier=path.stem, subset=subset)
            )
    return records


def to_events(record: WhoAndWhenRecord) -> tuple[Event, ...]:
    """The conversation as a Runopsy trace.

    Each turn becomes an ``llm_call`` attributed to the agent that spoke, because that is
    what it is: a model producing text. Nothing is invented — no exit codes, no tool
    statuses, no durations — since a fabricated field would be indistinguishable from a
    recorded one to every layer above, and the first thing a reader would conclude from a
    finding built on it is that Runopsy had seen something it had not.
    """
    run_id = f"ww_{record.subset.lower().replace('-', '_')}_{record.identifier.rsplit('-', 1)[-1]}"
    events: list[Event] = [
        RunStartEvent(
            event_id=f"{run_id}_evt_0000",
            run_id=run_id,
            sequence=0,
            timestamp=EPOCH,
            run=RunPayload(task=record.question[:500], runtime="who-and-when"),
        )
    ]

    for position, turn in enumerate(record.history, start=1):
        content = str(turn.get("content") or "")
        speaker = str(turn.get("role") or turn.get("name") or "agent") or "agent"
        events.append(
            LlmCallEvent(
                event_id=f"{run_id}_evt_{position:04d}",
                run_id=run_id,
                agent_id=_identifier(speaker),
                sequence=position,
                timestamp=EPOCH + timedelta(seconds=position),
                llm=LlmPayload(
                    model="unknown",
                    response_hash=hash_text(content) if content else None,
                ),
            )
        )

    last = len(record.history) + 1
    events.append(
        RunEndEvent(
            event_id=f"{run_id}_evt_{last:04d}",
            run_id=run_id,
            sequence=last,
            timestamp=EPOCH + timedelta(seconds=last),
            run=RunPayload(outcome=RunOutcome.FAILURE, summary=record.mistake_reason[:500]),
        )
    )
    return tuple(events)


def _identifier(name: str) -> str:
    """An agent id the schema will accept, from a free-text speaker name."""
    cleaned = "".join(character if character.isalnum() else "_" for character in name).strip("_")
    return (cleaned or "agent")[:60]


def to_labelled_run(record: WhoAndWhenRecord) -> LabelledRun:
    """One dataset record as a corpus case this project can already score.

    ``deterministically_detectable`` is False for every one of them, and that is a
    statement about the data rather than modesty about the engine: a conversation carries
    no exit code, no tool status and no repository, so there is nothing for the
    structural and behavioural layers to find. Marking them otherwise would let a
    deterministic score be quoted for cases the deterministic layers cannot see.
    """
    events = to_events(record)
    onset = record.mistake_step if 1 <= record.mistake_step <= len(record.history) else None
    return LabelledRun(
        name=f"whoandwhen-{record.identifier}",
        category=FailureCategory.REASONING,
        description=record.mistake_reason[:300] or "annotated multi-agent failure",
        events=events,
        onset_step=onset,
        labelled_by=ATTRIBUTION,
        runtime="who-and-when",
        notes=(
            f"agent blamed by the dataset: {record.mistake_agent}. "
            "Conversation trace: no exit codes, tool statuses or repository state, so the "
            "deterministic layers have nothing to read."
        ),
        deterministically_detectable=False,
    )


def write_corpus(records: list[WhoAndWhenRecord], directory: Path) -> int:
    """Write the records as corpus cases, returning how many were written."""
    from runopsy_bench.corpus import to_json

    directory.mkdir(parents=True, exist_ok=True)
    written = 0
    for record in records:
        case = to_labelled_run(record)
        if case.onset_step is None:
            # An annotation that points outside its own history cannot be scored, and
            # silently keeping it would count a case nobody can pass.
            continue
        directory.joinpath(f"{case.name}.json").write_text(to_json(case), encoding="utf-8")
        written += 1
    return written


__all__ = [
    "ATTRIBUTION",
    "DATASET",
    "DatasetUnavailableError",
    "WhoAndWhenRecord",
    "download",
    "fetch",
    "to_events",
    "to_labelled_run",
    "write_corpus",
]
