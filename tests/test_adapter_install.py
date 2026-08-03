"""Writing the hook block into somebody else's config file.

The paste-it-yourself path stays the default and is tested elsewhere. What needs pinning
here is the promise that makes writing acceptable at all: a config we cannot read is
refused rather than overwritten, the previous contents survive, and a file we were only
meant to add four lines to comes back with every other line intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runopsy_adapter import hermes

COMMAND = "runopsy hook"


class TestItLeavesTheRestOfTheFileAlone:
    def test_a_config_without_hooks_keeps_its_comments_and_formatting(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        original = (
            "# my hermes setup, do not lose this\n"
            "model:   openai/gpt-4o-mini   # trailing comment\n"
            "\n"
            "plugins:\n"
            "  enabled:\n"
            "    - something-else\n"
        )
        config.write_text(original, encoding="utf-8")

        result = hermes.install_hooks(COMMAND, config_path=config)

        after = config.read_text(encoding="utf-8")
        assert after.startswith(original), "the original bytes must survive verbatim"
        assert result.rewrote_file is False
        assert "# my hermes setup, do not lose this" in after
        assert "# trailing comment" in after

        parsed = yaml.safe_load(after)
        assert parsed["plugins"]["enabled"] == ["something-else"]
        assert set(parsed["hooks"]) == set(hermes.RECORDED_EVENTS)

    def test_the_written_block_is_what_adapter_status_looks_for(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("model: openai/gpt-4o-mini\n", encoding="utf-8")

        hermes.install_hooks(COMMAND, config_path=config)

        status = hermes.adapter_status(config_path=config)
        assert status.is_wired, f"missing: {status.missing}, error: {status.parse_error}"
        assert not status.missing


class TestItRefusesWhatItCannotUnderstand:
    def test_an_unparseable_config_is_left_untouched(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        broken = "hooks: [unclosed\n  and: nonsense\n"
        config.write_text(broken, encoding="utf-8")

        with pytest.raises(ValueError, match="not parseable"):
            hermes.install_hooks(COMMAND, config_path=config)

        assert config.read_text(encoding="utf-8") == broken

    def test_a_config_that_is_not_a_mapping_is_refused(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("- just\n- a\n- list\n", encoding="utf-8")

        with pytest.raises(ValueError, match="not a mapping"):
            hermes.install_hooks(COMMAND, config_path=config)


class TestItKeepsWhatWasThereBefore:
    def test_the_previous_contents_are_copied_aside(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        original = "model: openai/gpt-4o-mini\n"
        config.write_text(original, encoding="utf-8")

        result = hermes.install_hooks(COMMAND, config_path=config)

        assert result.backup_path is not None
        assert result.backup_path.read_text(encoding="utf-8") == original

    def test_a_second_backup_does_not_overwrite_the_first(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("model: a\n", encoding="utf-8")
        first = hermes.install_hooks(COMMAND, config_path=config).backup_path

        # Take the hooks back out so there is something to install a second time.
        config.write_text("model: b\n", encoding="utf-8")
        second = hermes.install_hooks(COMMAND, config_path=config).backup_path

        assert first is not None
        assert second is not None
        assert first != second
        assert first.read_text(encoding="utf-8") == "model: a\n"
        assert second.read_text(encoding="utf-8") == "model: b\n"


class TestRunningItTwiceIsSafe:
    def test_the_second_run_changes_nothing(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("model: openai/gpt-4o-mini\n", encoding="utf-8")
        hermes.install_hooks(COMMAND, config_path=config)
        after_first = config.read_text(encoding="utf-8")

        result = hermes.install_hooks(COMMAND, config_path=config)

        assert result.added == ()
        assert result.backup_path is None, "nothing changed, so nothing needed saving"
        assert config.read_text(encoding="utf-8") == after_first

    def test_a_foreign_hooks_section_is_merged_rather_than_replaced(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(
            "hooks:\n  post_tool_call:\n    - command: 'someone-elses-tool'\n      timeout: 5\n",
            encoding="utf-8",
        )

        result = hermes.install_hooks(COMMAND, config_path=config)

        assert result.rewrote_file is True, "a hooks section forces the parsed round-trip"
        entries = yaml.safe_load(config.read_text(encoding="utf-8"))["hooks"]["post_tool_call"]
        commands = [entry["command"] for entry in entries]
        assert "someone-elses-tool" in commands, "the other tool's hook must survive"
        assert any("runopsy" in command for command in commands)


class TestItCreatesAConfigWhenHermesHasNone:
    def test_a_missing_file_is_created_without_claiming_a_backup(self, tmp_path: Path) -> None:
        config = tmp_path / "nested" / "config.yaml"

        result = hermes.install_hooks(COMMAND, config_path=config)

        assert result.created_config is True
        assert result.backup_path is None, "there was nothing to back up"
        assert hermes.adapter_status(config_path=config).is_wired


class TestEnablingThePlugin:
    def test_a_config_without_plugins_keeps_everything_else(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        original = "# keep me\nmodel: openai/gpt-4o-mini\n"
        config.write_text(original, encoding="utf-8")

        assert hermes.enable_plugin(config) is True

        after = config.read_text(encoding="utf-8")
        assert after.startswith(original), "the original bytes must survive verbatim"
        assert yaml.safe_load(after)["plugins"]["enabled"] == ["runopsy"]

    def test_an_existing_plugin_list_is_appended_to(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("plugins:\n  enabled:\n    - other\n", encoding="utf-8")

        assert hermes.enable_plugin(config) is True

        assert yaml.safe_load(config.read_text(encoding="utf-8"))["plugins"]["enabled"] == [
            "other",
            "runopsy",
        ]

    def test_running_it_twice_changes_nothing(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("model: a\n", encoding="utf-8")
        hermes.enable_plugin(config)
        after_first = config.read_text(encoding="utf-8")

        assert hermes.enable_plugin(config) is False
        assert config.read_text(encoding="utf-8") == after_first

    def test_hooks_and_plugin_together_leave_a_valid_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("model: openai/gpt-4o-mini\n", encoding="utf-8")

        hermes.install_hooks(COMMAND, config_path=config)
        hermes.enable_plugin(config)

        parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert parsed["model"] == "openai/gpt-4o-mini"
        assert parsed["plugins"]["enabled"] == ["runopsy"]
        assert hermes.adapter_status(config_path=config).is_wired
