"""The local API from section 19.2 of the design document.

Local means local. The server binds to the loopback interface, has no authentication,
and is not built to be exposed — because everything it serves is the contents of
somebody's private repository, and an API that could be published would need an entirely
different security posture than one that cannot reach the network.

Two rules follow from that and are enforced rather than documented:

- **Read-mostly.** Recording events is allowed because an adapter may prefer HTTP to a
  subprocess. Replay execution is not: the only thing in Runopsy that can change the
  world stays behind a command a person types, where the plan can be read first.
- **The same redaction as everywhere else.** A payload flagged as holding a credential
  is withheld here exactly as it is in an export. A second surface with weaker rules is
  how the first surface's rules stop mattering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from runopsy_cli.report import render_report
from runopsy_collector import Collector, StorePaths
from runopsy_core import AnalysisContext, apply_replay_evidence, diagnose, infer_affects
from runopsy_core.schema import DiagnosisBundle, Event, TraceGraph
from runopsy_replay import build_plan, evidence_from_stored_run
from runopsy_server import __version__

LOOPBACK_ONLY = "127.0.0.1"


class EventBatch(BaseModel):
    """One or more normalized events to ingest."""

    events: list[dict[str, Any]] = Field(default_factory=list)


class IngestResult(BaseModel):
    recorded: int
    duplicates: int


class RunSummaryOut(BaseModel):
    run_id: str
    task: str
    runtime: str
    outcome: str
    event_count: int
    finished: bool


class GraphOut(BaseModel):
    """Nodes and edges, with inferred propagation kept separate from observation."""

    run_id: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    inferred_edges: list[dict[str, Any]] = Field(default_factory=list)


def _store_dependency(root: Path | None) -> Any:
    def dependency() -> Any:
        collector = Collector.open(root)
        try:
            yield collector
        finally:
            collector.close()

    return dependency


def _analyse(collector: Collector, run_id: str) -> tuple[AnalysisContext, DiagnosisBundle]:
    events: tuple[Event, ...] = collector.events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"no events recorded for run {run_id}")
    context = AnalysisContext.from_events(run_id, events)
    bundle = diagnose(context)
    for child in collector.runs():
        if child.run_id == run_id:
            continue
        evidence = evidence_from_stored_run(events, collector.events(child.run_id))
        if evidence is not None and evidence.parent_run_id == run_id:
            bundle = apply_replay_evidence(bundle, context.graph, evidence)
    return context, bundle


def create_app(store: Path | None = None) -> FastAPI:
    """Build the API over one store."""
    api = FastAPI(
        title="Runopsy",
        version=__version__,
        summary="Local diagnosis API. Binds to loopback; do not expose.",
    )
    store_dep = _store_dependency(store)

    @api.get("/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @api.post("/v1/events", response_model=IngestResult)
    def ingest(batch: EventBatch, collector: Collector = Depends(store_dep)) -> IngestResult:
        """Accept normalized events from an adapter that prefers HTTP to a subprocess."""
        from pydantic import TypeAdapter, ValidationError

        adapter: TypeAdapter[Event] = TypeAdapter(Event)
        recorded = duplicates = 0
        for raw in batch.events:
            try:
                event = adapter.validate_python(raw)
            except ValidationError as error:
                raise HTTPException(status_code=422, detail=error.errors()) from error
            if collector.record(event):
                recorded += 1
            else:
                duplicates += 1
        return IngestResult(recorded=recorded, duplicates=duplicates)

    @api.get("/v1/runs", response_model=list[RunSummaryOut])
    def list_runs(collector: Collector = Depends(store_dep)) -> list[RunSummaryOut]:
        return [
            RunSummaryOut(
                run_id=run.run_id,
                task=run.task,
                runtime=run.runtime,
                outcome=run.outcome.value,
                event_count=run.event_count,
                finished=run.is_finished,
            )
            for run in collector.runs()
        ]

    @api.get("/v1/runs/{run_id}", response_model=RunSummaryOut)
    def get_run(run_id: str, collector: Collector = Depends(store_dep)) -> RunSummaryOut:
        run = collector.store.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return RunSummaryOut(
            run_id=run.run_id,
            task=run.task,
            runtime=run.runtime,
            outcome=run.outcome.value,
            event_count=run.event_count,
            finished=run.is_finished,
        )

    @api.get("/v1/runs/{run_id}/graph", response_model=GraphOut)
    def get_graph(run_id: str, collector: Collector = Depends(store_dep)) -> GraphOut:
        """The trace graph.

        Observed edges and inferred ones are returned in separate fields. A viewer that
        merged them would show a guess and a measurement as the same kind of line.
        """
        context, bundle = _analyse(collector, run_id)
        graph: TraceGraph = context.graph
        inferred = infer_affects(graph, bundle.primary.onset_node_id) if bundle.primary else ()
        return GraphOut(
            run_id=run_id,
            nodes=[node.model_dump(mode="json") for node in graph.in_order()],
            edges=[edge.model_dump(mode="json") for edge in graph.edges],
            inferred_edges=[edge.model_dump(mode="json") for edge in inferred],
        )

    @api.post("/v1/runs/{run_id}/diagnose")
    def post_diagnose(run_id: str, collector: Collector = Depends(store_dep)) -> dict[str, Any]:
        """Deterministic diagnosis only.

        Hybrid mode spends the user's money, and a request arriving over a socket is a
        poor place to make that decision on their behalf.
        """
        _, bundle = _analyse(collector, run_id)
        return bundle.model_dump(mode="json")

    @api.post("/v1/runs/{run_id}/replay/plan")
    def post_replay_plan(
        run_id: str,
        from_step: int = Query(..., ge=0, description="Step to replay from."),
        collector: Collector = Depends(store_dep),
    ) -> dict[str, Any]:
        """Produce a plan. Execution is deliberately not exposed over HTTP.

        A replay is the only thing here that can change the world, and that decision
        belongs to a person reading the plan in a terminal, not to whatever can reach
        this port.
        """
        context, _ = _analyse(collector, run_id)
        if not any(node.sequence == from_step for node in context.graph.nodes):
            raise HTTPException(status_code=404, detail=f"run {run_id} has no step {from_step}")
        return build_plan(context, from_step).model_dump(mode="json")

    @api.get("/v1/runs/{run_id}/report", response_class=HTMLResponse)
    def get_report(run_id: str, collector: Collector = Depends(store_dep)) -> HTMLResponse:
        """The same self-contained report ``runopsy export`` writes, redacted by default."""
        context, bundle = _analyse(collector, run_id)
        summary = collector.store.run(run_id)
        return HTMLResponse(render_report(bundle, context.graph, summary, redact=True))

    @api.get("/", response_class=HTMLResponse)
    def index(collector: Collector = Depends(store_dep)) -> HTMLResponse:
        """A plain index of recorded runs, linking to each report."""
        runs = collector.runs()
        rows = "".join(
            f'<li><a href="/v1/runs/{run.run_id}/report">{run.run_id}</a>'
            f" — {run.task or 'no task recorded'}"
            f" <em>({run.outcome.value if run.is_finished else 'unfinished'})</em></li>"
            for run in runs
        )
        body = f"<ul>{rows}</ul>" if rows else "<p>No runs recorded yet.</p>"
        return HTMLResponse(
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<title>Runopsy</title><style>"
            "body{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;max-width:48rem;"
            "margin:3rem auto;padding:0 1rem}a{color:inherit}li{margin:.3rem 0}"
            "</style></head><body><h1>Runopsy</h1>"
            f"{body}<p style='color:#666;font-size:.9rem'>Local only. "
            "Replay execution is not available here — run it from the terminal, where "
            "the plan can be read first.</p></body></html>"
        )

    return api


def default_store() -> Path:
    return StorePaths.resolve().root
