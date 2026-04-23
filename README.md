# agentcli

**Agentic CLI embedding toolkit** — Claude Code, Codex, and GitHub Copilot CLI behind one session-aware API.

Small, dependency-free library that lets other Python projects embed agentic command-line agents (the tools that run their own tool-use loop and own their session state) as AI backends.

---

## Why

Three CLIs in the "agentic CLI" category have converged on similar primitives but speak different event formats, session schemes, and permission flags:

| | Claude Code | Codex CLI | GitHub Copilot CLI |
|---|---|---|---|
| Session | `--session-id`, `--resume <sid>` | `codex exec resume <sid>` | `--resume=<sid>`, `--name=<alias>` |
| Streaming | `--output-format stream-json` | `--json` JSONL | `--output-format json` JSONL |
| Sandbox/permission | `--permission-mode`, `--allowedTools` | `-s <mode>`, `-a <policy>` | `--allow-tool`, `--deny-tool`, `--add-dir` |
| Session storage | `~/.claude/projects/<cwd>/<sid>.jsonl` | `~/.codex/sessions/…/<sid>.jsonl` | managed by Copilot CLI |

`agentcli` normalizes all three into one contract so apps can switch, parallelize, or combine them without rewriting per-CLI glue.

## Design principle

**The CLI session is the single source of truth for history.** The library stores only `session_id` per provider — it does not re-inject prior turns into prompts. This is what keeps the library lightweight and tokens predictable.

- Each call either starts a new session (library captures the sid) or resumes (library supplies the sid via CLI flag).
- `Conversation.metadata["session_id:<provider>"]` is persisted; content is not.
- Sessionless providers (e.g., plain HTTP models if added later) still work — the library serializes prior messages for them.

## Install

```bash
pip install agentcli
# or, as an editable install from source:
pip install -e /path/to/agentcli
```

Requires Python 3.11+. Zero runtime dependencies.

External CLI binaries are looked up on `PATH` at call time:
- `claude` (Claude Code)
- `codex`
- `copilot` (or `gh copilot`)

## Quick start

### 1. One-shot call

```python
import asyncio
from agentcli import LLMClient, MemoryStore

client = LLMClient(store=MemoryStore())

resp = asyncio.run(client.chat_async(
    "Summarize today's market in one sentence.",
    provider="claude",
    owner="analyst-bot",
    alias="market-summary",
    cwd="/path/to/workspace",  # controls where the agent's session files live
))
print(resp.content)
print(resp.session_id, resp.tokens.total_tokens)
```

### 2. Multi-agent in parallel

```python
import asyncio
from agentcli import LLMClient, MemoryStore

client = LLMClient(store=MemoryStore())

async def team_analysis():
    return await asyncio.gather(
        client.chat_async("Bull case for NVDA?",
                          provider="claude", owner="team", alias="bull"),
        client.chat_async("Bear case for NVDA?",
                          provider="codex",  owner="team", alias="bear"),
        client.chat_async("Final trade call given both?",
                          provider="claude", owner="team", alias="trader"),
    )

results = asyncio.run(team_analysis())
```

Three independent agent sessions run truly in parallel (`asyncio.create_subprocess_exec`). Each session is addressed by its `alias` — re-using the same alias resumes the same agent session.

### 3. Streaming

```python
async for chunk in client.chat_stream(
    "Draft a blog post about embeddings.",
    provider="claude", owner="writer", alias="blog",
):
    if chunk.type == "text":
        print(chunk.content, end="", flush=True)
    elif chunk.type == "tool_use":
        print(f"\n[tool: {chunk.data.get('name')}]", flush=True)
    elif chunk.type == "done":
        print(f"\n[total tokens: {chunk.usage.total_tokens}]")
```

Normalized chunk types: `text` · `thinking` · `tool_use` · `tool_result` · `event` · `error` · `done`.

### 4. Agent profile + materialization

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

### 5. Drift observability

Every call hashes `AGENTS.md` / `CLAUDE.md` / `AGENTS.override.md` in `cwd` (mtime-cached) and records to `Conversation.metadata`. A warning log fires when instructions change between calls on the same conversation.

```python
drifts = client.list_drifts(owner="team")
for row in drifts:
    print(row["alias"], row["cwd_hashes"])
```

### 6. Token tracking

```python
# Raw totals
stats = client.get_token_stats(owner="team")
# {total_tokens, total_prompt, total_completion, total_cached,
#  total_latency_ms, total_calls, by_provider}

# Group by alias / model / provider / agent / day
by_alias = client.get_token_stats(owner="team", group_by="alias")
for alias, b in by_alias["groups"].items():
    print(alias, b["total_tokens"], "cached:", b["total_cached"])

# Filter
sonnet_only = client.get_token_stats(owner="team",
                                     provider="claude", model="sonnet")
```

`total_cached` tracks Codex's `cached_input_tokens` (prompt caching). Copilot does not expose input tokens — `prompt_tokens` is 0 for Copilot calls.

## API surface

```python
from agentcli import (
    # Client
    LLMClient,

    # Data
    Message, Conversation, LLMResponse, TokenUsage, StreamChunk,

    # Providers
    LLMProvider, ProviderRegistry, create_default_registry,

    # Storage
    ConversationStore, MemoryStore, SQLiteStore,

    # Profiles
    AgentProfile, AgentRegistry, set_default_client,
)
```

| Method | Returns |
|---|---|
| `client.chat(prompt, *, provider, alias, cwd, …)` | `LLMResponse` |
| `client.chat_async(prompt, …)` | `awaitable[LLMResponse]` |
| `client.chat_stream(prompt, …)` | `AsyncIterator[StreamChunk]` |
| `client.get_token_stats(owner, …, group_by=)` | `dict` |
| `client.list_drifts(owner=, alias=)` | `list[dict]` |
| `profile.chat_async(prompt, client=None, cwd=, materialize=)` | `awaitable[LLMResponse]` |
| `profile.materialize(cwd)` | `dict` (write manifest) |

## Provider capabilities

| Provider | `supports_sessions` | `supports_streaming` | Session ID source |
|---|---|---|---|
| `ClaudeProvider` | ✅ | ✅ | Library-generated UUID via `--session-id` |
| `CodexProvider` | ✅ | ✅ | Parsed from `thread.started.thread_id` |
| `CopilotProvider` | ✅ | ✅ | Parsed from `result.sessionId` |

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

164 tests cover session routing, async/streaming parity, alias resolution, drift detection, usage aggregation, profile materialization, and Codex/Copilot JSONL parsing.

## Status

- **0.2.0** — session parity across 3 providers, async + streaming, AgentProfile + drift, multi-axis token aggregation. API considered stable but not yet 1.0.
- Runtime deps: **none**.
- Tested on macOS. Linux should work (same CLI invocation path); Windows partial (only via `gh copilot` wrapper).

## License

MIT. See `LICENSE`.
