"""Credential resolution tests.

The property that matters is that a key is found in a predictable order and that no code
path ever renders it. ``doctor`` reporting a source is useful; ``doctor`` reporting a
value would be a vulnerability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from runopsy_cli.main import app
from runopsy_semantic import credentials

runner = CliRunner()
SECRET = "PLACEHOLDER-not-a-real-credential-0123456789"
VARIABLE = credentials.API_KEY_VARIABLE


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test states its own sources; the developer's real .env must not leak in."""
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.setattr(credentials, "read_keyring", lambda *a, **k: None)


class TestResolutionOrder:
    def test_nothing_configured_resolves_to_none(self, tmp_path: Path) -> None:
        """A normal state, not an error: everything deterministic works without a key."""
        assert credentials.resolve(cwd=tmp_path) is None

    def test_an_explicit_key_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE, "from-env")

        found = credentials.resolve("explicit", cwd=tmp_path)

        assert found is not None
        assert found.key == "explicit"
        assert found.source == "command line"

    def test_the_environment_beats_the_keyring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, "from-env")
        monkeypatch.setattr(credentials, "read_keyring", lambda *a, **k: "from-keyring")

        found = credentials.resolve(cwd=tmp_path)

        assert found is not None
        assert found.source == "environment"

    def test_the_keyring_beats_a_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(f"{VARIABLE}=from-file", encoding="utf-8")
        monkeypatch.setattr(credentials, "read_keyring", lambda *a, **k: "from-keyring")

        found = credentials.resolve(cwd=tmp_path)

        assert found is not None
        assert found.source == "OS keyring"

    def test_a_dotenv_is_the_last_resort(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f'{VARIABLE}="from-file"\n', encoding="utf-8")

        found = credentials.resolve(cwd=tmp_path)

        assert found is not None
        assert found.key == "from-file"
        assert found.source == ".env file"

    def test_an_exported_dotenv_line_is_read(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"export {VARIABLE}=x\n", encoding="utf-8")

        found = credentials.resolve(cwd=tmp_path)

        assert found is not None
        assert found.key == "x"

    def test_comments_and_blanks_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"# {VARIABLE}=commented\n\nOTHER=1\n", encoding="utf-8")

        assert credentials.resolve(cwd=tmp_path) is None

    def test_an_empty_value_counts_as_absent(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"{VARIABLE}=\n", encoding="utf-8")

        assert credentials.resolve(cwd=tmp_path) is None


class TestKeyringDegradation:
    def test_an_unavailable_backend_reads_as_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Headless containers have no Secret Service; that must not fail a diagnosis."""

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("no backend")

        monkeypatch.setattr(credentials, "_keyring", explode)

        assert credentials.read_keyring() is None

    def test_a_failed_delete_reports_false_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("no backend")

        monkeypatch.setattr(credentials, "_keyring", explode)

        assert credentials.delete_keyring() is False


class TestNeverRendersTheValue:
    def test_the_description_names_a_source_not_a_key(self) -> None:
        described = credentials.describe_source(credentials.ResolvedKey(SECRET, "environment"))

        assert SECRET not in described
        assert "environment" in described

    def test_a_dotenv_source_nudges_toward_setup(self) -> None:
        described = credentials.describe_source(credentials.ResolvedKey(SECRET, ".env file"))

        assert "runopsy setup" in described

    def test_doctor_shows_the_source_and_not_the_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, SECRET)

        result = runner.invoke(app, ["doctor", "--store", str(tmp_path / "s")])

        assert result.exit_code == 0, result.output
        assert SECRET not in result.output
        assert "environment" in result.output


class TestSetupCommand:
    def test_it_stores_what_was_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stored: dict[str, str] = {}
        monkeypatch.setattr("runopsy_cli.main.write_keyring", lambda key: stored.update(k=key))

        result = runner.invoke(app, ["setup"], input=f"{SECRET}\n")

        assert result.exit_code == 0, result.output
        assert stored["k"] == SECRET

    def test_the_typed_key_is_not_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hide_input keeps it off the screen, and therefore out of scrollback."""
        monkeypatch.setattr("runopsy_cli.main.write_keyring", lambda key: None)

        result = runner.invoke(app, ["setup"], input=f"{SECRET}\n")

        assert SECRET not in result.output

    def test_an_empty_entry_stores_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The prompt re-asks on an empty line and aborts; either way nothing is written."""
        calls: list[str] = []
        monkeypatch.setattr("runopsy_cli.main.write_keyring", lambda key: calls.append(key))

        result = runner.invoke(app, ["setup"], input="\n")

        assert result.exit_code != 0
        assert calls == []

    def test_whitespace_only_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr("runopsy_cli.main.write_keyring", lambda key: calls.append(key))

        result = runner.invoke(app, ["setup"], input="   \n")

        assert result.exit_code == 2
        assert calls == []

    def test_an_unavailable_keyring_points_at_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(key: str) -> None:
            raise credentials.KeyringUnavailableError("no backend")

        monkeypatch.setattr("runopsy_cli.main.write_keyring", explode)

        result = runner.invoke(app, ["setup"], input=f"{SECRET}\n")

        assert result.exit_code == 1
        assert VARIABLE in result.output

    def test_remove_reports_when_there_was_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("runopsy_cli.main.delete_keyring", lambda: False)

        result = runner.invoke(app, ["setup", "--remove"])

        assert result.exit_code == 0
        assert "No stored key" in result.output
