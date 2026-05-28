---
name: sync-i18n
description: Diff each English/Korean documentation pair (README, positioning, release, per-version release notes) and propose updates to bring them back in sync. Use when one side has changed and the other needs to follow, or to audit drift before a release.
---

# i18n parity check for agentcli

This repo keeps English and Korean docs paired. Every `*.md` has a matching `*.ko.md`. This skill detects drift and proposes minimal edits to restore parity.

## Pairs to check

- `README.md` ↔ `README.ko.md`
- `docs/positioning.md` ↔ `docs/positioning.ko.md`
- `docs/release.md` ↔ `docs/release.ko.md`
- Every `docs/releases/v*.md` ↔ `docs/releases/v*.ko.md`

## Steps

1. **List all pairs** by globbing for `*.md` files and their `*.ko.md` counterparts. Flag any `.md` without a `.ko.md` partner (or vice versa) as a parity gap.

2. **For each pair**, compare structure (headings, list items, code blocks, links). Use `git log -- <file>` to see which side was edited more recently — that side is usually authoritative.

3. **Classify drift** per pair:
   - **Headings/sections** present in one but missing in the other.
   - **Code blocks** that differ (these should normally be identical).
   - **Link targets** pointing to different files or anchors.
   - **Version strings / install snippets** out of sync.
   - **Prose updates** on one side without a corresponding update on the other.

4. **Report** as a single table per pair: column 1 = English, column 2 = Korean, column 3 = drift type. Then propose specific edits as diffs.

5. Do not auto-apply edits. Confirm direction (EN → KR or KR → EN) with the user per pair before writing.

## Notes

- The two sides are not required to be word-for-word translations; equivalent meaning and matching structure is the goal.
- Code blocks, command names, file paths, and version numbers must be identical across both sides.
- If a new doc was added in only one language, propose creating the missing counterpart as a stub with the structure mirrored.
