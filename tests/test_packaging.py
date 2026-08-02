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


class TestWhatPipInstallRunopsyGives:
    """The meta-distribution, which is the only name anyone will guess.

    Nine packages shipped and none of them was called `runopsy`, so the obvious command
    installed nothing of ours — and on a public index an unclaimed name is worse than
    missing, because somebody else can take it.
    """

    def manifest(self) -> dict[str, Any]:
        path = ROOT / "packages" / "runopsy" / "pyproject.toml"
        project: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        return project

    def test_the_obvious_name_exists(self) -> None:
        assert self.manifest()["name"] == "runopsy"

    def test_it_installs_the_whole_local_product(self) -> None:
        """Offline, no provider key: the CLI and the web view, not a stub."""
        dependencies = " ".join(self.manifest()["dependencies"])

        assert "runopsy-cli" in dependencies
        assert "runopsy-server" in dependencies

    def test_versions_are_pinned_exactly(self) -> None:
        """A meta-package whose parts can drift apart is a support burden, not a
        convenience: `runopsy` 0.1.0 must mean one known set of wheels."""
        for requirement in self.manifest()["dependencies"]:
            assert "==" in requirement, f"{requirement} is not pinned"

    def test_the_heavy_optional_dependency_stays_optional(self) -> None:
        """inspect-ai is large and most CLI users will never read an eval log."""
        extras = self.manifest()["optional-dependencies"]

        assert "inspect" in extras
        assert not any("runopsy-inspect" in item for item in self.manifest()["dependencies"])

    def test_it_ships_the_command_people_will_type(self) -> None:
        cli = tomllib.loads(
            (ROOT / "packages" / "runopsy-cli" / "pyproject.toml").read_text(encoding="utf-8")
        )

        assert cli["project"]["scripts"]["runopsy"] == "runopsy_cli.main:cli"


class TestTheParticularReleaseCannotComeApart:
    """Every package pins its siblings, not only the meta-distribution.

    The meta-package pinned `runopsy-cli` and `runopsy-server` exactly, and stopped
    there: the CLI's own dependencies on core, collector, replay and the rest floated.
    That was measured going wrong the first time it could. Publishing 0.1.1 while PyPI's
    index was still catching up resolved a 0.1.1 CLI onto a 0.1.0 collector — the release
    installed without the fix it was released for, and nothing reported anything unusual.

    A resolution race is the visible version of the standing problem: these packages
    share one trace schema and are released in lockstep, so any set that is not one
    version is a set nobody has run the tests against.
    """

    def manifests(self) -> dict[str, dict[str, Any]]:
        found = {}
        for path in sorted((ROOT / "packages").glob("*/pyproject.toml")):
            project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
            found[project["name"]] = project
        return found

    def test_every_sibling_dependency_names_one_version(self) -> None:
        manifests = self.manifests()
        names = set(manifests)

        for package, project in manifests.items():
            for requirement in project.get("dependencies", []):
                base = requirement.split("==")[0].split(">")[0].split("[")[0].strip()
                if base in names:
                    assert "==" in requirement, (
                        f"{package} depends on {base} without pinning it; "
                        "a mixed-version install is one nobody has tested"
                    )

    def test_the_pins_name_the_version_being_released(self) -> None:
        """A stale pin is worse than none: it resolves, and to the wrong thing."""
        manifests = self.manifests()
        version = manifests["runopsy"]["version"]

        for package, project in manifests.items():
            assert project["version"] == version, f"{package} is not at {version}"
            for requirement in [
                *project.get("dependencies", []),
                *[
                    item
                    for group in project.get("optional-dependencies", {}).values()
                    for item in group
                ],
            ]:
                if requirement.split("==")[0].strip() in manifests:
                    assert requirement.endswith(f"=={version}"), (
                        f"{package} pins {requirement}, but this release is {version}"
                    )


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

    @pytest.mark.parametrize("package", PACKAGES)
    def test_every_package_is_in_the_publish_matrix(self, package: str) -> None:
        """A package missing here is one that silently never reaches PyPI.

        The matrix is hand-listed because each entry needs its own environment: PyPI
        identifies a pending trusted publisher by (repository, workflow, environment),
        so ten distributions cannot share one. Hand-listed means it can fall behind,
        which is what this catches.
        """
        assert f"- {package}\n" in workflow("release.yml"), (
            f"{package} is not in the release publish matrix, so it would never be published"
        )

    @pytest.mark.parametrize("package", PACKAGES)
    def test_each_package_publishes_from_its_own_environment(self, package: str) -> None:
        """Shared environments are exactly what PyPI refuses to register twice."""
        release = workflow("release.yml")

        assert "environment: pypi-${{ matrix.package }}" in release
        assert release.count(f"- {package}\n") == 1
