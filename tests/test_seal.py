"""Evidence that a journal has not been edited since it was recorded.

Every other check in this system asks whether the *recorder* did its job: contiguous
step numbers, no duplicates, nothing out of order. None of them can see a file somebody
opened afterwards and changed, because a trace with one line quietly rewritten is
perfectly contiguous — and the diagnosis built on it is fluent, confident and wrong.

The distinction these tests hold onto is between "modified" and "unknown". A journal
recorded before sealing existed has no seal, and calling that tampering would make the
check worthless within a week of shipping it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import START, run_end, run_start
from runopsy_cli.main import app
from runopsy_collector import Collector, SealState
from runopsy_collector.seal import GENESIS, Seal, compute, fold
from runopsy_core.schema import Event, RunOutcome, ToolCallEvent, ToolPayload

RUN = "run_sealed"
runner = CliRunner()


def tool(sequence: int, *, exit_code: int = 0) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=f"{RUN}_evt_{sequence:04d}",
        run_id=RUN,
        sequence=sequence,
        timestamp=START + timedelta(seconds=sequence),
        tool=ToolPayload(name="pytest", exit_code=exit_code),
    )


def a_run() -> list[Event]:
    return [
        run_start(RUN, task="record something worth trusting"),
        tool(1),
        tool(2, exit_code=1),
        run_end(3, RUN, outcome=RunOutcome.FAILURE),
    ]


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    with Collector.open(store) as collector:
        collector.record_all(a_run())
    return store


def journal_of(store: Path) -> Path:
    from runopsy_collector import StorePaths

    return StorePaths.resolve(store).journal(RUN)


class TestTheChainItself:
    def test_order_is_part_of_what_is_hashed(self) -> None:
        """Two events swapped must not fold to the same value as two in order."""
        forward, _ = compute(b"one\ntwo\n")
        backward, _ = compute(b"two\none\n")

        assert forward != backward

    def test_a_changed_byte_changes_the_digest(self) -> None:
        assert compute(b"one\ntwo\n")[0] != compute(b"one\ntwa\n")[0]

    def test_appending_continues_rather_than_restarts(self) -> None:
        """Sealing has to be incremental: a hook appends one line and leaves."""
        whole, _ = compute(b"one\ntwo\n")
        first, _ = compute(b"one\n")
        continued, _ = compute(b"two\n", start=first)

        assert continued == whole

    def test_blank_lines_are_not_events(self) -> None:
        assert compute(b"one\n\n\ntwo\n") == compute(b"one\ntwo\n")

    def test_the_chain_starts_somewhere_fixed(self) -> None:
        assert compute(b"one\n")[0] == fold(GENESIS, b"one")


class TestARecordedRun:
    def test_it_is_sealed_as_it_is_written(self, recorded: Path) -> None:
        with Collector.open(recorded) as collector:
            verdict = collector.verify(RUN)

        assert verdict.state is SealState.INTACT
        assert verdict.is_trustworthy
        assert verdict.lines == 4

    def test_recording_more_keeps_the_seal_valid(self, recorded: Path) -> None:
        """Sealing must survive the ordinary case of a run still being written."""
        with Collector.open(recorded) as collector:
            collector.record(tool(4))
            verdict = collector.verify(RUN)

        assert verdict.state is SealState.INTACT
        assert verdict.lines == 5

    def test_an_edited_step_is_caught(self, recorded: Path) -> None:
        """The tampering that matters: making a failed step look successful, which
        moves the onset and changes the diagnosis without leaving a mark."""
        journal = journal_of(recorded)
        journal.write_text(
            journal.read_text(encoding="utf-8").replace('"exit_code":1', '"exit_code":0'),
            encoding="utf-8",
        )

        with Collector.open(recorded) as collector:
            verdict = collector.verify(RUN)

        assert verdict.state is SealState.BROKEN
        assert not verdict.is_trustworthy

    def test_a_removed_step_is_caught(self, recorded: Path) -> None:
        journal = journal_of(recorded)
        lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
        journal.write_text("".join(lines[:-1]), encoding="utf-8")

        with Collector.open(recorded) as collector:
            assert collector.verify(RUN).state is SealState.BROKEN

    def test_an_inserted_step_is_caught(self, recorded: Path) -> None:
        """A fabricated step is the other half: not hiding evidence, manufacturing it."""
        from runopsy_collector import serialize

        journal = journal_of(recorded)
        with journal.open("ab") as handle:
            handle.write(serialize(tool(9)))

        with Collector.open(recorded) as collector:
            assert collector.verify(RUN).state is SealState.BROKEN

    def test_integrity_alone_would_have_called_it_fine(self, recorded: Path) -> None:
        """Why this exists. A rewritten line leaves the step numbers untouched."""
        journal = journal_of(recorded)
        journal.write_text(
            journal.read_text(encoding="utf-8").replace('"exit_code":1', '"exit_code":0'),
            encoding="utf-8",
        )

        with Collector.open(recorded) as collector:
            assert collector.integrity(RUN).is_intact
            assert collector.verify(RUN).state is SealState.BROKEN


class TestAbsenceIsNotGuilt:
    def test_a_journal_with_no_seal_is_unsealed_not_broken(self, recorded: Path) -> None:
        """Runs recorded before sealing existed, and traces imported from elsewhere."""
        Seal(journal_of(recorded).parent).reset()

        with Collector.open(recorded) as collector:
            verdict = collector.verify(RUN)

        assert verdict.state is SealState.UNSEALED
        assert not verdict.is_trustworthy  # unknown, which is not the same as intact

    def test_a_run_that_was_never_recorded_is_empty(self, tmp_path: Path) -> None:
        with Collector.open(tmp_path / "store") as collector:
            assert collector.verify("never_ran").state is SealState.EMPTY


class TestTheCommand:
    def test_it_reports_an_untouched_run(self, recorded: Path) -> None:
        result = runner.invoke(app, ["verify", RUN, "--store", str(recorded)])

        assert result.exit_code == 0
        assert "intact" in result.output

    def test_it_fails_the_command_when_a_run_was_modified(self, recorded: Path) -> None:
        """This one *should* fail a command: the evidence is not what it claims."""
        journal = journal_of(recorded)
        journal.write_text(
            journal.read_text(encoding="utf-8").replace('"exit_code":1', '"exit_code":0'),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["verify", RUN, "--store", str(recorded)])

        assert result.exit_code == 1
        assert "modified" in result.output.lower()

    def test_an_unsealed_run_does_not_fail_the_command(self, recorded: Path) -> None:
        Seal(journal_of(recorded).parent).reset()

        result = runner.invoke(app, ["verify", RUN, "--store", str(recorded)])

        assert result.exit_code == 0
        assert "no seal" in result.output

    def test_all_checks_every_run(self, recorded: Path) -> None:
        with Collector.open(recorded) as collector:
            collector.record_all(
                [
                    run_start("run_other", task="another"),
                    run_end(1, "run_other", outcome=RunOutcome.SUCCESS),
                ]
            )

        result = runner.invoke(app, ["verify", "--all", "--store", str(recorded)])

        assert result.exit_code == 0
        assert RUN in result.output
        assert "run_other" in result.output

    def test_doctor_says_so_too(self, recorded: Path) -> None:
        """Somebody checking whether their store is healthy should not have to know
        that a second command exists."""
        journal = journal_of(recorded)
        journal.write_text(
            journal.read_text(encoding="utf-8").replace('"exit_code":1', '"exit_code":0'),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["doctor", "--store", str(recorded)])

        assert "MODIFIED" in result.output


class TestSealingSurvivesConcurrency:
    def test_parallel_writers_do_not_break_their_own_seal(self, tmp_path: Path) -> None:
        """The failure mode worth guarding: the recorder accusing itself.

        Without the append and the fold happening inside one lock, two processes can
        write their bytes in one order and chain them in another. The journal would be
        perfectly correct and the seal would call it modified — which is worse than no
        seal at all, because it teaches people to ignore the check.
        """
        import concurrent.futures

        store = tmp_path / "store"
        with Collector.open(store) as collector:
            journal = collector.journal(RUN)

            def write(index: int) -> None:
                journal.append(tool(index))

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(24)))

            verdict = collector.verify(RUN)

        assert verdict.state is SealState.INTACT
        assert verdict.lines == 24


class TestTheSealIsNotOverclaimed:
    def test_the_command_says_what_it_cannot_do(self) -> None:
        """Tamper evidence, not tamper proofing. Whoever can edit the journal can
        delete the seal, and claiming otherwise would be the dishonest version.

        Read from the docstring rather than the rendered help, which a narrow terminal
        wraps and truncates — the wording is what is being pinned, not the layout.
        """
        from runopsy_cli.main import verify as verify_command

        assert verify_command.__doc__ is not None
        text = verify_command.__doc__.lower()

        assert "tamper evidence, not tamper" in text
        assert "delete the seal" in text

    def test_the_seal_lives_beside_the_journal_and_says_nothing_else(self, recorded: Path) -> None:
        """A digest, not a copy: the seal must not become a second place trace content
        lives, or redaction would have two files to get right instead of one."""
        seal = journal_of(recorded).parent / ".seal"
        content = seal.read_text(encoding="utf-8").strip()

        assert len(content) == 64
        assert all(character in "0123456789abcdef" for character in content)
        assert "pytest" not in json.dumps(content)
