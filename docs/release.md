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
   git tag v0.4.2
   git push origin main --tags
   ```

4. Create a simple GitHub Release named `v0.4.2` using
   [docs/releases/v0.4.2.md](releases/v0.4.2.md) as the release note.

5. Users can install with:

   ```bash
   pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.4.2"
   ```

## Stage 2: PyPI release

Use this when the GitHub release has basic external validation and the package
name should become the stable public install path.

1. Reserve or create the `agentcli` project on PyPI.
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
   pip install agentcli
   ```

## Automated release (GitHub Actions)

`.github/workflows/release.yml` runs both stages on a `v*` tag push, so the
manual commands above are the fallback path:

```bash
git tag v0.5.1
git push origin main --tags
```

The workflow then:

1. **build + verify** — installs `".[dev]"`, runs `pytest` as a gate (a failing
   tag never ships), `python -m build`, and `twine check dist/*`.
2. **pypi-publish** — uploads via **PyPI Trusted Publishing (OIDC)** using
   `pypa/gh-action-pypi-publish`, *not* `twine upload`. No API token is stored;
   `twine` is used only for the `check` in step 1.
3. **github-release** — gated on a successful PyPI upload, creates the GitHub
   Release from `docs/releases/<tag>.md` with the built distributions attached.

One-time setup before the first tag (on PyPI → *Publishing* → add a pending
publisher): project `agentcli-py`, owner `saintiron82`, repo `agentcli`,
workflow `release.yml`, environment `pypi`. To use an API token instead, drop
`permissions.id-token` + `environment` from the `pypi-publish` job and pass
`password: ${{ secrets.PYPI_API_TOKEN }}` to the publish step.

## Current Recommendation

Prefer Stage 1 first. It keeps the package installable with `pip` while avoiding
locking the PyPI name before a small round of real-world validation.
