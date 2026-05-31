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

- Each call either starts a new session (library captures the sid) or resumes (library supplies the sid via CLI flag).
- `Conversation.metadata["session_id:<provider>"]` is persisted; content is not.
- `system_prompt` / `AgentProfile.instructions` are injected only when a session has not seen that instruction hash yet, or when the instruction changes; prior user/assistant turns are not.
- Sessionless providers (e.g., plain HTTP models if added later) still work — the library serializes prior messages for them.

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
pip install agentcli

# Until then, install directly from the public GitHub repository:
pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.5.0"

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

| Provider | `supports_sessions` | `supports_streaming` | Session ID source |
|---|---|---|---|
| `ClaudeProvider` | ❌ (stateless `-p`) | ✅ | Per-call UUID via `--session-id` (audit only) |
| `CodexProvider` | ✅ | ✅ | Parsed from `thread.started.thread_id` |
| `CopilotProvider` | ✅ | ✅ | Parsed from `result.sessionId` |

`ClaudeProvider` runs `claude -p` (single-shot/stateless). `--resume` is
incompatible with `-p`, so the library does not store or replay session
metadata for claude (see issue #4). A fresh `--session-id` is still passed
per call so usage rows have a stable per-call identifier.

## Security notes

Each provider exposes its permission flags. **Defaults are permissive for dev convenience** — tighten them when embedding into multi-tenant or untrusted contexts.

```python
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
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
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

230 tests cover session routing, async/streaming parity, alias resolution, health checks, drift detection, usage aggregation, profile materialization, SQLite session persistence, and Codex/Copilot JSONL parsing.

## Status

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
- v0.4.3 release note: [docs/releases/v0.4.3.md](docs/releases/v0.4.3.md) / [docs/releases/v0.4.3.ko.md](docs/releases/v0.4.3.ko.md)

## License

MIT. See `LICENSE`.
