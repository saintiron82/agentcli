---
name: release-check
description: Use when preparing an agentcli release or verifying the working tree is release-ready. Triggers include `/release-check`, "릴리즈 점검", "pytest + build + twine 확인 후 태그 명령 알려줘". Does not execute git tag / push / twine upload — surfaces them for the user to confirm.
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
