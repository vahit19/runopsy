"""Configuration tests.

The property under test is trustworthiness: every key in the file does something, and a
key the tool does not know is reported rather than silently ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_cli.config import example_config, load_config
from runopsy_cli.main import app

runner = CliRunner()


class TestLoading:
    def test_no_file_means_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "missing.toml")

        assert config.detector_settings.retry_threshold == 3
        assert config.vault_enabled is True
        assert config.warnings == ()

    def test_values_reach_the_detector_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "runopsy.toml"
        path.write_text(
            "[analysis]\nretry_threshold = 5\ncost_budget_usd = 0.25\n", encoding="utf-8"
        )

        config = load_config(path)

        assert config.detector_settings.retry_threshold == 5
        assert config.detector_settings.cost_budget_usd == 0.25

    def test_a_typo_is_reported_not_silently_ignored(self, tmp_path: Path) -> None:
        """A skipped typo leaves the user believing in a threshold they never set."""
        path = tmp_path / "runopsy.toml"
        path.write_text("[analysis]\nloop_treshold = 9\n", encoding="utf-8")

        config = load_config(path)

        assert any("loop_treshold" in warning for warning in config.warnings)
        assert config.detector_settings.loop_threshold == 3

    def test_an_unknown_section_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "runopsy.toml"
        path.write_text("[semantics]\nmodel = 'x'\n", encoding="utf-8")

        config = load_config(path)

        assert any("[semantics]" in warning for warning in config.warnings)

    def test_a_broken_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        """Refusing to diagnose over a config typo punishes the user when they need us."""
        path = tmp_path / "runopsy.toml"
        path.write_text("[analysis\nnot toml", encoding="utf-8")

        config = load_config(path)

        assert config.detector_settings.retry_threshold == 3
        assert any("could not read" in warning for warning in config.warnings)

    def test_the_example_file_parses_and_produces_no_warnings(self, tmp_path: Path) -> None:
        """The file we tell users to start from must itself be clean."""
        path = tmp_path / "runopsy.toml"
        path.write_text(example_config(), encoding="utf-8")

        config = load_config(path)

        assert config.warnings == ()

    def test_vault_can_be_disabled(self, tmp_path: Path) -> None:
        path = tmp_path / "runopsy.toml"
        path.write_text("[privacy]\nvault = false\n", encoding="utf-8")

        assert load_config(path).vault_enabled is False


class TestConfigCommand:
    def test_init_writes_the_example(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["config", "--init"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "runopsy.toml").exists()

    def test_init_refuses_to_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "runopsy.toml").write_text("[analysis]\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "--init"])

        assert result.exit_code == 2
        assert "not overwriting" in result.output

    def test_show_reports_effective_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "runopsy.toml").write_text(
            "[analysis]\nretry_threshold = 7\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["config"])

        assert result.exit_code == 0, result.output
        assert "7" in result.output
