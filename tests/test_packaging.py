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
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted(
    path.name
    for path in (ROOT / "packages").iterdir()
    # A pyproject is what makes a directory here a Python package. `runopsy-ui` is a
    # Node one: it belongs under packages/ because it is a component of the product,
    # and it is checked by its own CI job rather than by mypy.
    if path.is_dir() and (path / "pyproject.toml").is_file()
)


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

    @pytest.mark.parametrize("document", ["CONTRIBUTING.md", "CLAUDE.md"])
    @pytest.mark.parametrize("package", PACKAGES)
    def test_the_documented_command_checks_it_too(self, package: str, document: str) -> None:
        """Whoever runs the documented command should see what CI will see.

        Both files carry their own copy of the mypy invocation, and both had fallen
        behind the packages directory — CLAUDE.md by three. A command that passes
        locally and fails in CI wastes the time of the next person either way.
        """
        text = (ROOT / document).read_text(encoding="utf-8")

        assert f"packages/{package}/src" in text, (
            f"{package} is missing from the mypy command in {document}"
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


class TestWhatAPublishedWheelWillSay:
    """Metadata every distribution needs on an index, checked from the manifest.

    None of it affects a source checkout, which is exactly why it rots unnoticed: the
    first time anyone sees a package with no licence classifier, no repository link and
    no Python versions is after it is published, and a version number cannot be reused.
    """

    def manifest(self, package: str) -> dict[str, Any]:
        path = ROOT / "packages" / package / "pyproject.toml"
        project: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        return project

    @pytest.mark.parametrize("package", PACKAGES)
    def test_it_declares_its_licence_and_python_versions(self, package: str) -> None:
        project = self.manifest(package)
        classifiers = project.get("classifiers") or []

        assert project["license"] == "Apache-2.0"
        assert any("Apache Software License" in item for item in classifiers)
        assert any("Python :: 3.12" in item for item in classifiers)

    @pytest.mark.parametrize("package", PACKAGES)
    def test_it_points_back_at_the_repository(self, package: str) -> None:
        """A package on an index with no link home is one nobody can report a bug against."""
        urls = self.manifest(package).get("urls") or {}

        assert {"Homepage", "Repository", "Issues"} <= set(urls)

    @pytest.mark.parametrize("package", PACKAGES)
    def test_it_is_findable_and_described(self, package: str) -> None:
        project = self.manifest(package)

        assert project.get("keywords")
        assert len(str(project.get("description", ""))) > 20
        assert (ROOT / "packages" / package / "README.md").is_file()

    @pytest.mark.parametrize("package", PACKAGES)
    def test_claiming_to_be_typed_means_shipping_the_marker(self, package: str) -> None:
        """`Typing :: Typed` without py.typed makes every downstream mypy run silently
        treat this package as untyped — a promise that fails quietly."""
        project = self.manifest(package)
        if not any("Typing :: Typed" in item for item in project.get("classifiers") or []):
            pytest.skip(f"{package} does not claim to be typed")

        markers = list((ROOT / "packages" / package / "src").rglob("py.typed"))

        assert markers, f"{package} claims Typing :: Typed but ships no py.typed"


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
