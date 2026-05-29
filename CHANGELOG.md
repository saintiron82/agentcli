# Changelog

## 0.4.3 — 2026-05-29

### Fixed
- Claude provider no longer attaches `--resume <session_id>` to the `-p`
  (print/single-shot) invocation. The two modes are structurally incompatible
  and the combination caused a 5-minute hang on Windows when the SQLite
  session manager replayed a stale session_id ([#4]).
- `ClaudeProvider.supports_sessions` is now `False`, reflecting that
  `claude -p` cannot resume a prior interactive session. The library no
  longer stores or replays `session_id:claude` metadata. `--session-id`
  is still emitted per call as a fresh per-call identifier for usage logs.

### Changed
- `LLMClient.get_alias_status(...)` will no longer report claude in
  `session_providers` for new calls — this is by design since `-p` is
  stateless. Pre-existing stored `session_id:claude` metadata in your
  SQLite store is harmless but unused.

[#4]: https://github.com/saintiron82/agentcli/issues/4

## 0.4.2 — 2026-05-26

### Fixed
- Codex provider now ignores the native CLI bootstrap greeting
  (`Ready. What would you like me to work on?`) when parsing JSONL output.
- If a new Codex session returns only that bootstrap greeting, agentcli retries
  the same prompt once by resuming the newly created Codex thread.

## 0.4.1 — 2026-05-26

### Fixed
- Codex provider now resolves the CLI binary path before invocation, so Windows
  installs such as `codex.CMD` are used consistently with health checks.
- Provider prompt token reporting is now explicit about source and reliability:
  `TokenUsage` carries `payload_prompt_tokens`,
  `prompt_tokens_reliable`, and `prompt_tokens_source`.

### Changed
- Usage stats now include `total_payload_prompt` and
  `prompt_tokens_unreliable_calls` so app integrations can separate the prompt
  agentcli sent from provider CLI reported input usage.

## 0.4.0 — 2026-05-06

### Added
- `agentcli.__version__` for runtime package version visibility.
- `ProviderHealth.public_dict()` for UI/log-safe health output without raw CLI
  stdout/stderr payloads.
- Standard stream error helpers: `make_error_chunk()` and
  `standardize_error_chunk()`.
- `chat_stream(..., fallback=True)` now supports provider fallback only when the
  primary stream fails before any output chunk is emitted.
- `LLMClient.get_alias_status(owner, alias, cwd)` summarizes fresh/stale
  instruction hash state, session providers, and last update time for one alias.
- `LLMClient.clear_session_metadata(owner=..., alias=..., provider=...)` clears
  only agentcli-owned metadata handles; native Claude/Codex/Copilot session files
  are never deleted.

### Changed
- Error chunks emitted by `LLMClient.chat_stream()` now consistently include
  `provider`, `error_type`, `recoverable`, `suggested_action`, and `exit_code`
  in `chunk.data`.
- Streaming fallback is intentionally conservative: after any text/tool/event
  chunk has been yielded, a later failure is returned as a standardized error
  and the provider is not switched.

### Documentation
- Repositioned the project as a session-aware embedding SDK rather than another
  end-user agent CLI.
- Added Korean documentation, explicit product boundaries, GitHub-first release
  guidance, and v0.4.0 release notes.

## 0.3.0 — 2026-05-05

### Added
- Explicit model selection helpers: `LLMClient.select_model()` /
  `resolve_model()` and provider-level selector validation.
- Updated built-in model catalogs for Codex and Copilot, including
  `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.1-codex-max`,
  `gpt-5.5`, `gpt-5.4`, `claude-opus-4.7`, and `claude-sonnet-4.6`.
- Updated Claude Code model catalog to explicit supported selectors and removed
  guessed aliases that the local CLI rejects.
- `strict_model=True` on `chat`, `chat_async`, and `chat_stream` to reject
  unsupported model selectors before invoking a CLI subprocess.
- `LLMClient.health_check(provider, probe=False)` and provider-level health
  diagnostics for CLI binary, auth, timeout, and optional quota probes.
- `reset_on_instruction_change=True` to force a fresh session when
  `AGENTS.md` / `CLAUDE.md` / `GUIDE.md` / `AGENTS.override.md` or inline
  system prompt hashes change.

### Changed
- Session providers now inject `system_prompt` / `AgentProfile.instructions`
  only when the session has not seen that instruction hash yet, or when it
  changes, while still excluding prior user/assistant turns.
- Failed new calls now roll back the placeholder conversation as well as
  messages, usage, aliases, and drift metadata.
- Provider subprocess failures now return structured `error` / `error_type`
  details instead of indistinguishable empty responses.
- `LLMResponse` now carries `exit_code`, `recoverable`, and
  `suggested_action` for stable operational handling.
- Streaming timeout semantics are explicit: `idle_timeout` controls silence
  between chunks and `wall_timeout` controls total runtime.
- Provider fallback is now opt-in with `fallback=True`; a call to an explicit
  provider no longer silently switches to another CLI.
- `chat_stream()` now emits an explicit `stream_unsupported` error when a
  primary stream fails instead of implying automatic stream fallback.
- Storage docs and the `SQLiteSessionStore` alias now make clear that SQLite
  persists session handles and usage logs, not session-provider chat history.
- README and examples now lead with the minimal operational path: health check,
  explicit provider/model, resumable alias, and instruction-change reset.
- Provider call adapters now pass only supported keyword arguments, so simpler
  custom providers do not break on `alias`, `cwd`, or session options.
- `conversation_id` + `alias` conflicts now fail explicitly instead of silently
  routing the call to the alias-owned conversation.
- Client store mutations are serialized when the store exposes a lock, reducing
  SQLite/MemoryStore races under concurrent `chat_async()` workloads.
- Provider timeouts now use structured `exit_code=124`, and stream timeout paths
  wait for killed subprocesses before returning.
- `SQLiteStore.get_token_stats(days=0)` now matches `MemoryStore` and returns
  all rows instead of an empty recent window.
- `AgentProfile.materialize()` no longer writes volatile timestamps into
  managed instruction files, avoiding false instruction-hash changes.

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
