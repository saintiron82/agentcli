# Release Checklist

[English](release.md) | [한국어](release.ko.md)

This project can be distributed in two stages.

## Stage 1: GitHub-first public release

Use this first while the API is still beta and external users are validating
real Claude Code, Codex, and GitHub Copilot CLI behavior.

1. Commit the release-ready tree.
2. Create or publish `https://github.com/saintiron82/agentcli`.
3. Tag the commit:

   ```bash
   git tag v0.7.5
   git push origin main
   git push origin v0.7.5
   ```

4. Wait for the Release workflow to pass its tests/build gate and create the
   GitHub Release `v0.7.5` with wheel and source distributions, using
   [docs/releases/v0.7.5.md](releases/v0.7.5.md) as the release note.

5. Users can install with:

   ```bash
   pip install "agentcli-py @ git+https://github.com/saintiron82/agentcli.git@v0.7.5"
   ```

## Stage 2: PyPI release

Use this when the GitHub release has basic external validation and the package
name should become the stable public install path.

1. Configure a PyPI pending Trusted Publisher for `agentcli-py` (details below).
   A pending publisher does not reserve the name; the first upload creates the
   project. Alternatively, use an API token for a manual upload.
2. Build and check distributions:

   ```bash
   python -m pip install -e ".[dev]"
   python -m pytest
   python -m build
   python -m twine check dist/*
   ```

3. Upload:

   ```bash
   python -m twine upload dist/*
   ```

4. Users can install with:

   ```bash
   pip install agentcli-py
   ```

## Automated release (GitHub Actions)

`.github/workflows/release.yml` creates a GitHub Release on a `v*` tag push.
PyPI is a separate, opt-in publication path:

```bash
git tag v0.7.5
git push origin main
git push origin v0.7.5
```

The workflow then:

1. **build + verify** — installs `".[dev]"`, runs `pytest` as a gate (a failing
   tag never ships), `python -m build`, and `twine check dist/*`.
2. **github-release** — after successful build verification, creates the GitHub
   Release from `docs/releases/<tag>.md` with the built distributions attached.
   PyPI being disabled or failing does not block this job.
3. **pypi-publish** — only when the repository variable `PYPI_PUBLISH_ENABLED`
   is `true`, uploads via **PyPI Trusted Publishing (OIDC)** using
   `pypa/gh-action-pypi-publish`, *not* `twine upload`. No API token is stored;
   `twine` is used only for the `check` in step 1.

One-time setup before enabling PyPI (on PyPI → *Publishing* → add a pending
publisher): project `agentcli-py`, owner `saintiron82`, repo `agentcli`,
workflow `release.yml`, environment `pypi`. Then enable the GitHub repository
variable `PYPI_PUBLISH_ENABLED=true` before pushing a new release tag. Keep it
unset or `false` for GitHub-only releases. Do not infer PyPI availability from
the presence of a GitHub Release. To use an API token instead, drop
`permissions.id-token` + `environment` from the `pypi-publish` job and pass
`password: ${{ secrets.PYPI_API_TOKEN }}` to the publish step.

## Current Recommendation

Prefer Stage 1 first. Users can install the tagged source or the attached wheel
without waiting for PyPI registration. Update `pyproject.toml`, both README
install snippets, and paired release notes before committing/tagging a version.
Never move an already published version tag to different contents.
