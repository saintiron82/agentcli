# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agentcli` is a Python library that wraps three external agentic CLIs (`claude`, `codex`, `copilot`/`gh copilot`) into one session-aware client API. It is **not** an agent framework or a hosted LLM client.

## Hard constraints

- **Zero runtime dependencies.** Do not add anything to `[project.dependencies]` in `pyproject.toml`. Dev tools go in `[project.optional-dependencies].dev` only.
- **The CLI session is the single source of truth for history.** The library stores only `session_id` per provider — never re-inject prior user/assistant turns into prompts. `system_prompt` / `AgentProfile.instructions` are injected only when the instruction hash changes.
- **Three providers must stay normalized.** When adding or changing provider behavior, preserve the unified contract across `ClaudeProvider`, `CodexProvider`, `CopilotProvider` (session flags, streaming chunk types, permission flags). See the comparison table in `README.md`. Session resume is intended to work on every platform including Windows: the earlier Windows `-p` + `--resume` hang (issue #4) was an interactive-stdin wait, and all spawns use `stdin=DEVNULL`, so the former Windows `supports_sessions=False` guard was removed (issue #27 — CLI behavior reproduced on Windows 11; end-to-end Windows verification of the guard removal pending).
- **Korean and English docs are paired.** Every doc with a `.md` also has a `.ko.md` (README, `docs/positioning`, `docs/release`, `docs/releases/v*`). Changes to one must be mirrored in the other.

## Commands

- Install dev environment: `pip install -e ".[dev]"`
- Run tests: `pytest` (configured via `[tool.pytest.ini_options]`; `testpaths=tests`)
- Single test: `pytest tests/test_<file>.py -k <name>`
- Build distributions: `python -m build`
- Validate distributions: `python -m twine check dist/*`

No linter or formatter is configured. Do not introduce one unless the user asks.

## Streaming chunk contract

Normalized chunk types: `text` · `thinking` · `tool_use` · `tool_result` · `event` · `error` · `done`. Any new provider parsing must map to these — do not add new chunk types without updating all three providers and the docs.

## Release flow

Two-stage: GitHub tag/release first, then optional PyPI via twine. The full checklist is in `@docs/release.md`. Bumping versions touches `pyproject.toml` `version` and the README install snippet.

## Terminology

Do not use "고아" or "orphan" in any context. Prefer "참조 없는" / "unreferenced" / "잔여" / "residual".
