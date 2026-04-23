# Changelog

## 0.2.0 — 2026-04-24

First release under the `agentcli` name (previously internal `libs.llm`). Major rework grounded in a new core principle: **the CLI session is the single source of truth for history; the library stores only `session_id`.**

### Added
- **Async + streaming parity**: `LLMClient.chat_async`, `chat_stream`, and per-provider `invoke_async` / `stream_async` across Claude, Codex, and Copilot.
- **Session parity**: all three CLIs now participate as tier-1 session providers with normalized `session_id` extracted from their native event streams.
  - Claude: library-generated UUID via `--session-id`.
  - Codex: parsed from `thread.started.thread_id` of `codex exec --json`.
  - Copilot: parsed from `result.sessionId` of `--output-format json`.
- **Alias-based conversation identity**: `(owner, alias)` addresses long-lived sessions ergonomically. SQLite-level uniqueness enforced.
- **`AgentProfile` + `AgentRegistry`**: single source of truth for instructions/model/tools; `materialize(cwd)` writes `AGENTS.md` + `CLAUDE.md` with a managed marker; user-authored files are protected.
- **Drift observer**: per-call mtime-cached SHA256 of `AGENTS.md` / `CLAUDE.md` / `AGENTS.override.md` at `cwd`. Logs warn on drift; `client.list_drifts()` enumerates.
- **Multi-axis token tracking**: `get_token_stats(owner, *, alias, provider, model, agent, group_by=...)` with group axes `provider` / `model` / `alias` / `agent` / `day`. Dedicated `cached_tokens` column for prompt-cache observability (Codex).
- **Permission controls per provider**:
  - Claude: `permission_mode`, `allowed_tools`, `disallowed_tools`.
  - Codex: `sandbox_mode`, `approval_policy`, `full_auto`.
  - Copilot: `allow_all_tools`, `allowed_tools`, `disallowed_tools`, `available_tools`, `add_dirs`, `effort`.
- **`cwd` parameter** flows through `LLMClient` → provider subprocess, isolating session files per project.
- **MemoryStore TTL + cap**; **SQLiteStore WAL + busy_timeout**; `build_env()` caches `gh auth token`.

### Changed
- **No more history re-injection for session providers.** Prior `prev_messages` rolling-context injection (which duplicated what the CLI session already held) is removed. Non-session providers still serialize history if/when added.
- **Atomic store writes**: failed calls leave no residue (no orphaned user messages in conversation history).
- **Default fallback order**: `["claude", "copilot", "codex"]` (session-first, full-auto last).
- **Claude uses `--output-format json`** and parses `usage.input_tokens` / `output_tokens` directly instead of stderr regex.
- **Codex uses `--output-format json`** event stream parsing; `cached_input_tokens` flows into `TokenUsage.cached_tokens`.
- **Copilot** session id comes from the actual `result.sessionId` (previously a library-generated UUID that didn't match what Copilot stored).

### Removed
- `prev_messages` auto-injection for session-capable providers.
- Implicit double storage of content for session providers.
- Provider-level sync/async signature reflection per call (now cached).

### Performance
- `chat()` hot path 3× faster on MemoryStore (10.2µs → 3.0µs per call).
- `inspect.signature` reflection per call eliminated (7µs → 0.1µs via class cache).
- Drift file hashing skipped when mtime unchanged.

### Internal
- Package path changed: `libs.llm.*` → `agentcli.*`.
- 164 tests (pytest). Zero runtime dependencies.
