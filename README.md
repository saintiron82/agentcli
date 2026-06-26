# agentcli

[English](README.md) | [한국어](README.ko.md)

**Session-aware Python client for embedding Claude Code, Codex, and GitHub Copilot CLI as application backends.**

`agentcli` is not another agent CLI. It is a small, dependency-free library for
Python apps that already want to call the user's installed agentic CLIs and need
one stable client API for sessions, streaming, usage logs, instruction drift,
and provider fallback.

---

## What this is

Use `agentcli` when your product or automation needs to treat Claude Code,
Codex, or GitHub Copilot CLI as a long-lived AI backend:

```python
resp = await client.chat_async(
    "Review this repository and summarize the main risks.",
    provider="codex",
    owner="my-app",
    alias="repo-reviewer",
    cwd="/path/to/project",
)
```

The host app gets a normal Python API. The provider CLI keeps owning its native
session, tools, auth, and local configuration.

## What this is not

- Not a replacement for Claude Code, Codex, or GitHub Copilot CLI.
- Not a hosted LLM API client and not a credential broker.
- Not a full agent framework with its own tool loop.
- Not a session-sync product that copies native CLI histories between tools.

Each user must install and authenticate the provider CLIs in their own
environment. `agentcli` does not ship credentials, sessions, or provider
binaries.

## Why

Three CLIs in the "agentic CLI" category have converged on similar primitives but speak different event formats, session schemes, and permission flags:

| | Claude Code | Codex CLI | GitHub Copilot CLI |
|---|---|---|---|
| Session | `--session-id`, `--resume <sid>` | `codex exec resume <sid>` | `--resume=<sid>`, `--name=<alias>` |
| Streaming | `--output-format stream-json` | `--json` JSONL | `--output-format json` JSONL |
| Sandbox/permission | `--permission-mode`, `--allowedTools` | `-s <mode>`, `-a <policy>` | `--allow-tool`, `--deny-tool`, `--add-dir` |
| Session storage | `~/.claude/projects/<cwd>/<sid>.jsonl` | `~/.codex/sessions/…/<sid>.jsonl` | managed by Copilot CLI |

`agentcli` normalizes all three into one contract so apps can switch, parallelize, or combine them without rewriting per-CLI glue.

The important boundary is higher than `subprocess.run(...)`: `agentcli` gives
apps a client layer for `owner + alias + cwd` identity, native session handles,
usage accounting, instruction freshness, safe health output, streaming errors,
and opt-in provider fallback.

## Project status

`agentcli` is beta-quality. The API is tested and usable, but provider CLIs move
quickly, so early adopters should pin versions and expect minor breaking changes
before 1.0.

For a fuller product boundary, see [docs/positioning.md](docs/positioning.md).

## Design principle

**The CLI session is the single source of truth for history.** The library stores only `session_id` per provider — it does not re-inject prior turns into prompts. This is what keeps the library lightweight and tokens predictable.

- Each call either starts a new session (library captures the sid) or resumes (library supplies the sid via CLI flag). The one exception is Claude on Windows, which stays stateless — see [Provider capabilities](#provider-capabilities).
- `Conversation.metadata["session_id:<provider>"]` is persisted; content is not.
- `system_prompt` / `AgentProfile.instructions` are injected only when a session has not seen that instruction hash yet, or when the instruction changes; prior user/assistant turns are not.
- Sessionless providers (e.g., plain HTTP models if added later) still work — the library serializes prior messages for them.

This principle is one of four project invariants. See [Design invariants](docs/positioning.md#design-invariants) for the full set (zero runtime deps, session as source of truth, three-provider parity, paired docs).

### Choosing how history is used

Every call picks one of three history modes explicitly:

| Mode | How |
|---|---|
| CLI-native session (default) | Same `owner` + `alias` resumes the provider's own session — the CLI remembers prior turns. |
| Host-injected context | `inject_context=[{"conversation_id": ..., "limit": 10, "agent": ""}]` serializes host-curated messages into the prompt as a labeled context block. Works with session providers too. |
| No history | `new_session=True` runs this call in a fresh CLI session (the alias then tracks the new session). Or simply use a new alias. |

Because the provider CLI owns its execution environment, project-level
`CLAUDE.md`/`AGENTS.md`, Agent Skills (`.claude/skills/`), and custom
subagents (`.claude/agents/`) in `cwd` are picked up natively by the CLI —
verified end-to-end against Claude Code 2.1.x.

## Storage model

Storage in `agentcli` is for **session routing and usage audit**, not chat
transcripts.

- `MemoryStore` is the default lightweight choice.
- `SQLiteStore` persists aliases, provider session IDs, instruction hashes, and
  usage rows across process restarts.
- For Claude/Codex/Copilot, the SQLite `messages` table stays empty because
  those CLIs own their own history.
- The message APIs exist for future or custom non-session providers that need
  library-managed context.

## Install

```bash
# After the first PyPI release:
pip install agentcli-py

# Until then, install directly from the public GitHub repository:
pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.6.3"

# For local development:
pip install -e /path/to/agentcli
```

Requires Python 3.11+. Zero runtime dependencies.

External CLI binaries are looked up on `PATH` at call time:
- `claude` (Claude Code)
- `codex`
- `copilot` (or `gh copilot`)

## Quick start

The normal path is deliberately small:

1. Choose the provider and model explicitly.
2. Check the CLI before a long job.
3. Use `owner` + `alias` + `cwd` when you want a resumable CLI session.
4. Use `reset_on_instruction_change=True` when `AGENTS.md` / `GUIDE.md` can change.

```python
import asyncio
from agentcli import LLMClient, MemoryStore

async def main():
    client = LLMClient(store=MemoryStore())

    health = client.health_check("claude")
    if not health.ok:
        raise RuntimeError(health.suggested_action or health.message)

    resp = await client.chat_async(
        "Summarize this repository in three bullets.",
        provider="claude",
        model=client.select_model("claude", "sonnet"),
        strict_model=True,
        owner="demo",
        alias="repo-summary",
        cwd="/path/to/workspace",
        reset_on_instruction_change=True,
        wall_timeout=300,
    )
    if not resp.content:
        raise RuntimeError(resp.suggested_action or resp.error)
    print(resp.content)

asyncio.run(main())
```

A call to a named provider tries that provider only. Cross-provider retry is opt-in:

```python
resp = await client.chat_async(
    "Try Claude first, then the configured fallback chain if it fails.",
    provider="claude",
    fallback=True,
)
```

Leave `fallback=False` when you need strict usage control or clear failure attribution.
`owner` + `alias` identifies one logical session; passing the same alias with a
different `conversation_id` is rejected instead of silently switching sessions.

### Streaming

```python
async for chunk in client.chat_stream(
    "Draft a blog post about embeddings.",
    provider="claude", owner="writer", alias="blog",
    fallback=True,       # only before the first output chunk
    idle_timeout=300,   # max silence between stream chunks
    wall_timeout=900,   # max total wall-clock runtime
):
    if chunk.type == "text":
        print(chunk.content, end="", flush=True)
    elif chunk.type == "tool_use":
        print(f"\n[tool: {chunk.data.get('name')}]", flush=True)
    elif chunk.type == "done":
        print(f"\n[total tokens: {chunk.usage.total_tokens}]")
```

Normalized chunk types: `text` · `thinking` · `tool_use` · `tool_result` · `event` · `error` · `done`.
If `fallback=True`, streaming fallback is attempted only before the first output
chunk. Once any text/tool/event chunk has been yielded, later failure is returned
as a structured `error` chunk and the provider is not switched.

### Agent profile + materialization

Keep agent instructions in one place and materialize them per project:

```
~/agents-registry/
├── bull-analyst/
│   ├── AGENTS.md           # the canonical instructions (read by Codex + Copilot)
│   ├── profile.json        # {model, provider, allowed_tools, ...}
│   └── skills/             # optional Agent Skills bundle
├── bear-analyst/
└── trader/
```

```python
from agentcli import AgentRegistry

registry = AgentRegistry.from_dir("~/agents-registry")

bull = registry.get("bull-analyst")
# Write AGENTS.md + CLAUDE.md into project dir (protects user-authored files)
bull.materialize("/path/to/project")

# Or do both in one shot:
resp = await bull.chat_async(
    "Analyze MSFT", owner="team",
    cwd="/path/to/project", materialize=True,
)
```

Materialize writes `AGENTS.md` (Codex/Copilot convention) and `CLAUDE.md` (Claude Code convention) with a managed marker. User-authored files without the marker are **not** overwritten — content is written to `AGENTS.override.md` instead (Codex's override convention).

## Operational details

### Health checks

Run diagnostics before starting a production job:

```python
health = client.health_check("claude")
print(health.status, health.message, health.suggested_action)
print(health.public_dict())  # UI/log-safe; raw stdout/stderr excluded

# Optional: performs a minimal model call to catch quota/usage-limit failures.
deep = client.health_check("codex", probe=True, timeout=20)
```

Health checks distinguish binary missing, auth required, usage limit, timeout,
and ok states where the underlying CLI exposes enough signal. They do not
perform login flows automatically; they return the command the operator should
run, such as `claude auth login`, `codex login`, or `copilot login`.

### Instruction refresh

Successful calls hash `AGENTS.md` / `CLAUDE.md` / `GUIDE.md` /
`AGENTS.override.md` in `cwd` (mtime-cached) and record to
`Conversation.metadata`. A warning log fires when instructions change between
calls on the same conversation.

```python
drifts = client.list_drifts(owner="team")
for row in drifts:
    print(row["alias"], row["cwd_hashes"])

status = client.get_alias_status("team", "collector", "/path/to/project")
print(status["status"], status["session_providers"])
```

Set `reset_on_instruction_change=True` to start a fresh provider session when
those hashes or the inline `system_prompt` hash no longer match the previous
successful call.

### Token tracking

```python
# Raw totals
stats = client.get_token_stats(owner="team")
# {total_tokens, total_prompt, total_completion, total_cached,
#  total_payload_prompt, prompt_tokens_unreliable_calls,
#  total_latency_ms, total_calls, by_provider}

# Group by alias / model / provider / agent / day
by_alias = client.get_token_stats(owner="team", group_by="alias")
for alias, b in by_alias["groups"].items():
    print(alias, b["total_tokens"], "cached:", b["total_cached"])

# Filter
sonnet_only = client.get_token_stats(owner="team",
                                     provider="claude", model="sonnet")
```

`total_prompt` remains the provider CLI reported prompt/input token count.
For session CLIs this value is not a portable "payload size": Codex can include
its internal agent context, Claude Code can report only a partial input count,
and Copilot does not expose input tokens. Use `total_payload_prompt` for the
small prompt string estimate that agentcli passed to the CLI, and check
`prompt_tokens_unreliable_calls` before using provider-reported prompt totals
for cost or comparison.

### Model selection

`list_models()` exposes the models this library knows how to pass to each CLI.
Use `strict_model=True` to reject unsupported model selectors before a subprocess
is started.

```python
client.list_models("codex")
# includes: gpt-5.3-codex, gpt-5.2-codex, gpt-5.5, gpt-5.4-mini, ...

model = client.select_model("codex", "gpt-5.3-codex")
resp = await client.chat_async(
    "Review this repository",
    provider="codex",
    model=model,
    strict_model=True,
    owner="review-bot",
    alias="repo-review",
)
```

This is explicit selection, not autonomous model routing. If `model=""`, the
underlying CLI default is used.

Copilot models are based on the local `copilot help config` catalog for the
installed CLI. Codex models are based on the current OpenAI model catalog.

## API surface

```python
from agentcli import (
    __version__,

    # Client
    LLMClient,

    # Data
    Message, Conversation, LLMResponse, ProviderHealth, TokenUsage,
    StreamChunk, make_error_chunk, standardize_error_chunk,

    # Providers
    LLMProvider, ProviderRegistry, create_default_registry,

    # Storage
    ConversationStore, MemoryStore, SQLiteStore, SQLiteSessionStore,

    # Profiles
    AgentProfile, AgentRegistry, set_default_client,
)
```

| Method | Returns |
|---|---|
| `client.chat(prompt, *, provider, alias, cwd, fallback=False, …)` | `LLMResponse` |
| `client.chat_async(prompt, *, provider, fallback=False, …)` | `awaitable[LLMResponse]` |
| `client.chat_stream(prompt, *, fallback=False, …)` | `AsyncIterator[StreamChunk]` |
| `client.list_models(provider=)` | `list[dict]` |
| `client.select_model(provider, model)` | `str` |
| `client.health_check(provider, probe=False)` | `ProviderHealth` |
| `client.get_token_stats(owner, …, group_by=)` | `dict` |
| `client.list_drifts(owner=, alias=)` | `list[dict]` |
| `client.get_alias_status(owner, alias, cwd)` | `dict` |
| `client.clear_session_metadata(owner=, alias=, provider=)` | `list[dict]` |
| `profile.chat_async(prompt, client=None, cwd=, materialize=)` | `awaitable[LLMResponse]` |
| `profile.materialize(cwd)` | `dict` (write manifest) |

`SQLiteSessionStore` is an alias for `SQLiteStore` that documents the intended
use: durable session handles and usage logs, not conversation history.

## Provider capabilities

Capabilities differ per provider **and per OS** (e.g. claude has no sessions on
Windows). Query them before calling instead of guessing — the controller is the
source of truth for "does this feature work on this provider here?":

```python
client.capabilities("claude")          # ProviderCapabilities (OS-aware)
client.supports("codex", "lean")       # False — lean is claude-only
client.capability_matrix()             # {provider: {...}} comparison table
# Which passed options this provider will silently drop:
client.unsupported_options("codex", {"lean": True, "sandbox_mode": "..."})
# -> ["lean"]   (sandbox_mode is supported, lean is not)
```

| Capability | claude | codex | copilot | kiro |
|---|---|---|---|---|
| `sessions` (resume) | ✅ (Win ❌) | ✅ | ✅ | ✅ |
| `streaming` | ✅ | ✅ | ✅ | ✅ |
| `token_streaming` | ✅ (`partial_messages`) | ❌ (block) | ✅ (native delta) | ❌ |
| `session_recovery` (auto-reopen) | ✅ | ✅ | ✅ | ❌ |
| `session_liveness` (`session_alive`) | ✅ | ✅ | ❌ | ❌ |
| claude-only options | `lean`, `debug`, `partial_messages` | — | — | — |

| Provider | `supports_sessions` | `supports_streaming` | Session ID source |
|---|---|---|---|
| `ClaudeProvider` | ✅ (macOS/Linux) · ❌ (Windows) | ✅ | First call mints `--session-id`; later calls pass `--resume <sid>` |
| `CodexProvider` | ✅ | ✅ | Parsed from `thread.started.thread_id` |
| `CopilotProvider` | ✅ | ✅ | Parsed from `result.sessionId` |
| `KiroProvider` | ✅ (ACP `session/load`) | ✅ | `session/new` result `sessionId`; transport = ACP JSON-RPC over stdio (`kiro-cli acp`) |

`KiroProvider` drives `kiro-cli acp` (line-delimited JSON-RPC 2.0) as one-shot turns per call: `initialize` → first turn `session/new` / resume `session/load(stored sessionId)` → `session/prompt` → `session/update` stream. Token usage comes from `usage_update` notifications; permissions are auto-answered via `session/request_permission` (`trust_all`/`trust_tools`). Authentication uses `KIRO_API_KEY` (or `kiro-cli login`).

`ClaudeProvider` runs `claude -p` with native session resume on macOS/Linux:
the first call mints a fresh `--session-id`, the library stores it, and later
calls on the same conversation pass `--resume <sid>`. The resumed session keeps
the same ID (verified against Claude Code 2.1.x). On Windows, `-p` combined
with `--resume` can fall back to interactive input and hang (issue #4), so the
provider stays stateless there: each call gets a fresh per-call `--session-id`
used for usage audit only, and no conversation content is persisted by the
library.

## Security notes

Each provider exposes its permission flags. **Defaults are permissive for dev convenience** — tighten them when embedding into multi-tenant or untrusted contexts.

```python
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.providers.kiro import KiroProvider
from agentcli import ProviderRegistry

registry = ProviderRegistry()
registry.register(ClaudeProvider(
    permission_mode="default",           # default | acceptEdits | plan | bypassPermissions
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash"],
))
registry.register(CodexProvider(
    sandbox_mode="workspace-write",      # read-only | workspace-write | danger-full-access
    full_auto=False,
))
registry.register(CopilotProvider(
    allow_all_tools=False,
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash"],
    add_dirs=["/tmp"],
))
# KiroProvider defaults to trust_all=True (auto-approves every tool call).
# In multi-tenant or untrusted contexts, set trust_all=False with explicit trust_tools.
registry.register(KiroProvider(
    trust_all=False,
    trust_tools=["Read", "Grep"],
))
```

### Call-time provider options (MCP & permissions)

The constructor flags above are fixed per provider instance. To switch tools /
permissions / MCP servers **per call** — e.g. flip the *same* conversation from
read-only to acting — pass `provider_options` to `chat` / `chat_async` /
`chat_stream`. Each key is forwarded only to providers whose call accepts it, so
one provider's options are harmlessly ignored on fallback to another.

```python
# Read turn: planning permission, no external tools.
client.chat("Summarize the open issues.", provider="claude",
            owner="team", alias="triager",
            provider_options={"permission_mode": "plan"})

# Act turn on the SAME session: attach an external MCP server and allow its
# write tools. The conversation continues; only permissions/tools change.
client.chat("Comment 'investigating' on issue #154.", provider="claude",
            owner="team", alias="triager",
            provider_options={
                "permission_mode": "acceptEdits",
                "mcp_config": {"pair": {                # ClaudeProvider wraps this
                    "type": "http",                     # under {"mcpServers": ...}
                    "url": "https://pair.example/mcp",
                    "headers": {"Authorization": "Bearer <token>"}}},
                "strict_mcp_config": True,              # ignore ambient MCP config
                "allowed_tools": ["mcp__pair__add_comment"]})
```

Supported `provider_options` keys:

- **claude** — `mcp_config` (dict → serialized under `--mcp-config`, or a
  path/JSON string), `strict_mcp_config`, `permission_mode`, `allowed_tools`,
  `disallowed_tools`, `lean`, `debug`, `debug_log_path`, `partial_messages`
  (streaming-only).
- **codex** — `mcp_config`, `sandbox_mode`, `approval_policy`. Codex reads MCP
  servers from `~/.codex/config.toml`, so its `mcp_config` uses the
  **codex-native shape** — `{name: {url, bearer_token_env_var?}}` (HTTP; the
  token is an env-var *name*, not inline headers like claude) or
  `{name: {command, args?, env?}}` (stdio) — and each server is injected
  per-call as `-c mcp_servers.<name>=<toml>`. An act turn that needs write
  access pairs `provider_options={"sandbox_mode": "workspace-write"}` with
  `new_session=True` (resume ignores `-s`). **Caveat:** in non-interactive
  `codex exec` an MCP tool call must pass codex's approval gate or it is
  cancelled (`user cancelled MCP tool call`) — configure approval accordingly.

`mcp_config` is **provider-shaped**: claude expects `{name: {type, url,
headers}}` (wrapped under `mcpServers`), codex expects the `mcp_servers` schema
above. If you only edit files in `cwd`, you don't need `mcp_config` at all — the
`permission_mode` / `allowed_tools` constructor flags (or these per-call
overrides) are enough.

### Lean mode & debug (claude)

For a single completion that needs no tools — summarize / generate / draft over
a large context — `lean=True` strips the agent harness: it adds `--safe-mode`
(no CLAUDE.md/skills/plugins/hooks/MCP/custom agents) and `--tools ""` (no
built-in tools). The completion then can't pay for MCP startup or wander into an
autonomous tool loop. The dominant remaining cost is output-token generation, so
also pick a faster model when latency matters.

```python
# ~50k-char text → meeting minutes, no tools needed:
client.chat(report_text, provider="claude",
            model="claude-haiku-4-5",            # output speed is the main lever
            provider_options={"lean": True})     # strip harness; block tool loops
```

When a call is mysteriously slow, `debug=True` (optionally with
`debug_log_path`) appends Claude's `--debug`, logs the prompt-redacted argv, and
for streaming records a per-chunk timeline so a tool loop or init stall is
visible. A `debug_log_path` appends a JSON-Lines trace (argv, chunk timeline,
stderr, elapsed) you can inspect after reproducing the slow call.

```python
async for chunk in client.chat_stream(prompt, provider="claude",
                                       provider_options={
                                           "debug": True,
                                           "debug_log_path": "/tmp/agentcli.jsonl"}):
    ...
```

By default Claude's `stream-json` emits whole message blocks, so a single long
completion arrives as one `text` chunk near the end. Pass
`partial_messages=True` (streaming only) to add `--include-partial-messages` so
Claude emits token-level `text_delta`/`thinking_delta` events — `chat_stream`
then yields incremental `text`/`thinking` chunks (live A/B: a 12s generation's
first token dropped from ~12s to ~6s, 1 chunk → 28).

```python
async for chunk in client.chat_stream(prompt, provider="claude",
                                      provider_options={"partial_messages": True}):
    if chunk.type == "text":
        print(chunk.content, end="", flush=True)   # token-by-token
```

`lean` / `debug` / `partial_messages` are Claude-specific; other providers
ignore them on fallback.

### Tracking running CLIs (diagnostics)

Because each spawn runs in its own process group (0.6.2+), the bundled
`scripts/agentcli_ps.py` lists in-flight `claude -p` / `codex exec` /
`copilot -p` runs grouped by PGID — leader plus its children (MCP servers, node
helpers) — and flags `[long]` / `[residual]` / `[defunct]` groups. Zero deps
(stdlib + `ps`, POSIX):

```bash
python3 scripts/agentcli_ps.py                 # running agent groups
python3 scripts/agentcli_ps.py --older-than 300  # only suspiciously long ones
python3 scripts/agentcli_ps.py --kill <PGID>     # SIGKILL a stuck group
```

### Session liveness & recovery

Sessions resume across calls (and across restarts with `SQLiteStore`). If a
stored session is gone, the next call auto-opens a fresh one — claude on
`No conversation found with session ID`, codex on `no rollout found for thread
id` — and persists the new id (the reopened session starts without prior
history, since the CLI owns history and agentcli stores only the id).

To check before calling, `session_alive` probes the session file without an LLM
call:

```python
alive = client.session_alive("claude", owner="team", alias="triager", cwd=cwd)
# True = resumable · False = gone (next call reopens) · None = unknown (e.g. copilot)
```

### Pinned context (one big input, many queries)

For "load a big transcript once, then ask many things against it" — e.g. several
meeting-minute formats and revision requests — `pin_context` returns a
`ContextSession` with two modes:

```python
ctx = client.pin_context(transcript, provider="claude", owner="u", alias="mtg",
                         model="claude-haiku-4-5")

# refine(): continue the same session — the transcript is sent ONCE, follow-ups
# send only the instruction (the CLI session remembers it).
ctx.refine("Draft formal minutes.")
ctx.refine("Make them shorter.")          # sees the prior answer

# fork(): independent variations — each re-seeds the transcript in a fresh
# session so they don't see each other.
ctx.fork("Extract action items only.")
ctx.fork("Write a casual summary.", label="summary")
```

When you need **several big outputs**, `fork_many` runs independent variations
in parallel with a concurrency cap (wall-time ≈ the slowest, not the sum):

```python
results = await ctx.fork_many(
    ["Detailed minutes.", "Full issue table.", "Stakeholder report."],
    concurrency=3, labels=["minutes", "issues", "report"])
```

For large outputs, `fork_stream` each item to a file, or run non-lean and let
the agent write the files itself (`Write` tool in `cwd`).

Come back later (or after a process restart): re-`pin_context` with the same
`alias` and transcript. `refine` checks `session_alive` — if the session is
still alive it resumes (no re-send); if it died (expired/deleted) it
**auto-re-seeds** the transcript into a fresh session. Each mode has
`*_async` / `*_stream` variants. (agentcli stores only the session id, so the
host holds the transcript to reconstruct the handle.)

## Testing

```bash
pip install -e ".[dev]"
pytest
```

662 tests cover session routing, async/streaming parity, alias resolution, health checks, drift detection, usage aggregation, profile materialization, SQLite session persistence, same-conversation concurrency, lean/debug command building, partial-message token streaming, process-group teardown, and Codex/Copilot JSONL parsing.

## Status

- **0.5.1** — Claude native session resume on macOS/Linux (stateless only on Windows, #4), explicit history modes (`new_session`, working `inject_context`), Copilot stream error normalization, same-conversation call serialization, Codex `--` argument hardening.
- **0.4.3** — Claude provider declared stateless: `-p` mode no longer pairs with `--resume`, fixing a 5-minute Windows hang (#4).
- **0.4.2** — Codex bootstrap greeting filtering and one-time resume retry for greeting-only first turns.
- **0.4.1** — Windows Codex binary resolution and explicit provider token usage reliability metadata.
- **0.4.0** — product-facing polish: safe health output, standardized stream errors, pre-output stream fallback, alias status, and metadata-only session cleanup.
- Runtime deps: **none**.
- Tested on macOS. Linux should work (same CLI invocation path); Windows partial (only via `gh copilot` wrapper).

## Documentation

- Korean README: [README.ko.md](README.ko.md)
- Product positioning: [docs/positioning.md](docs/positioning.md) / [docs/positioning.ko.md](docs/positioning.ko.md)
- Release checklist: [docs/release.md](docs/release.md) / [docs/release.ko.md](docs/release.ko.md)
- v0.6.3 release note: [docs/releases/v0.6.3.md](docs/releases/v0.6.3.md) / [docs/releases/v0.6.3.ko.md](docs/releases/v0.6.3.ko.md)

## License

MIT. See `LICENSE`.
