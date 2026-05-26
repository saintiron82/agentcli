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
   git tag v0.4.1
   git push origin main --tags
   ```

4. Create a simple GitHub Release named `v0.4.1` using
   [docs/releases/v0.4.1.md](releases/v0.4.1.md) as the release note.

5. Users can install with:

   ```bash
   pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.4.1"
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

## Current Recommendation

Prefer Stage 1 first. It keeps the package installable with `pip` while avoiding
locking the PyPI name before a small round of real-world validation.
