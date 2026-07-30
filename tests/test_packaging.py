"""Guards on the repository's own wiring.

These test the build, not the product, and they exist because of a specific failure:
the CI type-check step names its packages by hand, and two of the eight were missing.
Nothing broke, no job went red, and mypy went on reporting success — it simply was not
looking at `runopsy-semantic` or `runopsy-server`. A gate that silently narrows is worse
than no gate, because the green tick keeps being believed.

A glob would not have fixed it: the CI matrix runs bash and PowerShell, and PowerShell
does not expand a glob for a native command, so `packages/*/src` would have reached mypy
as a literal path. The list stays explicit and this test keeps it honest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted(path.name for path in (ROOT / "packages").iterdir() if path.is_dir())


def workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


class TestEveryPackageIsWiredIn:
    def test_there_are_packages_to_check(self) -> None:
        """Guards the guards: an empty list would make every test below vacuously pass."""
        assert len(PACKAGES) >= 8

    @pytest.mark.parametrize("package", PACKAGES)
    def test_ci_type_checks_it(self, package: str) -> None:
        assert f"packages/{package}/src" in workflow("ci.yml"), (
            f"{package} is not in the CI type-check step, so mypy never sees it"
        )

    @pytest.mark.parametrize("package", PACKAGES)
    def test_the_workspace_resolves_it_locally(self, package: str) -> None:
        """Without this a package would resolve from PyPI, where it does not exist."""
        root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        assert root["tool"]["uv"]["sources"][package] == {"workspace": True}

    @pytest.mark.parametrize("package", PACKAGES)
    def test_it_declares_a_version_the_release_job_can_read(self, package: str) -> None:
        """The release job greps this line; a dynamic version would defeat the tag check."""
        manifest = (ROOT / "packages" / package / "pyproject.toml").read_text(encoding="utf-8")

        assert any(line.startswith("version = ") for line in manifest.splitlines())


class TestTheGatesCannotBeSkippedByAccident:
    def test_type_checking_covers_the_tests_too(self) -> None:
        """Tests are where the type errors that matter tend to be written."""
        assert workflow("ci.yml").rstrip().endswith("retention-days: 7")
        assert " tests\n" in workflow("ci.yml")

    def test_the_benchmark_report_is_verified_against_a_regeneration(self) -> None:
        """The claim in the README is only as good as the check that it is current."""
        assert "git diff --exit-code benchmarks/baseline-report.md" in workflow("ci.yml")

    def test_a_release_runs_the_full_gate_before_publishing(self) -> None:
        release = workflow("release.yml")

        assert "needs: verify" in release
        assert "startsWith(github.ref, 'refs/tags/v')" in release
