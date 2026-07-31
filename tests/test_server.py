"""Local API tests.

The endpoints matter less than the boundaries: what this surface refuses to do, and
whether it applies the same redaction and the same observed-versus-inferred split as
every other surface. A second view with weaker rules is how the first view's rules stop
mattering.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import START, run_end, run_start
from runopsy_collector import Collector
from runopsy_core.schema import (
    Event,
    RunOutcome,
    SecurityMetadata,
    ToolCallEvent,
    ToolPayload,
)
from runopsy_server import create_app

RUN = "run_api"


def tool(
    sequence: int, *, name: str = "terminal", exit_code: int = 0, secret: bool = False
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"evt_{sequence}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name=name, exit_code=exit_code),
        security=SecurityMetadata(contains_secret=secret),
    )


def failing_run() -> list[Event]:
    return [
        run_start(RUN, task="configure and verify"),
        tool(1, name="write_config", exit_code=1),
        tool(2, name="edit_file"),
        tool(3, name="curl", secret=True),
        tool(4, name="pytest", exit_code=1),
        run_end(5, RUN, outcome=RunOutcome.FAILURE),
    ]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    with Collector.open(root) as collector:
        collector.record_all(failing_run())
    return root


@pytest.fixture
def client(store: Path) -> TestClient:
    return TestClient(create_app(store))


class TestReading:
    def test_health_reports_a_version(self, client: TestClient) -> None:
        body = client.get("/v1/health").json()

        assert body["status"] == "ok"
        assert body["version"]

    def test_runs_are_listed(self, client: TestClient) -> None:
        body = client.get("/v1/runs").json()

        assert [run["run_id"] for run in body] == [RUN]
        assert body[0]["outcome"] == "failure"

    def test_a_single_run_is_returned(self, client: TestClient) -> None:
        body = client.get(f"/v1/runs/{RUN}").json()

        assert body["task"] == "configure and verify"
        assert body["finished"] is True

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get("/v1/runs/nope").status_code == 404

    def test_diagnosis_matches_the_cli_shape(self, client: TestClient) -> None:
        body = client.post(f"/v1/runs/{RUN}/diagnose").json()

        assert body["run_id"] == RUN
        assert body["candidates"]
        assert all("confidence" in c for c in body["candidates"])

    def test_diagnosis_of_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.post("/v1/runs/nope/diagnose").status_code == 404


class TestObservedVersusInferred:
    def test_the_graph_keeps_them_in_separate_fields(self, client: TestClient) -> None:
        """Merged, a viewer would draw a guess and a measurement as the same line."""
        body = client.get(f"/v1/runs/{RUN}/graph").json()

        assert body["nodes"]
        assert "edges" in body
        assert "inferred_edges" in body

    def test_inferred_edges_carry_a_confidence_below_one(self, client: TestClient) -> None:
        body = client.get(f"/v1/runs/{RUN}/graph").json()

        for edge in body["inferred_edges"]:
            assert edge["kind"] == "affects"
            assert edge["confidence"] < 1.0

    def test_observed_edges_are_fully_confident(self, client: TestClient) -> None:
        body = client.get(f"/v1/runs/{RUN}/graph").json()

        for edge in body["edges"]:
            assert edge["confidence"] == 1.0


class TestWhatItRefusesToDo:
    def test_a_replay_plan_is_available(self, client: TestClient) -> None:
        response = client.post(f"/v1/runs/{RUN}/replay/plan", params={"from_step": 1})

        assert response.status_code == 200
        assert response.json()["parent_run_id"] == RUN

    def test_there_is_no_endpoint_that_executes_a_replay(self, store: Path) -> None:
        """The one thing that can change the world stays behind a typed command."""
        paths = {getattr(route, "path", "") for route in create_app(store).routes}

        assert "/v1/runs/{run_id}/replay" not in paths
        assert not any(p.endswith("/replay/execute") for p in paths)

    def test_a_plan_for_a_missing_step_is_a_404(self, client: TestClient) -> None:
        response = client.post(f"/v1/runs/{RUN}/replay/plan", params={"from_step": 999})

        assert response.status_code == 404

    def test_a_negative_step_is_rejected(self, client: TestClient) -> None:
        response = client.post(f"/v1/runs/{RUN}/replay/plan", params={"from_step": -1})

        assert response.status_code == 422

    def test_the_step_may_arrive_as_a_json_body(self, client: TestClient) -> None:
        """The obvious way to call a POST endpoint.

        It used to accept the step only as a query parameter, so a client sending JSON
        — which is what a POST invites — got a 422 naming a query field it had not used,
        and no indication that the body had been ignored.
        """
        response = client.post(f"/v1/runs/{RUN}/replay/plan", json={"from_step": 1})

        assert response.status_code == 200, response.text
        assert response.json()["parent_run_id"] == RUN

    def test_omitting_the_step_entirely_says_where_it_can_go(self, client: TestClient) -> None:
        response = client.post(f"/v1/runs/{RUN}/replay/plan")

        assert response.status_code == 422
        assert "body" in response.json()["detail"]


class TestDiagnosisLookup:
    """`GET /v1/diagnoses/{id}`, and the storage split it depends on.

    The design lists diagnoses as JSON on disk; nothing was writing them, so the
    directory existed and stayed empty and the endpoint had nothing to serve.
    """

    def test_diagnosing_stores_the_bundle(self, client: TestClient, store: Path) -> None:
        created = client.post(f"/v1/runs/{RUN}/diagnose").json()

        fetched = client.get(f"/v1/diagnoses/{created['diagnosis_id']}")

        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["run_id"] == RUN

    def test_an_unknown_id_is_a_404_not_a_recomputation(self, client: TestClient) -> None:
        """Recomputing on demand would answer for ids that were never issued."""
        response = client.get("/v1/diagnoses/diag:made-up:0000")

        assert response.status_code == 404

    def test_a_traversal_attempt_cannot_escape_the_directory(self, client: TestClient) -> None:
        """The id is built from a run id, which comes from a runtime we do not control."""
        response = client.get("/v1/diagnoses/..%2F..%2Fetc%2Fpasswd")

        assert response.status_code in {404, 400}


class TestExportOverHttp:
    def test_html_is_the_default(self, client: TestClient) -> None:
        response = client.post("/v1/export", json={"run_id": RUN})

        assert response.status_code == 200, response.text
        assert response.text.startswith("<!doctype html>")

    def test_otlp_is_available_and_valid_json(self, client: TestClient) -> None:
        response = client.post("/v1/export", json={"run_id": RUN, "format": "otlp"})

        assert response.status_code == 200, response.text
        assert response.json()["resourceSpans"]

    def test_it_is_redacted_unless_asked_otherwise(self, client: TestClient) -> None:
        """An export is a sharing surface whichever transport carries it."""
        default = client.post("/v1/export", json={"run_id": RUN, "format": "otlp"})
        revealed = client.post(
            "/v1/export",
            json={"run_id": RUN, "format": "otlp", "include_sensitive": True},
        )

        assert default.text != revealed.text or "[redacted]" not in revealed.text

    def test_an_unknown_format_is_rejected(self, client: TestClient) -> None:
        response = client.post("/v1/export", json={"run_id": RUN, "format": "pdf"})

        assert response.status_code == 422


class TestTheEventStream:
    def test_it_replays_what_is_already_recorded(self, client: TestClient) -> None:
        with client.stream("GET", f"/v1/runs/{RUN}/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())

        assert body.count("data:") >= 2

    def test_it_ends_when_the_run_does(self, client: TestClient) -> None:
        """A finished run must close the stream rather than hold the connection open."""
        with client.stream("GET", f"/v1/runs/{RUN}/stream") as response:
            body = "".join(response.iter_text())

        assert "event: end" in body


class TestIngest:
    def test_events_can_be_recorded_over_http(self, client: TestClient, store: Path) -> None:
        event = tool(9, name="extra").model_dump(mode="json")

        body = client.post("/v1/events", json={"events": [event]}).json()

        assert body["recorded"] == 1
        with Collector.open(store) as collector:
            assert any(e.sequence == 9 for e in collector.events(RUN))

    def test_a_repeated_event_is_counted_as_a_duplicate(self, client: TestClient) -> None:
        event = tool(1).model_dump(mode="json")

        body = client.post("/v1/events", json={"events": [event]}).json()

        assert body["duplicates"] == 1
        assert body["recorded"] == 0

    def test_a_malformed_event_is_rejected_with_detail(self, client: TestClient) -> None:
        response = client.post("/v1/events", json={"events": [{"kind": "nonsense"}]})

        assert response.status_code == 422


class TestReportSurface:
    def test_the_report_is_served_and_self_contained(self, client: TestClient) -> None:
        body = client.get(f"/v1/runs/{RUN}/report").text

        assert body.startswith("<!doctype html>")
        assert "<script" not in body.lower()

    def test_the_served_report_is_redacted(self, client: TestClient) -> None:
        """The same rule as export. A weaker second surface undoes the first."""
        body = client.get(f"/v1/runs/{RUN}/report").text

        assert "[redacted]" in body

    def test_the_index_lists_runs_and_states_the_limits(self, client: TestClient) -> None:
        body = client.get("/").text

        assert RUN in body
        assert "Replay execution is not available here" in body

    def test_an_empty_store_still_renders_an_index(self, tmp_path: Path) -> None:
        empty = TestClient(create_app(tmp_path / "empty"))

        assert "No runs recorded yet" in empty.get("/").text
