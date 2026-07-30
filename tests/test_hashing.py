"""Hashing tests.

These digests are how a trace refers to prompts, arguments and files without carrying
them, so stability and format are part of the on-disk contract.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from runopsy_core import hash_bytes, hash_file, hash_text, is_digest


def test_digests_are_prefixed_and_lowercase_hex() -> None:
    digest = hash_text("runopsy")

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert is_digest(digest)


def test_known_value_matches_sha256_of_empty_input() -> None:
    """Pinned so a future refactor cannot silently change the algorithm."""
    assert hash_bytes(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_whitespace_differences_are_not_collapsed() -> None:
    """Prompt whitespace is a real difference; hiding it would hide replay divergence."""
    assert hash_text("a b") != hash_text("a  b")


def test_hash_file_matches_hash_bytes(tmp_path: Path) -> None:
    payload = b"integration test exit code 1\n" * 1000
    path = tmp_path / "output.log"
    path.write_bytes(payload)

    assert hash_file(path) == hash_bytes(payload)


def test_malformed_references_are_not_digests() -> None:
    assert not is_digest("deadbeef")
    assert not is_digest("md5:" + "a" * 32)
    assert not is_digest("sha256:" + "A" * 64)


@given(payload=st.binary(max_size=2048))
def test_hashing_is_deterministic(payload: bytes) -> None:
    assert hash_bytes(payload) == hash_bytes(payload)


@given(text=st.text(max_size=512))
def test_text_hashing_agrees_with_utf8_bytes(text: str) -> None:
    assert hash_text(text) == hash_bytes(text.encode("utf-8"))
