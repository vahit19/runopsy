"""Replay execution tests: the counterfactual loop, end to end, with real subprocesses.

The scenario is the product's whole thesis in miniature. A step writes a wrong config
value; a later check fails because of it. The original run makes the early step a
suspect. Substituting a corrected command at that step and re-running downstream makes
the failure disappear — and only that observation is allowed to produce
``replay_supported``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_adapter import record_steps
from runopsy_cli.main import app
from runopsy_collector import Collector, PayloadVault
from runopsy_core import AnalysisContext, apply_replay_evidence, diagnose
from runopsy_core.schema import DiagnosisStatus
from runopsy_replay import build_plan, evidence_from_stored_run, execute_plan

runner = CliRunner()
PY = sys.executable
RUN = "run_orig"


def write_cfg(value: str) -> str:
    return f"{PY} -c \"open('cfg.txt','w').write('{value}')\""


def check_cfg() -> str:
    return f"{PY} -c \"import sys; sys.exit(0 if open('cfg.txt').read()=='good' else 1)\""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny project directory the sandbox will copy."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


@pytest.fixture
def store(project: Path, tmp_path: Path) -> Path:
    """The original faulty run: writes 'bad' at step 1, the check fails at step 2."""
    root = tmp_path / "store"
    with Collector.open(root) as collector:
        record_steps(
            [write_cfg("bad"), check_cfg()],
            run_id=RUN,
            task="configure and verify",
            sink=collector,
            vault=collector.vault,
            cwd=project,
        )
    return root


class TestVault:
    def test_payload_text_round_trips(self, tmp_path: Path) -> None:
        vault = PayloadVault(tmp_path / "vault")

        digest = vault.put("pytest -x tests/")

        entry = vault.get(digest)
        assert entry is not None
        assert entry.text == "pytest -x tests/"
        assert entry.executable

    def test_a_secret_is_stored_redacted_under_the_original_digest(self, tmp_path: Path) -> None:
        """The trace's hash must still find the entry, but the raw secret exists nowhere."""
        vault = PayloadVault(tmp_path / "vault")
        secret_command = "curl -H 'Authorization: Bearer abcdefghijklmnopqrst'"

        digest = vault.put(secret_command, stored_text="curl -H '[REDACTED]'")

        entry = vault.get(digest)
        assert entry is not None
        assert "abcdefghijklmnopqrst" not in entry.text
        assert not entry.executable

    def test_a_missing_digest_returns_none(self, tmp_path: Path) -> None:
        vault = PayloadVault(tmp_path / "vault")

        assert vault.get("sha256:" + "0" * 64) is None

    def test_recording_through_the_cli_fills_the_vault(self, store: Path) -> None:
        with Collector.open(store) as collector:
            events = collector.events(RUN)
            hashes = [
                e.tool.arguments_hash
                for e in events
                if hasattr(e, "tool") and e.tool.arguments_hash
            ]
            assert hashes
            for digest in hashes:
                assert collector.vault.get(digest) is not None


class TestExecution:
    def _run_experiment(
        self, store: Path, project: Path, **kwargs: object
    ) -> tuple[object, object]:
        with Collector.open(store) as collector:
            events = collector.events(RUN)
            context = AnalysisContext.from_events(RUN, events)
            plan = build_plan(context, 1)
            verdict = execute_plan(
                plan,
                context,
                collector.vault,
                collector,
                replay_run_id=f"{RUN}_replay1",
                cwd=project,
                approve_unknown=True,
                **kwargs,  # type: ignore[arg-type]
            )
        return context, verdict

    def test_a_straight_rerun_reproduces_but_does_not_support(
        self, store: Path, project: Path
    ) -> None:
        """Reproduction is consistency, not causation."""
        _, verdict = self._run_experiment(store, project)

        assert verdict.reproduced is True  # type: ignore[attr-defined]
        assert verdict.supports_onset is False  # type: ignore[attr-defined]

    def test_substituting_the_onset_clears_the_downstream_failure(
        self, store: Path, project: Path
    ) -> None:
        """The counterfactual: fix step 1, and step 2 passes."""
        _, verdict = self._run_experiment(store, project, substitute=write_cfg("good"))

        assert verdict.outcome_changed is True  # type: ignore[attr-defined]
        assert verdict.supports_onset is True  # type: ignore[attr-defined]

    def test_the_working_tree_is_never_touched(self, store: Path, project: Path) -> None:
        """The original run's files are evidence; the experiment runs on a copy."""
        (project / "cfg.txt").write_text("bad", encoding="utf-8")

        self._run_experiment(store, project, substitute=write_cfg("good"))

        assert (project / "cfg.txt").read_text(encoding="utf-8") == "bad"

    def test_two_interventions_at_once_are_refused(self, store: Path, project: Path) -> None:
        with pytest.raises(ValueError, match="not both"):
            self._run_experiment(store, project, substitute=write_cfg("good"), skip_onset=True)

    def test_the_replay_run_is_recorded_with_its_lineage(self, store: Path, project: Path) -> None:
        self._run_experiment(store, project, substitute=write_cfg("good"))

        with Collector.open(store) as collector:
            child = collector.events(f"{RUN}_replay1")

        start = child[0]
        assert start.run.parent_run_id == RUN  # type: ignore[union-attr]
        assert start.run.intervention_kind == "substitute"  # type: ignore[union-attr]
        assert start.run.intervention_target == 1  # type: ignore[union-attr]


class TestEvidenceFolding:
    def test_supporting_evidence_upgrades_exactly_one_candidate(
        self, store: Path, project: Path
    ) -> None:
        with Collector.open(store) as collector:
            events = collector.events(RUN)
            context = AnalysisContext.from_events(RUN, events)
            plan = build_plan(context, 1)
            execute_plan(
                plan,
                context,
                collector.vault,
                collector,
                replay_run_id=f"{RUN}_replay1",
                cwd=project,
                substitute=write_cfg("good"),
                approve_unknown=True,
            )
            child_events = collector.events(f"{RUN}_replay1")

        bundle = diagnose(context)
        evidence = evidence_from_stored_run(events, child_events)
        assert evidence is not None
        upgraded = apply_replay_evidence(bundle, context.graph, evidence)

        supported = [c for c in upgraded.candidates if c.status is DiagnosisStatus.REPLAY_SUPPORTED]
        assert len(supported) == 1
        assert supported[0].replay_run_id == f"{RUN}_replay1"
        assert supported[0].is_definitive is True

    def test_a_plain_rerun_upgrades_nothing(self, store: Path, project: Path) -> None:
        with Collector.open(store) as collector:
            events = collector.events(RUN)
            context = AnalysisContext.from_events(RUN, events)
            plan = build_plan(context, 1)
            execute_plan(
                plan,
                context,
                collector.vault,
                collector,
                replay_run_id=f"{RUN}_replay1",
                cwd=project,
                approve_unknown=True,
            )
            child_events = collector.events(f"{RUN}_replay1")

        bundle = diagnose(context)
        evidence = evidence_from_stored_run(events, child_events)
        assert evidence is not None
        upgraded = apply_replay_evidence(bundle, context.graph, evidence)

        assert upgraded == bundle

    def test_evidence_for_another_run_is_ignored(self, store: Path, project: Path) -> None:
        with Collector.open(store) as collector:
            events = collector.events(RUN)
        context = AnalysisContext.from_events(RUN, events)
        bundle = diagnose(context)

        from runopsy_core import ReplayEvidence

        foreign = ReplayEvidence(
            replay_run_id="x",
            parent_run_id="someone_else",
            intervention_target=1,
            outcome_changed=True,
            intervened=True,
        )

        assert apply_replay_evidence(bundle, context.graph, foreign) == bundle


class TestFullLoopThroughTheCli:
    def test_suspicion_becomes_a_supported_cause(self, store: Path, project: Path) -> None:
        """The complete product thesis, through the real commands a user types.

        Step 1 *succeeded* while writing the wrong value, so before the experiment the
        engine cannot even name it — this is exactly the blind spot the benchmark
        labels. The counterfactual replay is what reaches it.
        """
        before = runner.invoke(app, ["diagnose", RUN, "--store", str(store)])
        assert "Observed failure" in before.output
        assert "Cause, supported by replay" not in before.output

        experiment = runner.invoke(
            app,
            [
                "replay",
                RUN,
                "--from-step",
                "1",
                "--store",
                str(store),
                "--execute",
                "--yes",
                "--substitute",
                write_cfg("good"),
            ],
        )
        assert experiment.exit_code == 0, experiment.output
        assert "Cause, supported by replay" in experiment.output

        after = runner.invoke(app, ["diagnose", RUN, "--store", str(store)])
        assert "Cause, supported by replay" in after.output
        assert "validated" in after.output

    def test_execution_asks_before_running_anything(self, store: Path, project: Path) -> None:
        """Design 16.1: replay requires confirmation. Declining must run nothing."""
        result = runner.invoke(
            app,
            ["replay", RUN, "--from-step", "1", "--store", str(store), "--execute"],
            input="n\n",
        )

        assert result.exit_code == 1
        with Collector.open(store) as collector:
            assert collector.store.run(f"{RUN}_replay1") is None

    def test_two_interventions_are_refused_at_the_cli(self, store: Path) -> None:
        result = runner.invoke(
            app,
            [
                "replay",
                RUN,
                "--from-step",
                "1",
                "--store",
                str(store),
                "--execute",
                "--yes",
                "--skip-onset",
                "--substitute",
                "echo x",
            ],
        )

        assert result.exit_code == 2
        assert "one intervention" in result.output

    def test_a_store_inside_the_project_does_not_break_the_sandbox(
        self, project: Path
    ) -> None:
        """Regression: the copy must exclude the store, whatever it is named.

        With the store inside the working directory, the sandbox copy used to include
        it — and on Windows the open DuckDB file is locked, which failed the whole
        experiment. Found by running the live demo, not by the original tests, which
        all kept the store outside the project.
        """
        inner_store = project / ".rp"
        with Collector.open(inner_store) as collector:
            record_steps(
                [write_cfg("bad"), check_cfg()],
                run_id=RUN,
                task="configure and verify",
                sink=collector,
                vault=collector.vault,
                cwd=project,
            )

        result = runner.invoke(
            app,
            [
                "replay",
                RUN,
                "--from-step",
                "1",
                "--store",
                str(inner_store),
                "--execute",
                "--yes",
                "--substitute",
                write_cfg("good"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Cause, supported by replay" in result.output

    def test_replay_runs_get_distinct_ids(self, store: Path, project: Path) -> None:
        for _ in range(2):
            runner.invoke(
                app,
                [
                    "replay",
                    RUN,
                    "--from-step",
                    "1",
                    "--store",
                    str(store),
                    "--execute",
                    "--yes",
                ],
            )

        with Collector.open(store) as collector:
            assert collector.store.run(f"{RUN}_replay1") is not None
            assert collector.store.run(f"{RUN}_replay2") is not None
