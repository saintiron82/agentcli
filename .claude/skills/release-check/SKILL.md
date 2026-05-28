---
name: release-check
description: Run the agentcli release checklist locally — pytest, build, twine check — and surface the tag/upload commands for the current version. Use when preparing a release or verifying the tree is release-ready.
---

# Release check for agentcli

Follows the two-stage release flow from `docs/release.md`.

## Steps

1. **Read the current version** from `pyproject.toml` (`[project].version`). Call it `VERSION`.

2. **Verify the dev environment**:
   ```bash
   python -m pip install -e ".[dev]"
   ```
   If this fails, stop and report why.

3. **Run the full test suite**:
   ```bash
   python -m pytest
   ```
   Stop on any failure. Do not auto-fix tests — surface failures to the user first.

4. **Build distributions**:
   ```bash
   rm -rf dist/
   python -m build
   ```

5. **Validate distributions**:
   ```bash
   python -m twine check dist/*
   ```

6. **Check doc parity**:
   - Confirm `docs/releases/v${VERSION}.md` and `docs/releases/v${VERSION}.ko.md` both exist.
   - Confirm `README.md` and `README.ko.md` install snippets reference `v${VERSION}`.
   - Confirm `CHANGELOG.md` has an entry for `${VERSION}`.

7. **Report** a summary to the user with:
   - Pass/fail for steps 2–5.
   - Doc-parity gaps from step 6, if any.
   - The exact next commands they should run for Stage 1 (GitHub):
     ```bash
     git tag v${VERSION}
     git push origin main --tags
     ```
   - And for Stage 2 (PyPI), if they confirm:
     ```bash
     python -m twine upload dist/*
     ```

Do not run `git tag`, `git push`, or `twine upload` automatically. These are user-confirmed steps.
