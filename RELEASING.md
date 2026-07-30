# Releasing

Runopsy has not been published yet. This is the runbook for the first release and every
one after it.

It is written down because the release workflow depends on settings that live in the
GitHub and PyPI web interfaces rather than in this repository. Someone reading
`.github/workflows/release.yml` alone cannot tell that they exist, and a release that
fails halfway is the worst time to find out.

## Before the first push

The repository has never had a remote, so **CI has never run**. Every step in the
workflows has been executed locally on Windows and passes, but a green local run is not a
green matrix — Linux and macOS have not been observed, and the shell adapter runs real
subprocesses whose path handling has already caused one platform-specific bug.

Expect the first CI run to be where that gets settled, and treat a failure there as
information rather than as a surprise.

```bash
git remote add origin https://github.com/<owner>/runopsy.git
git push -u origin main
```

Push to a **private** repository first if you want the workflows observed before the code
is public. Actions run on private repositories, and making one public later is a single
setting; making a public one private again does not un-publish what was fetched.

## One-time setup on PyPI

The publish job uses **trusted publishing**, so there is no API token in repository
secrets to leak. That means PyPI has to be told which workflow it trusts, and until it is
told, the job fails at the last step with an authentication error.

For each of the eight distributions, on PyPI under *Publishing → Add a pending
publisher*:

| field | value |
| --- | --- |
| PyPI project name | `runopsy-core`, `runopsy-collector`, … (one entry per package) |
| Owner | your GitHub account or organisation |
| Repository name | `runopsy` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then create the `pypi` environment in the repository's *Settings → Environments*. Adding
a required reviewer there is worth doing: it makes publication a decision someone takes
rather than a side effect of pushing a tag.

A `PYPI_API_TOKEN` is **not** needed and should not be added. A long-lived token in
repository secrets is exactly the credential that trusted publishing exists to remove,
and a project whose subject matter is failure analysis should not keep one lying around.

## Cutting a release

1. Update `CHANGELOG.md`: move `[Unreleased]` to the version and date.
2. Set the same version in every `packages/*/pyproject.toml`. The build job compares the
   tag against `runopsy-cli`'s declared version and refuses a mismatch — a wheel whose
   version disagrees with the tag that produced it cannot be repaired once published,
   because the index keeps the file forever.
3. Confirm the benchmark report is current. CI checks this, but knowing the numbers moved
   before you tag is better than learning it after:
   ```bash
   uv run runopsy bench --write benchmarks/baseline-report.md && git diff --exit-code
   ```
4. Commit, tag and push:
   ```bash
   git tag -a v0.1.0 -m "Runopsy 0.1.0"
   git push origin main --follow-tags
   ```

The tag runs the full gate on all three platforms, builds, and only then publishes.

## Rehearsing without spending a version

Run the workflow manually from the Actions tab with `dry_run` left checked. It builds and
verifies everything and stops before publishing, so the pipeline can be exercised as
often as you like. Do this at least once before the first real tag.

## What a release does not yet claim

The benchmark numbers in the README come from 20 synthetic single-fault traces. They show
the ranking behaves as designed; they do not show it saves anyone time on real work.
Please do not let release notes upgrade that claim — the project's whole argument is that
a confident statement should be one you can check.
