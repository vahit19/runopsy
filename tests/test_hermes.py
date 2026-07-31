"""Hermes adapter tests.

Payloads are taken from hermes-agent 0.19.0's own documentation and test fixtures
(`agent/shell_hooks.py`, `hermes_cli/hooks.py`), so these are a contract check against
the real runtime rather than against an imagined one.

The most important test is the last group: the hook must never fail the run it is
observing, whatever it is handed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from runopsy_adapter import assert_adapter_contract
from runopsy_adapter.hermes import RECORDED_EVENTS, hooks_config_block, map_payload, run_id_for
from runopsy_cli.main import app
from runopsy_collector import Collector
from runopsy_core import AnalysisContext, diagnose
from runopsy_core.hashing import hash_text
from runopsy_core.schema import (
    CallStatus,
    HandoffEvent,
    LlmCallEvent,
    RunEndEvent,
    RunOutcome,
    RunStartEvent,
    ToolCallEvent,
)

runner = CliRunner()
NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
SESSION = "sess_abc123"


def payload(event: str, **overrides: Any) -> dict[str, Any]:
    """A payload shaped as Hermes documents it in agent/shell_hooks.py."""
    base: dict[str, Any] = {
        "hook_event_name": event,
        "session_id": SESSION,
        "cwd": "/home/user/project",
        "extra": {},
    }
    base.update(overrides)
    return base


def mapped(event: str, *, sequence: int = 0, **overrides: Any) -> Any:
    return map_payload(payload(event, **overrides), sequence=sequence, timestamp=NOW)


class TestSessionLifecycle:
    def test_a_session_start_opens_a_run(self) -> None:
        event = mapped(
            "on_session_start", extra={"model": "claude-sonnet-4-20250514", "platform": "cli"}
        )

        assert isinstance(event, RunStartEvent)
        assert event.run.runtime == "hermes"
        assert event.run.model == "claude-sonnet-4-20250514"
        assert event.run.provider == "cli"

    def test_the_session_id_becomes_the_run_id(self) -> None:
        assert run_id_for(payload("on_session_start")) == SESSION

    def test_a_completed_turn_ends_as_success(self) -> None:
        event = mapped("on_session_end", extra={"completed": True, "interrupted": False})

        assert isinstance(event, RunEndEvent)
        assert event.run.outcome is RunOutcome.SUCCESS

    def test_an_interrupted_turn_is_cancelled_not_failed(self) -> None:
        """A user pressing ctrl-c is not the agent getting the task wrong."""
        event = mapped("on_session_end", extra={"completed": False, "interrupted": True})

        assert isinstance(event, RunEndEvent)
        assert event.run.outcome is RunOutcome.CANCELLED

    def test_an_inconclusive_turn_is_unknown_rather_than_guessed(self) -> None:
        event = mapped("on_session_end", extra={})

        assert isinstance(event, RunEndEvent)
        assert event.run.outcome is RunOutcome.UNKNOWN


class TestToolCalls:
    def test_a_successful_call_is_recorded(self) -> None:
        event = mapped(
            "post_tool_call",
            tool_name="terminal",
            tool_input={"command": "echo hello"},
            extra={"status": "ok", "duration_ms": 42, "turn_id": "turn_7"},
        )

        assert isinstance(event, ToolCallEvent)
        assert event.tool.name == "terminal"
        assert event.tool.status is CallStatus.OK
        assert event.tool.duration_ms == 42
        assert event.parent_id == "turn_7"

    def test_hermes_status_maps_onto_the_schema(self) -> None:
        for hermes_status, expected in (
            ("ok", CallStatus.OK),
            ("error", CallStatus.ERROR),
            ("blocked", CallStatus.BLOCKED),
        ):
            event = mapped("post_tool_call", tool_name="t", extra={"status": hermes_status})
            assert isinstance(event, ToolCallEvent)
            assert event.tool.status is expected

    def test_an_error_gets_a_nonzero_exit_code_so_detectors_fire(self) -> None:
        event = mapped(
            "post_tool_call",
            tool_name="terminal",
            extra={"status": "error", "error_type": "ValueError"},
        )

        assert isinstance(event, ToolCallEvent)
        assert event.tool.exit_code == 1
        assert event.tool.error_type == "ValueError"

    def test_command_text_is_hashed_not_stored(self) -> None:
        """Tool input is arbitrary user content and must not enter the trace."""
        event = mapped(
            "post_tool_call",
            tool_name="terminal",
            tool_input={"command": "deploy --token ghp_" + "b" * 28},
            extra={"status": "ok"},
        )

        assert isinstance(event, ToolCallEvent)
        assert "ghp_" not in json.dumps(event.model_dump(mode="json"))

    def test_a_credential_in_the_command_sets_the_flag(self) -> None:
        event = mapped(
            "post_tool_call",
            tool_name="terminal",
            tool_input={"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrst'"},
            extra={"status": "ok"},
        )

        assert event is not None
        assert event.security.contains_secret is True


class TestModelAndSubagents:
    def test_a_model_call_is_recorded(self) -> None:
        event = mapped(
            "post_llm_call", extra={"model": "gpt-4", "platform": "cli", "duration_ms": 900}
        )

        assert isinstance(event, LlmCallEvent)
        assert event.llm.model == "gpt-4"
        assert event.llm.latency_ms == 900

    def test_a_subagent_stop_becomes_a_handoff(self) -> None:
        event = mapped(
            "subagent_stop",
            extra={
                "child_session_id": "sess_child",
                "child_role": "tester",
                "child_summary": "ran the suite",
                "child_status": "success",
            },
        )

        assert isinstance(event, HandoffEvent)
        assert event.handoff.to_agent_id == "sess_child"
        assert event.handoff.missing_fields == ()

    def test_a_subagent_returning_nothing_is_an_incomplete_handoff(self) -> None:
        """A child that reports no summary is exactly the multi-agent gap to surface."""
        event = mapped("subagent_stop", extra={"child_session_id": "sess_child"})

        assert isinstance(event, HandoffEvent)
        assert event.handoff.missing_fields == ("child_summary",)


class TestSafety:
    def test_unrecognised_events_are_ignored_not_invented(self) -> None:
        assert mapped("pre_tool_call") is None
        assert mapped("pre_api_request") is None
        assert mapped("something_new_in_a_later_version") is None

    def test_only_observational_hooks_are_registered(self) -> None:
        """pre_tool_call can block in Hermes; Runopsy deliberately does not use it."""
        assert not any(name.startswith("pre_") for name in RECORDED_EVENTS)

    def test_a_hostile_session_id_cannot_escape_the_store(self) -> None:
        """Session ids come from a third party and are used to build paths."""
        assert "/" not in run_id_for(payload("on_session_start", session_id="../../.ssh/x"))

    def test_a_missing_session_id_still_yields_a_usable_run(self) -> None:
        assert run_id_for({"hook_event_name": "on_session_start"}) == "hermes_session"


class TestGeneratedConfig:
    def test_every_recorded_event_is_configured(self) -> None:
        block = hooks_config_block("runopsy hook")

        for event in RECORDED_EVENTS:
            assert f"  {event}:" in block

    def test_it_names_the_command_hermes_should_run(self) -> None:
        assert "command: 'runopsy hook post_tool_call'" in hooks_config_block("runopsy hook")

    def test_the_whole_invocation_is_one_quoted_scalar(self) -> None:
        """Quoting only the path is invalid YAML, and Hermes fails silently on it.

        ``command: "C:/x y/runopsy" hook post_tool_call`` parses as a quoted scalar
        followed by junk. Hermes discards the entire config, runs with defaults, and
        records nothing — a session that looks completely normal and produces no trace.
        """
        import yaml

        block = hooks_config_block("C:/Program Files/runopsy.exe hook")
        parsed = yaml.safe_load(block)

        command = parsed["hooks"]["post_tool_call"][0]["command"]
        assert command == "C:/Program Files/runopsy.exe hook post_tool_call"

    def test_a_command_containing_a_quote_survives_the_round_trip(self) -> None:
        import yaml

        block = hooks_config_block("/opt/o'brien/runopsy hook")

        assert yaml.safe_load(block)["hooks"]["on_session_end"][0]["command"] == (
            "/opt/o'brien/runopsy hook on_session_end"
        )

    def test_it_bounds_the_hook_timeout(self) -> None:
        """An unbounded hook would let a stalled recorder hang the agent."""
        assert "timeout: 10" in hooks_config_block("runopsy hook")


class TestHookCommandNeverBreaksTheRun:
    """The rule that outranks every other behaviour of this command."""

    def _invoke(self, event: str, body: str, store: Path) -> Any:
        return runner.invoke(app, ["hook", event, "--store", str(store)], input=body)

    def test_valid_input_is_recorded(self, tmp_path: Path) -> None:
        store = tmp_path / "store"

        result = self._invoke("on_session_start", json.dumps(payload("on_session_start")), store)

        assert result.exit_code == 0
        with Collector.open(store) as collector:
            assert len(collector.events(SESSION)) == 1

    def test_malformed_json_exits_zero(self, tmp_path: Path) -> None:
        result = self._invoke("post_tool_call", "{not json", tmp_path / "store")

        assert result.exit_code == 0

    def test_empty_input_exits_zero(self, tmp_path: Path) -> None:
        result = self._invoke("post_tool_call", "", tmp_path / "store")

        assert result.exit_code == 0

    def test_an_unwritable_store_exits_zero(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        result = self._invoke("on_session_start", json.dumps(payload("on_session_start")), blocker)

        assert result.exit_code == 0

    def test_it_always_prints_a_decision_hermes_can_parse(self, tmp_path: Path) -> None:
        """Hermes JSON-parses what the hook writes, so the decision must survive a failure.

        The assertion looks for the decision anywhere in the output because the test
        runner merges stderr into stdout. Hermes captures the two streams separately, so
        in practice its parser only ever sees this line.
        """
        result = self._invoke("post_tool_call", "{broken", tmp_path / "store")

        decisions = [line for line in result.output.splitlines() if line.strip() == "{}"]
        assert decisions, result.output
        assert json.loads(decisions[0]) == {}

    def test_a_failure_is_reported_rather_than_hidden(self, tmp_path: Path) -> None:
        """Silent failure leaves a user with no trace and no reason why."""
        result = self._invoke("post_tool_call", "{broken", tmp_path / "store")

        assert "could not record post_tool_call" in result.output

    def test_it_never_blocks_a_tool_call(self, tmp_path: Path) -> None:
        result = self._invoke(
            "post_tool_call",
            json.dumps(payload("post_tool_call", tool_name="rm", extra={"status": "ok"})),
            tmp_path / "store",
        )

        assert "block" not in result.output


class TestEndToEndSession:
    def test_a_recorded_hermes_session_is_contract_valid_and_diagnosable(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "store"
        session = [
            payload("on_session_start", extra={"model": "gpt-4", "platform": "cli"}),
            payload(
                "post_tool_call",
                tool_name="read_file",
                tool_input={"path": "config.yaml"},
                extra={"status": "ok", "duration_ms": 12},
            ),
            payload(
                "post_tool_call",
                tool_name="write_config",
                tool_input={"path": "config.yaml"},
                extra={"status": "error", "error_type": "PermissionError", "duration_ms": 30},
            ),
            payload("post_llm_call", extra={"model": "gpt-4", "duration_ms": 800}),
            payload(
                "post_tool_call",
                tool_name="pytest",
                tool_input={"command": "pytest -x"},
                extra={"status": "error", "error_type": "AssertionError", "duration_ms": 4000},
            ),
            payload("on_session_end", extra={"completed": False, "interrupted": False}),
        ]

        for entry in session:
            runner.invoke(
                app,
                ["hook", str(entry["hook_event_name"]), "--store", str(store)],
                input=json.dumps(entry),
            )

        with Collector.open(store) as collector:
            events = collector.events(SESSION)
            assert collector.integrity(SESSION).is_intact

        assert_adapter_contract(events)

        bundle = diagnose(AnalysisContext.from_events(SESSION, events))
        assert bundle.primary is not None
        # The write failed first; the test failure at the end is only where it showed.
        assert bundle.primary.onset_node_id == f"{SESSION}_evt_0002"


@pytest.mark.parametrize("event", RECORDED_EVENTS)
def test_every_registered_event_maps_to_something(event: str) -> None:
    """A hook we ask Hermes to call must produce an event, or we are wasting its time."""
    assert mapped(event, extra={"child_session_id": "c", "model": "m"}) is not None


class TestPayloadsReachTheVault:
    """The trace stores hashes; the text has to be kept somewhere or it is lost.

    It was lost. The Hermes mapper hashed the command and the output and dropped both,
    so every layer that needs to *read* a step degraded silently: `--mode hybrid` on a
    real 42-event session withheld all twenty steps as "not in the local store" and
    charged for the model call anyway.
    """

    class Vault:
        def __init__(self) -> None:
            self.stored: dict[str, str] = {}

        def put(self, original_text: str, *, stored_text: str | None = None) -> str:
            digest = hash_text(original_text)
            self.stored[digest] = original_text if stored_text is None else stored_text
            return digest

    def call(self, vault: Vault | None, *, result: str = "3 passed") -> Any:
        return map_payload(
            payload(
                "post_tool_call",
                tool_name="terminal",
                args="pytest -q",
                extra={"result": result, "status": "ok", "duration_ms": 12},
            ),
            sequence=1,
            timestamp=NOW,
            vault=vault,
        )

    def test_the_command_and_output_are_preserved(self) -> None:
        vault = self.Vault()

        event = self.call(vault)

        assert vault.stored[str(event.tool.arguments_hash)] == "pytest -q"
        assert vault.stored[str(event.tool.output_hash)] == "3 passed"

    def test_the_hash_in_the_trace_is_of_the_original_text(self) -> None:
        """A digest that does not match what was hashed cannot be looked up."""
        vault = self.Vault()

        event = self.call(vault)

        assert event.tool.arguments_hash == hash_text("pytest -q")

    def test_a_secret_is_redacted_before_it_lands_on_disk(self) -> None:
        """The vault is local, but a secret written anywhere outlives the scan."""
        vault = self.Vault()
        secret = "export TOKEN=" + "ghp_" + "b" * 36

        event = self.call(vault, result=secret)

        assert "ghp_" not in vault.stored[str(event.tool.output_hash)]

    def test_without_a_vault_nothing_is_stored_and_nothing_breaks(self) -> None:
        """Recording must still work when the vault is switched off in config."""
        event = self.call(None)

        assert event.tool.arguments_hash == hash_text("pytest -q")
