# Normalized reasoning controls — effort + thinking (design)

Date: 2026-07-03
Status: Draft for review
Scope: `agentcli` — two independent, normalized reasoning controls across
`claude` / `codex` / `copilot`.

## Problem

Today "reasoning control" is a fragment, not a system:

- `effort` exists only as a **constructor** parameter on `ClaudeProvider`
  (uncommitted working-tree change) and `CopilotProvider` (committed). It is
  **not** a per-call argument, so it never appears in `capabilities().options`
  (verified: `copilot options: ['debug','debug_log_path']`,
  `claude options: [... no effort]`).
- Providers are registered as no-arg singletons (`ClaudeProvider()` …), so
  `self._effort` is always `None` through the normal `LLMClient` path — the
  knob is effectively **unreachable**.
- `codex` has no effort knob at all; `CHANGELOG.md:398` already mis-lists
  copilot's options (drift).
- There is **no** normalized control for reasoning **output visibility**
  (thinking), even though the CLIs expose it.

The two axes are genuinely distinct (confirmed by the CLIs themselves):

- **effort** = how hard/long the model reasons (input dial). `claude --effort`,
  `copilot --effort/--reasoning-effort`, `codex -c model_reasoning_effort`.
- **thinking** = whether/how the reasoning is surfaced (output switch).
  `copilot --enable-reasoning-summaries` is a *separate* flag from `--effort` —
  proof the axes are orthogonal.

## Goals

1. Two **independent** normalized controls — `effort` and `thinking` — each
   with one canonical vocabulary that maps to every provider's native form.
2. First-class, per-call, reachable through the default registry.
3. Never silently degrade: when a provider can't honor a requested level,
   clamp to the nearest supported and **report** what was applied.
4. Preserve project invariants: zero runtime deps, three-provider parity,
   paired docs, existing streaming chunk contract, session-as-source-of-truth.

## Non-goals (YAGNI)

- `strict_effort` / `strict_thinking` raise-on-unsupported flags (clamp+report
  is the decided behavior).
- `kiro` reasoning controls (ACP transport; no effort/summary concept). `kiro`
  reports empty support and no-ops with a report.
- Changing how thinking **content** is parsed/streamed (already normalized as
  the `thinking` chunk type). This design controls *visibility requests*, not
  parsing.

## Verified provider reality

### effort (input dial)

| canonical | claude | copilot | codex (`model_reasoning_effort`) |
|-----------|--------|---------|----------------------------------|
| `minimal` | `low` (↑clamp) | `none` (rename) | `minimal` |
| `low`     | `low`  | `low`   | `low` |
| `medium`  | `medium` | `medium` | `medium` |
| `high`    | `high` | `high`  | `high` |
| `xhigh`   | `xhigh` | `xhigh` | `high` (↓clamp) |
| `max`     | `max`  | `max`   | `high` (↓clamp) |

Native accepted sets (from `--help`): claude `{low,medium,high,xhigh,max}`,
copilot `{none,low,medium,high,xhigh,max}`, codex OpenAI reasoning effort
`{minimal,low,medium,high}` (confirm against installed model at implementation).

### thinking (output switch)

| canonical | claude | copilot | codex (`model_reasoning_summary`) |
|-----------|--------|---------|-----------------------------------|
| `off`     | no-op* | (omit `--enable-reasoning-summaries`) | `none` |
| `concise` | no-op* | `--enable-reasoning-summaries` | `concise` |
| `detailed`| no-op* | `--enable-reasoning-summaries` (↓clamp to boolean) | `detailed` |

\* claude has **no** CLI toggle for reasoning-summary visibility (verified
absent in `--help`). claude thinking blocks are always emitted when the model
produces them and are already parsed into `thinking` chunks; the canonical
`thinking` control is a **documented no-op on claude** and reports
`applied=""`, `supported=false`.

codex `model_reasoning_summary` accepted values (`none|auto|concise|detailed`)
to be confirmed against the installed codex version during implementation; if a
value is unsupported the codex CLI surfaces its own error (we only emit a value
from the confirmed set).

## Architecture

### New module: `agentcli/reasoning.py` (zero-dep)

Single source of truth for both controls. Pure stdlib.

```
# Ordered canonical scales (least → most)
EFFORT   = ("minimal", "low", "medium", "high", "xhigh", "max")
THINKING = ("off", "concise", "detailed")

# Per-provider native maps + supported sets (one table per control)
# resolve(scale, provider_table, value) -> LevelResolution
@dataclass(frozen=True)
class LevelResolution:
    requested: str        # canonical value the caller passed
    applied: str          # native value emitted to the CLI ("" if unsupported)
    clamped: bool         # effective rank changed (True), i.e. the provider could
                          # not represent the requested rank and fell to the
                          # nearest. A pure same-rank rename (canonical `minimal`
                          # → copilot native `none`) is NOT clamped.
    supported: bool       # provider has this control at all (False on claude/thinking, kiro/*)

def resolve_effort(provider_id: str, canonical: str) -> LevelResolution
def resolve_thinking(provider_id: str, canonical: str) -> LevelResolution
def effort_levels(provider_id: str) -> frozenset[str]     # native supported
def thinking_levels(provider_id: str) -> frozenset[str]
```

- Unknown canonical value (not in the scale) → `ValueError` listing valid
  levels (mirrors `strict_model`). This is caller error, distinct from clamp.
- Clamp = nearest supported by ordinal index (up on the low end, down on the
  high end). Rename (canonical `minimal` → copilot `none`) is not a clamp when
  it is the same rank; it is a clamp only when the rank changes (canonical
  `minimal` → claude `low`).

### Provider changes (claude / codex / copilot)

Follow the existing `lean`/`debug` pattern exactly:

- Constructor gains `effort: str | None = None`, `thinking: str | None = None`
  (canonical values) as **defaults**.
- `invoke` / `invoke_async` / `stream_async` gain per-call
  `effort: str | None = None`, `thinking: str | None = None`.
  Effective value = per-call if not `None` else `self._effort` / `self._thinking`.
- In `_build_cmd`: for each set control, call the resolver and append the
  native flag:
  - claude: `--effort <native>`; thinking → no-op.
  - copilot: `--effort <native>`; thinking `concise|detailed` → append
    `--enable-reasoning-summaries`, `off` → omit.
  - codex: `-c model_reasoning_effort=<native>`, `-c model_reasoning_summary=<native>`
    via existing `_toml_inline`.
- Because the params are on `invoke`/`stream_async`, `capabilities().options`
  auto-includes `effort` and `thinking` for all three (inspect-derived). This
  also fixes the registry-singleton unreachability.

### Client changes (`LLMClient`)

- `chat` / `chat_async` / `chat_stream` gain first-class
  `effort: str | None = None`, `thinking: str | None = None`, threaded into the
  provider call like `model`.
- If the same control is given **both** as a first-class kwarg and inside
  `provider_options`, raise `ValueError` (ambiguity rejected — consistent with
  the alias/conversation_id mismatch rejection).

### Reporting (no silent degradation)

- `LLMResponse` gains `reasoning: ReasoningResolution | None`:

  ```
  @dataclass
  class ReasoningResolution:
      effort:   LevelResolution | None = None
      thinking: LevelResolution | None = None
  ```

  Populated whenever a control is set. `None` when neither control used
  (full backward compatibility — existing responses unchanged).
- Streaming: when any control is clamped or unsupported, emit **one** `event`
  chunk at stream start:
  `StreamChunk(type="event", data={"reasoning": {...}})`. No new chunk type
  (contract preserved). Non-clamped, supported controls emit no event; the
  applied value is still available via the final response metadata.

## Error handling / edge cases

- `effort`/`thinking` = `None` → no flag emitted, no metadata → unchanged
  behavior for all existing callers.
- Unknown canonical → `ValueError` (fail fast) at resolve time.
- Unsupported-on-provider (claude thinking, kiro anything) → no flag,
  `LevelResolution(supported=False, applied="")`, reported.
- codex value rejected by the model at runtime → codex's own error path
  (unchanged); we only ever emit a value from the confirmed native set.

## Testing

- `reasoning.py` unit: scale membership, unknown → `ValueError`, full
  effort/thinking mapping tables, clamp cases (effort `minimal`→claude `low`,
  `xhigh`/`max`→codex `high`; thinking `detailed`→copilot boolean, any→claude
  unsupported), `*_levels()` contents.
- Provider command-building: correct native flag per provider, per-call
  overrides constructor default, `None` omits flag, codex `-c` serialization.
- Capabilities: `options` includes `effort` and `thinking` for all three;
  `effort_levels` / `thinking_levels` populated; claude thinking empty.
- Client: `chat_async(effort=, thinking=)` threads through; `LLMResponse.reasoning`
  populated; double-specify (kwarg + provider_options) → `ValueError`.
- Streaming: clamp/unsupported emits exactly one `event` chunk with the
  resolution; supported+exact emits none.
- Backward-compat: calls without the controls produce byte-identical argv and
  `reasoning=None`.

## Docs & migration

- Paired docs (en/ko): README provider-comparison table gains `effort` /
  `thinking` rows; a short "Reasoning controls" section documents the canonical
  scales, the mapping tables, clamp+report semantics, and claude's thinking
  no-op. CHANGELOG entry. Fix the `CHANGELOG.md:398` copilot-options drift.
- The in-progress uncommitted `claude.py` effort change is **superseded**:
  reimplemented as the per-call pattern above rather than constructor-only.
- `capability_matrix()` gains effort/thinking columns (same treatment as the
  0.6.4 `debug` capability).

## Open items to confirm at implementation

1. codex `model_reasoning_summary` accepted value set on the installed version.
2. codex `model_reasoning_effort` accepted set for the target model
   (esp. whether `minimal` is valid there).
3. Exact shape of `ProviderCapabilities` additions (`effort_levels`,
   `thinking_levels`) vs. reusing a generic `reasoning` sub-struct.
