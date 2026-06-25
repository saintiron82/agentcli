# Changelog

## 0.6.2 — 2026-06-25

### Added
- **`ClaudeProvider` lean mode (`lean=True`).** For single-shot completions
  (summarize/generate — no tool use), appends `--safe-mode` (disables
  CLAUDE.md/skills/plugins/hooks/MCP/custom agents) + `--tools` allowlist
  (`""` to disable all built-in tools, or the explicit `allowed_tools`). This
  strips the agent harness so a large-context completion does not pay for
  MCP startup or risk an autonomous tool loop. Constructor arg + per-call
  override + `provider_options={"lean": True}`. MCP/`disallowed_tools` are
  ignored in lean mode. Default `False` — existing behavior unchanged.
- **Optional debug instrumentation (`debug=True`, `debug_log_path=...`).**
  Appends Claude's `--debug` flag, logs the (prompt-redacted) argv, and — for
  streaming — records a per-chunk timeline (`+{elapsed}s type evt/name`) so a
  tool loop or init stall is legible. `debug_log_path` appends a JSON-Lines
  trace (argv, chunk timeline, drained stderr, elapsed). stderr is drained
  concurrently during streaming to avoid a `--debug` pipe-buffer deadlock.
  Threaded through `invoke`/`invoke_async`/`stream_async` and
  `provider_options`. Default `False`.

### Fixed
- **Zombie grandchild-process accumulation.** A CLI spawns its
  own children (MCP servers, hooks, node helpers). Killing only the direct
  child on timeout/cleanup left those grandchildren running — and a grandchild
  holding the stdout pipe could even wedge `subprocess.run`'s post-timeout
  cleanup. All spawn sites now start a new session (process group) and tear
  down the **whole group** via `os.killpg(SIGKILL)` on timeout/cancel/early-exit
  (POSIX; Windows falls back to direct kill). New `run_subprocess_sync` replaces
  `subprocess.run` in `ClaudeProvider.invoke` for the same group teardown +
  `stdin=DEVNULL`. Verified with deterministic repro tests (sync + async +
  streaming).

### Notes
- `lean`/`debug` are Claude-specific (they map to Claude Code flags). The
  client's `provider_options` filtering means other providers safely ignore
  them; Codex/Copilot equivalents are not implemented.
- The process-group teardown lives in the shared `base` runners, so the
  streaming and async paths benefit across all providers; the sync
  `run_subprocess_sync` switch is wired for claude (codex/copilot can adopt).

## 0.6.1 — 2026-06-21

### Added
- **codex `mcp_config` pass-through (#154 follow-up).** `CodexProvider`
  invoke/invoke_async/stream_async + `provider_options` accept `mcp_config`
  (codex-native shape `{name: {url, bearer_token_env_var?}}` or `{name:
  {command, args?, env?}}`); each server is injected per-call as
  `-c mcp_servers.<name>=<TOML inline table>` (`~/.codex/config.toml` override).
  An embedded codex can now reach external MCP servers — verified end-to-end:
  codex performed a real MCP tool call against a throwaway server. Note: in
  non-interactive `codex exec`, MCP tool calls must clear codex's approval gate
  (a restrictive `sandbox_mode` + `approval_policy="never"` cancels them).

## 0.6.0 — 2026-06-21

### Added
- **KiroProvider** — 네 번째 provider. `kiro-cli acp`(ACP, JSON-RPC 2.0 over
  stdio)를 호출당 one-shot turn 으로 감싸 세션 연속성·타입드 스트리밍·토큰
  통계를 제공. 외부 LLMProvider 계약·청크 타입은 기존 3종과 동일. 제로 의존성.
- **호출 시점 `provider_options` — MCP & 권한 오버라이드 (#154).**
  `LLMClient.chat`/`chat_async`/`chat_stream` 에 `provider_options` 인자 추가:
  도구/권한/MCP 설정을 호출마다 오버라이드한다(키는 그 키를 받는 provider 에만
  전달 → fallback 안전). `ClaudeProvider` 가 `--mcp-config`
  (+`--strict-mcp-config`)를 방출해 임베드된 claude 가 외부 MCP 서버(예: Pair)에
  닿을 수 있고, `permission_mode`/`allowed_tools` 를 호출 시점에 바꿔 같은
  세션에서 읽기↔행위를 오간다. `CodexProvider` 는 `sandbox_mode`/`approval_policy`
  오버라이드(행위 턴은 `new_session=True` 와 병행 — resume 는 `-s` 무시). 실제
  claude CLI 로 `--mcp-config` 포맷 수용 확인.

### Fixed
- **KiroProvider `session/new`·`session/load` 에 ACP 필수 `mcpServers` 필드 누락.**
  실 `kiro-cli` 2.8.1 대상 라이브 검증에서 매 턴 stall(idle timeout) 발견 —
  `{"cwd": ...}` 만 보내 kiro 가 응답하지 않던 문제. `"mcpServers": []` 추가로
  수정. raw ACP spike 로 나머지 필드 매핑(`sessionUpdate`/`content.text`/
  `stopReason`)은 실측 일치 확인. 계정 인증(`kiro-cli login`)으로 spike 실행.

## 0.5.1 — 2026-06-10

실사용("호스트 프로그램의 에이전트 백엔드") 기준으로 핵심 격차 5건을 수정.

### Added
- **Claude 세션 resume 복구 (macOS/Linux).** `ClaudeProvider.supports_sessions`
  가 POSIX 에서 `True`. 첫 호출은 `--session-id` 발급, 이후 같은 conversation
  은 `-p --resume <sid>` 로 재개 (Claude Code 2.1.x 실검증 — resume 후에도
  동일 sid 유지). Windows 는 issue #4 hang 회피를 위해 기존 stateless 유지.
- 만료된 Claude session_id 로 resume 실패 시 ("No conversation found")
  새 세션으로 1회 자동 복구 — invoke/invoke_async/stream_async 모두.
- `LLMProvider.stores_history` 계약: CLI 가 히스토리를 소유하는 3-provider 는
  비세션 모드(Windows claude)에서도 대화 내용을 messages 테이블에 저장하지
  않고 이전 턴을 재주입하지 않는다. custom 비세션 provider 는 기본값 True.
- 같은 conversation 동시 호출 직렬화 (in-process): sync 는 per-conversation
  `threading.Lock`, async 는 루프별 `asyncio.Lock`. 잠금 획득 후 session_id
  재조회로 동시 호출이 CLI 세션을 분기·덮어쓰는 문제 차단.
- **히스토리 3-모드 명시화.** ① CLI 네이티브 세션(기본, alias resume),
  ② 호스트 주입(`inject_context` 가 세션 provider 에도 적용 — 이전에는
  3-provider 에서 무동작), ③ 미사용(신규 `new_session=True` 호출 단위
  스위치). `build_session_prompt` 가 명시 주입분을 "Context" 블록으로
  직렬화 — 무엇을 담을지는 client 의 모드 결정이 담당.
- E2E 검증 (Claude Code 2.1.x, agentcli 경유): 세션 연속성, `new_session`
  격리, `inject_context` 전달, 그리고 `cwd` 의 CLAUDE.md 지시 반영 /
  Agent Skills(`.claude/skills/`) 호출 / 커스텀 서브에이전트
  (`.claude/agents/`) 활성화 모두 동작 확인.

### Fixed
- **Copilot 스트리밍이 error 이벤트를 `event` 청크로 위장하던 문제.**
  `error`/`assistant.error`/`session.error` 및 `result.exitCode!=0` 이 이제
  정규화된 `error` 청크로 방출 — 호스트가 `chunk.type == "error"` 로 실패를
  감지할 수 있다 (비스트리밍 파서와 동일 계약).
- 배치 JSONL 파서 (claude/codex/copilot) 와 스트림 템플릿이 "유효한 JSON
  이지만 객체가 아닌" 라인에서 `AttributeError` 를 호스트로 전파하던 문제.
  배치는 skip, 스트림은 raw `event` 청크로 처리.
- `run_subprocess_async` 가 호출 task 취소(`CancelledError`) 시 subprocess
  를 정리하지 않던 누수 — streaming 쪽 issue #10 과 동일 클래스의 invoke
  경로 문제. `try/finally` 로 모든 종료 경로에서 kill 보장.
- Codex 위치 인자(prompt, resume session_id) 앞에 `--` 구분자 삽입 —
  `-` 로 시작하는 untrusted 입력이 CLI 플래그로 해석되는 주입 차단.

## 0.5.0 — 2026-05-31

Internal refactor — `providers/` 3-provider 중복 골격을 base 의 template
method + helper 로 흡수. 사용자 API (`LLMClient.chat/chat_async/chat_stream`,
`LLMResponse`, `StreamChunk`) 와 동작은 변경 없음. 신규 provider 추가 비용
감소 + 한 provider 만 수정해서 다른 둘과 어긋날 위험 감소가 목적 ([#7]).

### Added
- `LLMProvider._run_stream_template(cmd, state, ...)`: 3-provider 공통
  스트리밍 골격 (subprocess 생성 → readline + idle/wall timeout → JSON
  파싱 → `_dispatch_stream_event` hook → done/error + cleanup).
- `LLMProvider._dispatch_stream_event(evt, state)`: 정규화된 chunk 변환을
  위한 provider hook. default 는 raw `event` chunk.
- `StreamState` dataclass (base): `text_parts`, `final_session_id`,
  `final_usage`, `extra`.
- `run_subprocess_async(cmd, ...)` helper (base): `invoke_async` 공통
  subprocess + timeout 패턴. `(stdout, stderr, returncode, timed_out)` 반환.
- `tests/test_provider_normalization.py`: 3-provider 정규화 계약 회귀
  테스트 51건 — declarative attribute / 메서드 시그니처 / `list_models`
  shape / `resolve_model` / 바이너리 부재 path / `StreamChunk` 정규화
  타입 집합 / `build_session_prompt` 공유 사용.
- `repairman.adapter.yaml`: RePairMan 프로젝트 어댑터.

### Changed
- `ClaudeProvider.stream_async`: 487줄 → 391줄. 공통 골격은 base 위임,
  `_dispatch_stream_event` 가 system/assistant/user/result 해석.
- `CodexProvider.stream_async`: 600줄 → 512줄. thread.started/item.completed/
  turn.completed/error/turn.failed 해석.
- `CopilotProvider.stream_async`: 565줄 → 472줄. assistant.message_delta/
  message/result/tool_* 해석.
- 3-provider `invoke_async`: `run_subprocess_async` 사용으로 proc + timeout
  + cleanup 시퀀스 일원화.
- `providers/` 총합: 1894줄 → 1845줄. 중복 ~280줄이 base 한 곳으로 이동.

### Fixed
- `CodexProvider._build_cmd` 가 바이너리 없을 때 `"codex"` 문자열로 fallback
  하던 정규화 계약 위반을 수정. claude/copilot 와 동일하게 `None` 반환 →
  호출자가 즉시 `exit_code=127`, `error_type=binary_missing` 으로 실패한다.
  회귀 테스트가 시작 단계에서 잡았다.

### Compatibility
- 사용자 코드 변경 불필요. `LLMClient` API 시그니처·동작·streaming chunk
  순서·`LLMResponse` 필드 모두 그대로.
- 3-provider 정규화 계약 (chunk type 7종, session_single_source, zero
  runtime deps) 보존.
- 신규 provider 를 추가할 때는 hook 4개 (`_build_cmd`, `_dispatch_stream_event`,
  `invoke`, `health_check`) 만 구현하면 된다.

[#7]: https://github.com/saintiron82/agentcli/issues/7

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
- **Atomic store writes**: failed calls leave no residue (no unreferenced user messages in conversation history).
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
