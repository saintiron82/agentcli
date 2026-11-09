# EXAONE ACP provider (unofficial) — Design

Status: draft for review · Date: 2026-11-09 · Branch: `feat/exaone-provider`

## 1. Problem

K-EXAONE 2.5 has no vendor CLI — only an OpenAI-compatible HTTP API with an API
key. agentcli wraps CLIs and must not become an HTTP LLM client or own
conversation history. Verified dead ends: Codex CLI 0.166.0 rejects
`wire_api = "chat"` ("no longer supported"), and EXAONE documents no Responses
or Anthropic-compatible endpoint, so neither Codex nor Claude Code can be
pointed at it.

Resolution: a separate, unofficial CLI (`exaone-cli`, own repo, spec
`D:\BProject\exaone-cli\docs\superpowers\specs\2026-11-09-exaone-cli-design.md`)
speaks ACP over stdio and owns sessions. agentcli wraps it like `kiro-cli acp`.

## 2. Goals / non-goals

Goals:

- `ExaoneProvider` (`provider_id="exaone"`) driving `exaone acp`, with sessions,
  streaming (`thinking` + `text` chunks), usage, effort control.
- Marked **unofficial**: not in `create_default_registry()`, opt-in only.
- Service auth profile support (`exaone_api_key`, per-profile `EXAONE_HOME`).
- Extract the reusable ACP driver out of `kiro.py` so Kiro and EXAONE share it.

Non-goals: tool loop (exaone-cli stage 2), per-call API key, quota lookup,
debug instrumentation, warm sessions, login flow (API key only).

## 3. ACP base extraction

New module `agentcli/providers/acp_base.py` with `AcpProvider(LLMProvider)`.
Moved from `KiroProvider` unchanged in behavior:

- `invoke`, `invoke_async`, `stream_async`, `_acp_turn`,
  `_subprocess_conn_factory`, `_handle_agent_request`, `_decide_permission`,
  `_within_cwd`, `_fs_read`, `_fs_write`, `_map_session_update`
- constructor options `trust_all`, `trust_tools`, `allow_file_reads`,
  `allow_file_writes`, `max_file_bytes`, `model`

Hooks a subclass provides:

| Hook | Kiro | EXAONE |
|---|---|---|
| `binary_name` | `kiro-cli` | `exaone` |
| `_command(binary, model, reasoning)` | `[bin, "acp", --agent?]` | `[bin, "acp", --model?, --reasoning-effort?]` |
| `usage_source` | `kiro_cli_reported` | `exaone_cli_reported` |
| `client_name` in `initialize` | `agentcli` | `agentcli` |
| `health_check` | unchanged Kiro version (rejects auth profiles) | §6 |

Strings that today say "kiro-cli not found" / "kiro stream error" become
`f"{binary_name} not found"` / `f"{provider_id} stream error"` — same text for
Kiro.

Bug fix carried by the extraction: per the ACP spec an agent **replays the
whole conversation as `session/update` notifications during `session/load`**.
The current Kiro driver maps every `session/update` it receives during the turn,
so on resume the replayed history would be emitted as new `text`/`thinking`
chunks and folded into the response. The base driver drops `session/update`
notifications that arrive before `session/prompt` is sent. This also fixes
Kiro (its scripted test agent does not replay, so existing tests are
unaffected); a new test with a replaying agent pins it.

Guard: the full existing Kiro test suite (`test_kiro_provider.py`,
`test_kiro_coverage.py`, `test_kiro_security.py`, ACP helpers, registry and
capability tests) must pass **without edits**. `kiro.py` re-imports any moved
symbol that tests or users import from it (e.g. `_map_session_update`,
`KIRO_MODELS`), and `KiroProvider` keeps its constructor signature, defaults
and module path.

## 4. ExaoneProvider

```python
class ExaoneProvider(AcpProvider):
    provider_id = "exaone"
    supports_sessions = True
    supports_streaming = True
    stores_history = False          # exaone-cli owns history
    binary_name = "exaone"
```

- Constructor: `model: str = "k-exaone_v2.5"`, `effort: str | None = None`,
  plus inherited ACP options. Defaults `trust_all=False`,
  `allow_file_reads=False`, `allow_file_writes=False`: exaone-cli stage 1 never
  requests tools or files, so nothing is granted by default (stricter than
  Kiro; re-evaluated in stage 2).
- `invoke` / `stream_async` gain `effort: str | None = None` (call value wins,
  constructor default otherwise, via `_resolve_reasoning`). The resolved native
  value is passed as `--reasoning-effort`; the `done` chunk carries the
  `ReasoningResolution` like the other providers.
- `thinking` is unsupported (EXAONE always reasons; reasoning is always streamed
  as `thinking` chunks). The empty thinking map makes `resolve_thinking` return
  `supported=False` (no exception), exactly like Claude: it is reported in
  `LLMResponse.reasoning` and as a streaming `event` chunk, never applied.
  `invoke`/`stream_async` therefore accept both `effort` and `thinking`.
- `list_models()` → `[{"id": "k-exaone_v2.5", "name": "K-EXAONE 2.5",
  "aliases": ["default"]}]`. Model selector is passed through non-strictly
  (unknown IDs are forwarded; the API decides).
- `capabilities().notes` = `"unofficial: wraps exaone-cli (not an LG AI Research
  product)"`.

### Effort mapping (`reasoning.py`)

| canonical | native | clamped |
|---|---|---|
| minimal | low | yes |
| low | low | no |
| medium | high | yes |
| high | high | no |
| xhigh | max | yes |
| max | max | no |

`medium` rounds up (equidistant between low and high; under-reasoning is the
worse failure). Thinking map for `exaone`: empty (unsupported).

## 5. Registration (unofficial, opt-in)

- Not added to `create_default_registry()` or the fallback order.
- Exported as `agentcli.providers.exaone.ExaoneProvider` only — not from
  `agentcli/__init__`.
- Usage: `reg.register(ExaoneProvider())`. Fallback order is up to the host.

## 6. Health check

- Binary missing → `binary_missing` (same as Kiro).
- `exaone --version` → version string; a nonzero exit is `cli_error`.
- No key in the effective child env → `auth_required`, `auth_ok=False`,
  message "EXAONE_API_KEY not set".
- Key present, no probe: under an auth profile, `configured_health` (same as
  Codex/Copilot, `status="configured"`); without a profile, `ok=True`,
  `auth_ok=None` (set but unverified).
- `probe=True` runs one minimal turn (consumes tokens; documented). It is
  safe to call from inside a running event loop (the probe runs on a worker
  thread with the copied context, so the auth profile scope is kept).
- Works with and without an auth profile.

## 7. Service auth profile

`AuthProfile` changes (`agentcli/auth.py`):

- New field `exaone_api_key: str | None` (repr hidden).
- `"exaone"` allowed in `disabled_providers` and in `directory()` (private dir
  `<root>/exaone`, created by `prepare()`).
- `method("exaone")` → `"api_key"`. `validate("exaone")` requires
  `exaone_api_key` (no file fallback; there is no login).
- `_environment("exaone")` sets `EXAONE_API_KEY` and
  `EXAONE_HOME=<root>/exaone`. `EXAONE_BASE_URL` is **not** inherited from the
  host (it is not in `_BASE_ENV`), so a host value cannot redirect a profile's
  key.
- `_AUTH_PREFIXES` gains `"EXAONE_"`, so `extra_env` cannot set or override any
  EXAONE variable.
- `binding` unchanged in shape; the key is never in `public_dict()`.

Without a profile (legacy path): the child inherits the host environment as is
(`dict(os.environ)`; unlike `build_env` it never adds a token via
`gh auth token`), so a host `EXAONE_API_KEY`, `EXAONE_BASE_URL` and
`~/.exaone-cli` are used — the same model as the other legacy providers, and
documented. A direct `provider.invoke()` outside a profile-bound client
therefore uses host auth.

Under a profile, `EXAONE_BASE_URL` cannot be set (the `EXAONE_` prefix guard
blocks it in `extra_env`), so profiles always use the default endpoint. An
explicit `exaone_base_url` field is a possible later extension.

Per-call API key: not offered in this version.

## 8. Error mapping

exaone-cli returns JSON-RPC errors whose messages start with fixed prefixes
(`authentication required/failed`, `rate limit exceeded (HTTP 429)`,
`timeout:`, `network error:`). The existing `classify_error` maps them to
`auth`, `usage_limit`, `timeout`, `network` without new code. A test pins this
contract with each prefix.

`session/load` failure → existing one-shot recovery to `session/new` (inherited
from the Kiro driver).

## 9. Docs

- New section "Unofficial providers" in `README.md` / `README.ko.md`: what
  exaone-cli is, install, opt-in registration, profile fields, limits
  (no tools, no per-call key).
- Capability table: add an `exaone (unofficial)` column.
- `docs/service-auth.md` / `.ko.md`: `exaone_api_key`, `EXAONE_HOME`.
- CHANGELOG entry. Version chosen by the user.

## 10. Testing

Offline:

- Refactor guard: existing Kiro tests unchanged and green.
- `tests/test_exaone_provider.py` with a fake `exaone` launcher
  (`write_fake_cli_launcher`) implementing the ACP subset: new session, resume
  via `session/load`, stale-ID recovery, thought/text chunk order, usage,
  stop reason, command line contains `--model`/`--reasoning-effort` with the
  mapped value, `thinking` rejected, each error prefix → `error_type`,
  binary missing, not in default registry.
- Auth profile: env contains only the profile key and `EXAONE_HOME`; host
  `EXAONE_API_KEY` / `EXAONE_BASE_URL` not leaked; `extra_env` rejects
  `EXAONE_*`; disabled provider; missing key.

Live (after exaone-cli stage 1 exists; with the user's key and approval): one
turn + one resume through `LLMClient.chat(provider="exaone")`.

## 11. Order of work

1. exaone-cli stage 1 (separate repo) — this provider depends on its ACP
   behavior.
2. ACP base extraction (Kiro-only refactor, tests green).
3. ExaoneProvider + reasoning map + auth profile + docs.
