# agentcli Cookbook — recipes by situation

[English](cookbook.md) | [한국어](cookbook.ko.md)

Find your situation below and follow the recipe as-is. The *why* (mechanics
and measured numbers) lives in the
[README performance guide](../README.md#performance-guide-claude).
All recipes assume v0.7.3+.

## Quick index

| Situation | One-line recipe |
|---|---|
| [1. Bulk text organize/transform batches](#1-bulk-text-organizetransform-batches) | `env="lean"` — tools only slow these down |
| [2. Tool agent embedded in a service](#2-tool-agent-embedded-in-a-service) | keep the default, pass only what you need |
| [3. A tool that should use *my* machine's setup](#3-a-tool-that-should-use-my-machines-setup) | say `env="inherit"` explicitly |
| [4. Hundreds of calls with the same instructions](#4-hundreds-of-calls-with-the-same-instructions) | split `system_prompt` + stabilize the cache |
| [5. Killing per-call boot cost](#5-killing-per-call-boot-cost) | warm sessions |
| [6. Long responses over warm](#6-long-responses-over-warm) | `stream_limit` (default 8MiB) |
| [7. "It's slow" — where to look first](#7-its-slow--where-to-look-first) | 3-step diagnosis |
| [8. Unusually slow or stuck on Windows](#8-unusually-slow-or-stuck-on-windows) | upgrade the CLI + timeout ≥ 420s |
| [9. Running several in parallel](#9-running-several-in-parallel) | 3–4 concurrent + cache warming |
| [10. Quality dropped](#10-quality-dropped) | don't lower effort — find the cause |
| [11. Wondering about token cost](#11-wondering-about-token-cost) | read `resp.tokens` |
| [12. Upgrading from an old version](#12-upgrading-from-an-old-version) | one breaking change to check |

---

## 1. Bulk text organize/transform batches

Table rewriting, summarizing, classification, extraction — **text in, text
out**. With tools available the model wanders into tool loops and gets
several times slower (measured: the same 20k-char job took 6min on inherit
vs 2min 25s on lean).

```python
from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message

p = ClaudeProvider(env="lean")                 # no customization, no tools
r = p.invoke([Message(role="user", content=prompt)],
             model="sonnet", timeout=300)
```

- The remaining time is almost all **output generation** — to cut further,
  batch fewer items per call, or use `model="haiku"` where quality allows.
- Same instructions every call? Combine with
  [recipe 4](#4-hundreds-of-calls-with-the-same-instructions).
- Dozens of calls or more? Combine with
  [recipe 5](#5-killing-per-call-boot-cost).

## 2. Tool agent embedded in a service

An agent inside a host app (FastAPI etc.) that needs Bash/files/your own
MCP server. **The default tier (explicit) is designed for exactly this** —
whatever is installed on the dev machine stays out; only what you pass
gets in.

```python
p = ClaudeProvider(                            # env="explicit" is the default
    permission_mode="default",                 # tighten permissions when embedding
    allowed_tools=["Bash", "Read"],            # load only these built-in tools
    mcp_config={"myserver": {"type": "http",   # only your service's MCP
                             "url": "https://my/mcp"}},
)
```

- Mixing `mcp__myserver__*` names into `allowed_tools` automatically routes
  the list through the permission gate — MCP-tool narrowing keeps working.
- The "works on machine A, times out on machine B" reproducibility problem
  disappears with this default.

## 3. A tool that should use *my* machine's setup

If your dev-assistant tool *wants* your machine's CLAUDE.md, skills, and
MCP servers, say so explicitly:

```python
p = ClaudeProvider(env="inherit")              # the pre-0.7.2 default behavior
```

- The price: per-turn tokens can balloon by tens or hundreds of thousands
  depending on the host, and behavior differs across machines. Use it only
  when that's the point.

## 4. Hundreds of calls with the same instructions

Sending a multi-KB spec/instruction block on every call? Turn on two things:

```python
p = ClaudeProvider(
    exclude_dynamic_system_prompt=True,        # cache survives git changes
)
r = p.invoke(
    [Message(role="system", content=big_instructions),  # ← split as system,
     Message(role="user", content=small_payload)],      #   don't inline it
    model="sonnet")
print(r.tokens.cached_tokens)                  # verify the cache actually hit
```

- **Don't switch model or effort mid-batch** — both are part of the cache
  key; switching is a full cache miss.
- Cache TTL is 1 hour on subscription auth, extended on every hit.
- `exclude_dynamic_system_prompt` is opt-in because older CLIs lack the
  flag and error out — verified on Claude Code 2.1.229+.

## 5. Killing per-call boot cost

`claude -p` boots the harness on every call (~2s now, 3–11s on older
CLIs). Pipelines with many calls should boot once via a warm session:

```python
from agentcli.providers.warm import open_warm

s = await open_warm(append_system_prompt=instructions)   # lean by default
for item in batch:
    text = await s.send(item)                            # just the turn, no boot
await s.close()
```

- One session is serial — open several for parallelism.
- To drop context between items: `await s.send("/clear")`.

## 6. Long responses over warm

Since v0.7.3 the per-event line limit is 8MiB (it was 64KiB — the cause of
long responses dying with `LimitOverrunError`), so this normally needs no
attention. For genuinely bigger responses:

```python
s = await open_warm(stream_limit=32 * 1024 * 1024)
```

- Exceeding the limit raises `WarmSessionError`; close and reopen that
  session.

## 7. "It's slow" — where to look first

In order, one at a time:

1. **Versions** — agentcli v0.7.3+? CLI (`claude --version`) 2.1.229+?
   (Older CLIs boot several times slower, and 2.1.220/221 has the stall
   bug in [recipe 8](#8-unusually-slow-or-stuck-on-windows).)
2. **Tier** — is a data job running without `env="lean"`? If
   `r.tokens.prompt_tokens` is abnormally large for the task (tens of
   thousands+), host inheritance / tool definitions are riding along.
3. **Output volume** — if both are fine, check
   `r.tokens.completion_tokens`. Large output tokens *are* the time —
   batch fewer items per call or use a faster model. Still stumped?
   `ClaudeProvider(debug=True, debug_log_path=...)` records a per-chunk
   timeline.

## 8. Unusually slow or stuck on Windows

- Claude Code **2.1.220/221 has an unresolved bug where headless `-p`
  stalls ~405s** (claude-code#83859). On those versions, upgrade the CLI
  first. If you can't, keep timeouts ≥ 420s so a stall isn't mistaken for
  a hang.
- The classic Windows problems — the 32,767-char argv limit, stdin hangs —
  are handled by the library (prompts over 8,000 bytes route via stdin
  automatically). No action needed.

## 9. Running several in parallel

Rate limits are pooled per account. Per community reports, bursts beyond
3–4 simultaneous spawns hit 429 even on top paid tiers.

- Use a queue capped at 3–4 concurrent children with start jitter.
- Sessions in the same directory (cwd) share the server-side cache —
  **run one call to completion first to build the cache**, then release
  the rest. Faster and cheaper.

## 10. Quality dropped

If it got faster but worse, suspect in this order:

1. **Did lowering the tier cut context the task needed?** If the task
   depended on CLAUDE.md/skills, retry with `env="inherit"` and compare.
2. **Did you downgrade the model?** Revert. Real case: sonnet
   false-positive-refused security articles, so the 11%-slower opus was
   the right choice.
3. **Did you lower effort/thinking?** Hard-disabling thinking has a
   benchmarked **10–15pp accuracy cost**. Never lower it without an A/B on
   your own task; if you do step down, stop at `medium` (official anchor:
   Sonnet 5 medium ≈ Sonnet 4.6 high).

## 11. Wondering about token cost

Every response carries measured numbers — don't guess:

```python
r = p.invoke(...)
t = r.tokens
print(t.prompt_tokens,          # the real input context, in full
      t.cached_tokens,          # portion read from cache (~10% price)
      t.cache_creation_tokens,  # portion newly written to cache
      t.completion_tokens)      # output (the main time cost)
```

- `cached_tokens` near zero means
  [recipe 4](#4-hundreds-of-calls-with-the-same-instructions) has room to
  help.

## 12. Upgrading from an old version

```bash
pip install "agentcli-py @ git+https://github.com/saintiron82/agentcli.git@v0.7.4"
```

There is **one** breaking change to check (0.7.2): the claude provider no
longer inherits the host environment by default. After upgrading —

- Most embedding code: leave it alone (it gets faster and reproducible).
- Code that **depended on** host MCP/skills/CLAUDE.md: add one line,
  `env="inherit"`.
- Code using `lean=True` / `isolated=True`: keeps working (tier aliases).
- Coming from v0.6.x: warm sessions (0.7.1), cache-token visibility
  (0.7.2), and the 1s boot cut (0.7.3) come along for free.
