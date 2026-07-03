# Normalized Reasoning Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two independent, normalized reasoning controls — `effort` (input dial) and `thinking` (output visibility) — across `claude`/`codex`/`copilot`, reachable per-call with clamp-and-report semantics.

**Architecture:** A zero-dep `agentcli/reasoning.py` owns canonical scales + per-provider native maps (explicit `(native, clamped)` tuples) and pure resolver functions. Each provider gains a `_reasoning_flags()` helper that appends its native flags and returns a `ReasoningResolution`; the resolution is attached to `LLMResponse.reasoning` (non-streaming) or emitted as one `event` chunk (streaming). `LLMClient.chat*` expose first-class `effort=`/`thinking=` kwargs threaded through the existing `_*_with_alias` helpers.

**Tech Stack:** Python 3.11+, stdlib only (`dataclasses`, `inspect`). pytest for tests.

## Global Constraints

- **Zero runtime dependencies.** Nothing added to `[project.dependencies]`. `reasoning.py` is stdlib-only.
- **Three-provider parity.** `claude`/`codex`/`copilot` all gain both controls. `kiro` is out of scope (ACP transport; reports empty capability, accepts no reasoning kwargs).
- **Streaming chunk contract is fixed.** Reasoning reporting reuses the existing `event` chunk type — no new chunk types.
- **Session = source of truth.** No change to session handling; reasoning flags are per-call CLI args only.
- **Paired docs.** Every `.md` doc change is mirrored in its `.ko.md`.
- **Canonical scales (verbatim):** `EFFORT = ("minimal","low","medium","high","xhigh","max")`, `THINKING = ("off","concise","detailed")`.
- **Backward compatibility:** with `effort=None` and `thinking=None`, argv and responses are byte-identical to today (`reasoning=None`).

---

## File Structure

- `agentcli/reasoning.py` — **new.** Canonical scales, per-provider maps, `LevelResolution`/`ReasoningResolution` dataclasses, `resolve_effort`/`resolve_thinking`, `effort_levels`/`thinking_levels`. Single source of truth.
- `agentcli/types.py` — **modify.** Add `LLMResponse.reasoning` field.
- `agentcli/providers/base.py` — **modify.** `capabilities()` populates `effort_levels`/`thinking_levels`; `ProviderCapabilities` gains those fields + `to_dict` entries.
- `agentcli/providers/claude.py` — **modify.** `_reasoning_flags`, constructor `effort`/`thinking`, per-call params, `_build_cmd` hook (replaces the uncommitted constructor-only `--effort` block).
- `agentcli/providers/copilot.py` — **modify.** Same wiring; thinking = boolean `--enable-reasoning-summaries`.
- `agentcli/providers/codex.py` — **modify.** Same wiring; flags via `-c model_reasoning_effort=` / `-c model_reasoning_summary=`.
- `agentcli/client.py` — **modify.** First-class `effort`/`thinking` on `chat`/`chat_async`/`chat_stream`; thread through `_invoke_with_alias`/`_invoke_async_with_alias`/`_stream_with_alias`; double-specify guard.
- `tests/test_reasoning.py` — **new.** Unit tests for the module.
- `tests/test_*_provider.py`, `tests/test_capabilities.py`, `tests/test_client_reasoning.py` — **modify/new.** Wiring + capability + client tests.
- `README.md` / `README.ko.md`, `CHANGELOG.md` — **modify.** Docs + drift fix.

---

### Task 1: `agentcli/reasoning.py` — canonical scales, maps, resolvers

**Files:**
- Create: `agentcli/reasoning.py`
- Test: `tests/test_reasoning.py`

**Interfaces:**
- Produces:
  - `EFFORT: tuple[str,...]`, `THINKING: tuple[str,...]`
  - `@dataclass(frozen=True) LevelResolution(requested: str, applied: str, clamped: bool, supported: bool)`
  - `@dataclass ReasoningResolution(effort: LevelResolution | None = None, thinking: LevelResolution | None = None)`
  - `resolve_effort(provider_id: str, canonical: str) -> LevelResolution`
  - `resolve_thinking(provider_id: str, canonical: str) -> LevelResolution`
  - `effort_levels(provider_id: str) -> frozenset[str]` (canonical levels the provider distinguishes, i.e. supported & not clamped)
  - `thinking_levels(provider_id: str) -> frozenset[str]`

- [ ] **Step 1: Discard the stale uncommitted constructor-only effort change**

The working tree has an incomplete constructor-only `--effort` change in `claude.py`. This plan supersedes it; start clean.

Run:
```bash
git restore agentcli/providers/claude.py
git status --short   # expect: clean (no M agentcli/providers/claude.py)
```
Expected: no modified `claude.py`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_reasoning.py`:
```python
import pytest
from agentcli.reasoning import (
    EFFORT, THINKING, LevelResolution,
    resolve_effort, resolve_thinking, effort_levels, thinking_levels,
)

def test_scales_are_ordered_low_to_high():
    assert EFFORT == ("minimal", "low", "medium", "high", "xhigh", "max")
    assert THINKING == ("off", "concise", "detailed")

def test_unknown_effort_raises_valueerror_listing_valid():
    with pytest.raises(ValueError) as e:
        resolve_effort("claude", "ultra")
    assert "ultra" in str(e.value)
    assert "minimal" in str(e.value)  # lists valid levels

@pytest.mark.parametrize("prov,canon,applied,clamped", [
    ("claude",  "minimal", "low",    True),   # claude has no sub-low -> clamp up
    ("claude",  "high",    "high",   False),
    ("claude",  "max",     "max",    False),
    ("copilot", "minimal", "none",   False),  # rename, same rank -> not clamped
    ("copilot", "max",     "max",    False),
    ("codex",   "minimal", "minimal", False),
    ("codex",   "high",    "high",   False),
    ("codex",   "xhigh",   "high",   True),   # codex caps at high -> clamp down
    ("codex",   "max",     "high",   True),
])
def test_resolve_effort_mapping(prov, canon, applied, clamped):
    r = resolve_effort(prov, canon)
    assert r == LevelResolution(requested=canon, applied=applied,
                                clamped=clamped, supported=True)

@pytest.mark.parametrize("prov,canon,applied,clamped,supported", [
    ("claude",  "concise",  "",         False, False),  # no toggle -> unsupported no-op
    ("claude",  "detailed", "",         False, False),
    ("copilot", "off",      "",         False, True),
    ("copilot", "concise",  "on",       False, True),
    ("copilot", "detailed", "on",       True,  True),   # boolean collapse -> clamp
    ("codex",   "off",      "none",     False, True),
    ("codex",   "detailed", "detailed", False, True),
])
def test_resolve_thinking_mapping(prov, canon, applied, clamped, supported):
    r = resolve_thinking(prov, canon)
    assert r == LevelResolution(requested=canon, applied=applied,
                                clamped=clamped, supported=supported)

def test_level_queries():
    assert effort_levels("claude") == frozenset({"low","medium","high","xhigh","max"})
    assert effort_levels("codex") == frozenset({"minimal","low","medium","high"})
    assert thinking_levels("claude") == frozenset()          # unsupported
    assert thinking_levels("copilot") == frozenset({"off","concise"})  # detailed clamps
    assert thinking_levels("codex") == frozenset({"off","concise","detailed"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `. .venv/bin/activate && pytest tests/test_reasoning.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentcli.reasoning'`.

- [ ] **Step 4: Write the module**

Create `agentcli/reasoning.py`:
```python
"""정규화된 reasoning 제어 — effort(입력 다이얼) + thinking(출력 가시성).

두 축은 독립이며 각 provider 의 native 표현으로 매핑된다. provider 가 요청
rank 를 표현 못 하면 최근접으로 clamp 하고 그 사실을 보고한다(silent 금지).
런타임 의존성 0 — 순수 stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass

# 낮은 → 높은 순서.
EFFORT = ("minimal", "low", "medium", "high", "xhigh", "max")
THINKING = ("off", "concise", "detailed")

# canonical -> (native, clamped). clamped=True 는 provider 가 요청 rank 를
# 표현 못 해 최근접으로 떨어졌음을 뜻한다. 같은 rank 의 rename 은 clamped=False.
_EFFORT_MAP = {
    "claude":  {"minimal": ("low", True), "low": ("low", False),
                "medium": ("medium", False), "high": ("high", False),
                "xhigh": ("xhigh", False), "max": ("max", False)},
    "copilot": {"minimal": ("none", False), "low": ("low", False),
                "medium": ("medium", False), "high": ("high", False),
                "xhigh": ("xhigh", False), "max": ("max", False)},
    "codex":   {"minimal": ("minimal", False), "low": ("low", False),
                "medium": ("medium", False), "high": ("high", False),
                "xhigh": ("high", True), "max": ("high", True)},
}
# copilot thinking 은 불리언 — "on" 은 --enable-reasoning-summaries 를 뜻하는
# sentinel(값 없는 플래그). claude 는 토글이 없어 표가 비어 있다(미지원).
_THINKING_MAP = {
    "claude":  {},
    "copilot": {"off": ("", False), "concise": ("on", False),
                "detailed": ("on", True)},
    "codex":   {"off": ("none", False), "concise": ("concise", False),
                "detailed": ("detailed", False)},
}


@dataclass(frozen=True)
class LevelResolution:
    requested: str        # 호출자가 넘긴 canonical 값
    applied: str          # CLI 에 방출한 native 값 ("" = 미지원/무플래그)
    clamped: bool         # 유효 rank 가 바뀌었나 (rename 은 False)
    supported: bool       # 이 provider 가 이 제어를 갖고 있나


@dataclass
class ReasoningResolution:
    effort: LevelResolution | None = None
    thinking: LevelResolution | None = None


def _resolve(scale, table, provider_id, value):
    if value not in scale:
        raise ValueError(
            f"unknown level {value!r}; valid: {', '.join(scale)}")
    pmap = table.get(provider_id)
    if not pmap:
        return LevelResolution(value, "", clamped=False, supported=False)
    native, clamped = pmap[value]
    return LevelResolution(value, native, clamped=clamped, supported=True)


def _levels(scale, table, provider_id):
    pmap = table.get(provider_id) or {}
    return frozenset(c for c in scale if c in pmap and not pmap[c][1])


def resolve_effort(provider_id, value):
    return _resolve(EFFORT, _EFFORT_MAP, provider_id, value)


def resolve_thinking(provider_id, value):
    return _resolve(THINKING, _THINKING_MAP, provider_id, value)


def effort_levels(provider_id):
    return _levels(EFFORT, _EFFORT_MAP, provider_id)


def thinking_levels(provider_id):
    return _levels(THINKING, _THINKING_MAP, provider_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest tests/test_reasoning.py -q`
Expected: PASS (all parametrized cases).

- [ ] **Step 6: Commit**

```bash
git add agentcli/reasoning.py tests/test_reasoning.py
git commit -m "feat(reasoning): normalized effort/thinking scales + resolvers (zero-dep)"
```

---

### Task 2: `LLMResponse.reasoning` field

**Files:**
- Modify: `agentcli/types.py` (`LLMResponse`, class at line 35)
- Test: `tests/test_reasoning.py` (append)

**Interfaces:**
- Consumes: `ReasoningResolution` from `agentcli.reasoning` (Task 1).
- Produces: `LLMResponse.reasoning: ReasoningResolution | None = None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reasoning.py`:
```python
def test_llmresponse_has_reasoning_field_defaulting_none():
    from agentcli.types import LLMResponse
    r = LLMResponse(content="hi", provider="claude", model="sonnet")
    assert r.reasoning is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest tests/test_reasoning.py::test_llmresponse_has_reasoning_field_defaulting_none -q`
Expected: FAIL with `TypeError` or `AttributeError` (no `reasoning` attribute).

- [ ] **Step 3: Add the field**

In `agentcli/types.py`, inside the `LLMResponse` dataclass (after `suggested_action: str = ""`), add:
```python
    # 정규화 reasoning 제어(effort/thinking)의 요청/적용 결과. 둘 다 미사용이면 None.
    reasoning: "ReasoningResolution | None" = None
```
At the top of `types.py`, add the import (near the other imports):
```python
from .reasoning import ReasoningResolution
```
(No circular import: `reasoning.py` imports only stdlib.)

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest tests/test_reasoning.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentcli/types.py tests/test_reasoning.py
git commit -m "feat(types): LLMResponse.reasoning for reasoning-control reporting"
```

---

### Task 3: `ProviderCapabilities` effort/thinking levels + `capabilities()` population

**Files:**
- Modify: `agentcli/types.py` (`ProviderCapabilities`, class at line 199, and `to_dict`)
- Modify: `agentcli/providers/base.py` (`capabilities()`, line 231)
- Test: `tests/test_capabilities.py`

**Interfaces:**
- Consumes: `effort_levels`/`thinking_levels` from `agentcli.reasoning` (Task 1).
- Produces: `ProviderCapabilities.effort_levels: frozenset`, `.thinking_levels: frozenset`; both surfaced in `to_dict()` and therefore in `LLMClient.capability_matrix()` (client.py:995, which calls `to_dict()`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_capabilities.py`:
```python
def test_capabilities_expose_reasoning_levels():
    from agentcli.providers.claude import ClaudeProvider
    from agentcli.providers.codex import CodexProvider
    caps_claude = ClaudeProvider().capabilities()
    caps_codex = CodexProvider().capabilities()
    assert caps_claude.effort_levels == frozenset({"low","medium","high","xhigh","max"})
    assert caps_claude.thinking_levels == frozenset()          # claude: no toggle
    assert caps_codex.effort_levels == frozenset({"minimal","low","medium","high"})
    assert "effort_levels" in caps_claude.to_dict()
    assert "thinking_levels" in caps_claude.to_dict()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest tests/test_capabilities.py::test_capabilities_expose_reasoning_levels -q`
Expected: FAIL (`TypeError: __init__() missing` or `AttributeError: effort_levels`).

- [ ] **Step 3: Add fields to `ProviderCapabilities`**

In `agentcli/types.py`, inside `ProviderCapabilities` (after `debug: bool = False`), add:
```python
    effort_levels: frozenset = frozenset()     # 지원하는 canonical effort 레벨
    thinking_levels: frozenset = frozenset()   # 지원하는 canonical thinking 레벨
```
In `ProviderCapabilities.to_dict`, add to the returned dict:
```python
            "effort_levels": sorted(self.effort_levels),
            "thinking_levels": sorted(self.thinking_levels),
```

- [ ] **Step 4: Populate in `capabilities()`**

In `agentcli/providers/base.py`, inside `capabilities()` (line 231), import and pass the levels. At the top of the method body add:
```python
        from ..reasoning import effort_levels, thinking_levels
```
and in the `ProviderCapabilities(...)` constructor call add:
```python
            effort_levels=effort_levels(self.provider_id),
            thinking_levels=thinking_levels(self.provider_id),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest tests/test_capabilities.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agentcli/types.py agentcli/providers/base.py tests/test_capabilities.py
git commit -m "feat(capabilities): expose per-provider effort/thinking levels"
```

---

### Task 4: ClaudeProvider wiring

**Files:**
- Modify: `agentcli/providers/claude.py` (`__init__` line 62, `_build_cmd` line 202, `invoke` 286, `invoke_async` 386, `stream_async` 478)
- Test: `tests/test_claude_provider.py`

**Interfaces:**
- Consumes: `resolve_effort`, `resolve_thinking`, `ReasoningResolution` from `agentcli.reasoning`.
- Produces: `ClaudeProvider._reasoning_flags(effort, thinking) -> tuple[list[str], ReasoningResolution | None]`; `effort`/`thinking` kwargs on `__init__`/`invoke`/`invoke_async`/`stream_async`; `_build_cmd(..., reasoning_args=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claude_provider.py`:
```python
def test_claude_effort_flag_and_report():
    p = ClaudeProvider()
    args, res = p._reasoning_flags("high", None)
    assert args == ["--effort", "high"]
    assert res.effort.applied == "high" and res.effort.clamped is False

def test_claude_effort_minimal_clamps_up_to_low():
    args, res = ClaudeProvider()._reasoning_flags("minimal", None)
    assert args == ["--effort", "low"]
    assert res.effort.clamped is True

def test_claude_thinking_is_unsupported_noop():
    args, res = ClaudeProvider()._reasoning_flags(None, "detailed")
    assert args == []                      # no flag emitted
    assert res.thinking.supported is False and res.thinking.applied == ""

def test_claude_percall_effort_overrides_constructor_default():
    p = ClaudeProvider(effort="low")
    args, _ = p._reasoning_flags("max", None)   # per-call wins
    assert args == ["--effort", "max"]

def test_claude_no_reasoning_means_no_args_and_none():
    args, res = ClaudeProvider()._reasoning_flags(None, None)
    assert args == [] and res is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest tests/test_claude_provider.py -k reasoning -q`
Expected: FAIL with `AttributeError: 'ClaudeProvider' object has no attribute '_reasoning_flags'`.

- [ ] **Step 3: Add constructor params**

In `agentcli/providers/claude.py` `__init__` (line 62), add to the signature (after `partial_messages: bool = False`):
```python
                 effort: str | None = None,
                 thinking: str | None = None,
```
and in the body (after `self._partial_messages = partial_messages`):
```python
        # 정규화 reasoning 제어의 생성자 기본값(호출 시 override 가능).
        self._effort = effort
        self._thinking = thinking
```

- [ ] **Step 4: Add `_reasoning_flags` helper**

Add this method to `ClaudeProvider` (e.g. just after `_build_cmd`):
```python
    def _reasoning_flags(self, effort, thinking):
        """유효 effort/thinking → (claude native 플래그 args, ReasoningResolution).

        유효값 = 호출 인자(None 이 아니면) 우선, 아니면 생성자 기본값.
        claude 는 thinking 토글이 없어 thinking 은 무플래그 no-op 로 보고된다.
        """
        from ..reasoning import resolve_effort, resolve_thinking
        from ..types import ReasoningResolution
        eff = self._effort if effort is None else effort
        thk = self._thinking if thinking is None else thinking
        args, er, tr = [], None, None
        if eff:
            er = resolve_effort(self.provider_id, eff)
            if er.applied:
                args += ["--effort", er.applied]
        if thk:
            tr = resolve_thinking(self.provider_id, thk)  # 미지원 → 무플래그
        res = ReasoningResolution(effort=er, thinking=tr) if (er or tr) else None
        return args, res
```

- [ ] **Step 5: Replace the old effort block in `_build_cmd`**

In `_build_cmd` (line 202), change the signature to accept pre-built reasoning args — add to the keyword params:
```python
                   reasoning_args: list[str] | None = None,
```
Then **remove** the stale block (if present after `git restore` it is already gone):
```python
        if self._effort:
            cmd += ["--effort", self._effort]
```
and instead, immediately after the `if model:` block, insert:
```python
        if reasoning_args:
            cmd += reasoning_args
```

- [ ] **Step 6: Thread through `invoke` / `invoke_async` / `stream_async`**

For each of `invoke` (286), `invoke_async` (386), `stream_async` (478): add to the signature:
```python
               effort: str | None = None,
               thinking: str | None = None,
```
At the start of each body (before the `_build_cmd` call), add:
```python
        reasoning_args, reasoning = self._reasoning_flags(effort, thinking)
```
Pass `reasoning_args=reasoning_args` into the `_build_cmd(...)` call. Then:
- In `invoke`/`invoke_async`: after the `LLMResponse` is constructed and before returning, set `resp.reasoning = reasoning` (attach to every return path that produced output). For the binary-missing early return, leave `reasoning` unset (None).
- In `stream_async`: immediately after resolving, if `reasoning` is not None and either control clamped/unsupported, yield the event first:
```python
        if reasoning and _reasoning_needs_event(reasoning):
            yield StreamChunk(type="event",
                              data={"reasoning": _reasoning_to_dict(reasoning)})
```

- [ ] **Step 7: Add the two small reporting helpers to `agentcli/reasoning.py`**

Append to `agentcli/reasoning.py`:
```python
def needs_event(res) -> bool:
    """clamp 또는 미지원이 하나라도 있으면 스트리밍 event 로 알린다."""
    for lr in (res.effort, res.thinking):
        if lr is not None and (lr.clamped or not lr.supported):
            return True
    return False


def to_dict(res) -> dict:
    def _one(lr):
        return None if lr is None else {
            "requested": lr.requested, "applied": lr.applied,
            "clamped": lr.clamped, "supported": lr.supported}
    return {"effort": _one(res.effort), "thinking": _one(res.thinking)}
```
In `claude.py`, import at the call site: `from ..reasoning import needs_event as _reasoning_needs_event, to_dict as _reasoning_to_dict` (top-of-file import). Ensure `StreamChunk` is imported (it already is for streaming).

- [ ] **Step 8: Write a command-integration test**

Append to `tests/test_claude_provider.py`:
```python
def test_claude_build_cmd_includes_effort_via_reasoning_args():
    p = ClaudeProvider()
    args, _ = p._reasoning_flags("xhigh", None)
    cmd, _sid = p._build_cmd("hi", "", "", reasoning_args=args)
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "xhigh"
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest tests/test_claude_provider.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add agentcli/providers/claude.py agentcli/reasoning.py tests/test_claude_provider.py
git commit -m "feat(claude): per-call effort (thinking no-op) with clamp reporting"
```

---

### Task 5: CopilotProvider wiring

**Files:**
- Modify: `agentcli/providers/copilot.py` (`__init__` 73, `_build_cmd` 151, `invoke` 202, `invoke_async` 278, `stream_async` 355)
- Test: `tests/test_copilot_provider.py`

**Interfaces:**
- Consumes: `resolve_effort`, `resolve_thinking`, `needs_event`, `to_dict` from `agentcli.reasoning`.
- Produces: `CopilotProvider._reasoning_flags(effort, thinking) -> tuple[list[str], ReasoningResolution | None]`; `effort`/`thinking` on `__init__`/`invoke`/`invoke_async`/`stream_async`; `_build_cmd(..., reasoning_args=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_copilot_provider.py`:
```python
def test_copilot_effort_flag():
    args, res = CopilotProvider()._reasoning_flags("medium", None)
    assert args == ["--effort", "medium"] and res.effort.applied == "medium"

def test_copilot_effort_minimal_renames_to_none_not_clamped():
    args, res = CopilotProvider()._reasoning_flags("minimal", None)
    assert args == ["--effort", "none"] and res.effort.clamped is False

def test_copilot_thinking_concise_enables_summaries():
    args, res = CopilotProvider()._reasoning_flags(None, "concise")
    assert args == ["--enable-reasoning-summaries"]
    assert res.thinking.applied == "on" and res.thinking.clamped is False

def test_copilot_thinking_detailed_clamps_to_boolean():
    args, res = CopilotProvider()._reasoning_flags(None, "detailed")
    assert args == ["--enable-reasoning-summaries"]
    assert res.thinking.clamped is True

def test_copilot_thinking_off_emits_no_flag():
    args, res = CopilotProvider()._reasoning_flags(None, "off")
    assert args == [] and res.thinking.applied == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest tests/test_copilot_provider.py -k reasoning -q`
Expected: FAIL (`AttributeError: _reasoning_flags`).

- [ ] **Step 3: Add constructor params**

In `copilot.py` `__init__` (73), replace the stale committed `effort: str | None = None` / `self._effort = effort` handling with both controls. After `self._add_dirs = add_dirs or []`, ensure:
```python
        self._effort = effort
        self._thinking = thinking
```
and add `thinking: str | None = None` to the signature next to the existing `effort`.

- [ ] **Step 4: Add `_reasoning_flags`**

Add to `CopilotProvider`:
```python
    def _reasoning_flags(self, effort, thinking):
        """유효 effort/thinking → (copilot native 플래그, ReasoningResolution).

        thinking 은 불리언 — concise/detailed 는 --enable-reasoning-summaries,
        off 는 무플래그. detailed 는 불리언으로 접혀 clamped 로 보고된다.
        """
        from ..reasoning import resolve_effort, resolve_thinking
        from ..types import ReasoningResolution
        eff = self._effort if effort is None else effort
        thk = self._thinking if thinking is None else thinking
        args, er, tr = [], None, None
        if eff:
            er = resolve_effort(self.provider_id, eff)
            if er.applied:
                args += ["--effort", er.applied]
        if thk:
            tr = resolve_thinking(self.provider_id, thk)
            if tr.applied == "on":
                args.append("--enable-reasoning-summaries")
        res = ReasoningResolution(effort=er, thinking=tr) if (er or tr) else None
        return args, res
```

- [ ] **Step 5: `_build_cmd` hook**

In `_build_cmd` (151), add `reasoning_args: list[str] | None = None` to the signature. **Remove** the existing:
```python
        if self._effort:
            cmd += ["--effort", self._effort]
```
and after the `--add-dir` loop insert:
```python
        if reasoning_args:
            cmd += reasoning_args
```

- [ ] **Step 6: Thread through `invoke`/`invoke_async`/`stream_async`**

Same shape as Task 4 Step 6. Add `effort`/`thinking` params; at body start:
```python
        reasoning_args, reasoning = self._reasoning_flags(effort, thinking)
```
Pass `reasoning_args=reasoning_args` into the `self._build_cmd(...)` call (note copilot's `_build_cmd` also takes `alias`, `resume_by_alias`). Attach `resp.reasoning = reasoning` on output-producing returns; in `stream_async` yield the event when `needs_event(reasoning)`:
```python
        from ..reasoning import needs_event, to_dict as _rz_to_dict
        if reasoning and needs_event(reasoning):
            yield StreamChunk(type="event", data={"reasoning": _rz_to_dict(reasoning)})
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest tests/test_copilot_provider.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agentcli/providers/copilot.py tests/test_copilot_provider.py
git commit -m "feat(copilot): per-call effort + thinking summaries with clamp reporting"
```

---

### Task 6: CodexProvider wiring

**Files:**
- Modify: `agentcli/providers/codex.py` (`__init__` for `class CodexProvider` at 108, `_build_cmd` 208 + retry/resume branches, `invoke` ~272, `invoke_async` ~390, `stream_async` ~476)
- Test: `tests/test_codex_provider.py`

**Interfaces:**
- Consumes: `resolve_effort`, `resolve_thinking`, `needs_event`, `to_dict`, and the existing `_toml_inline` (codex.py:89).
- Produces: `CodexProvider._reasoning_flags(effort, thinking) -> tuple[list[str], ReasoningResolution | None]`; `effort`/`thinking` on `__init__`/`invoke`/`invoke_async`/`stream_async`; `_build_cmd(..., reasoning_args=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_provider.py`:
```python
def test_codex_effort_via_config_flag():
    args, res = CodexProvider()._reasoning_flags("high", None)
    assert args == ["-c", 'model_reasoning_effort="high"']
    assert res.effort.applied == "high"

def test_codex_effort_max_clamps_to_high():
    args, res = CodexProvider()._reasoning_flags("max", None)
    assert args == ["-c", 'model_reasoning_effort="high"']
    assert res.effort.clamped is True

def test_codex_thinking_detailed_via_config():
    args, res = CodexProvider()._reasoning_flags(None, "detailed")
    assert args == ["-c", 'model_reasoning_summary="detailed"']
    assert res.thinking.applied == "detailed"

def test_codex_thinking_off_maps_to_none():
    args, _ = CodexProvider()._reasoning_flags(None, "off")
    assert args == ["-c", 'model_reasoning_summary="none"']
```
(`_toml_inline("high")` yields `"high"` with quotes — confirm the exact serialization in Step 2 and adjust the expected strings to match `_toml_inline`.)

- [ ] **Step 2: Confirm `_toml_inline` string form**

Run:
```bash
. .venv/bin/activate && python -c "from agentcli.providers.codex import _toml_inline; print(_toml_inline('high'))"
```
Expected: a TOML string literal (e.g. `"high"`). If the output differs from the test's expected `'model_reasoning_effort="high"'`, update the test expectations in Step 1 to the exact form before proceeding.

- [ ] **Step 3: Add constructor params**

In `CodexProvider.__init__`, add `effort: str | None = None`, `thinking: str | None = None` to the signature and store:
```python
        self._effort = effort
        self._thinking = thinking
```

- [ ] **Step 4: Add `_reasoning_flags`**

Add to `CodexProvider`:
```python
    def _reasoning_flags(self, effort, thinking):
        """유효 effort/thinking → (codex `-c` config args, ReasoningResolution).

        codex 는 --effort 플래그가 없어 config override 로 전달한다:
        `-c model_reasoning_effort=<native>` / `-c model_reasoning_summary=<native>`.
        xhigh/max 는 codex 상한(high)으로 clamp 되어 보고된다.
        """
        from ..reasoning import resolve_effort, resolve_thinking
        from ..types import ReasoningResolution
        eff = self._effort if effort is None else effort
        thk = self._thinking if thinking is None else thinking
        args, er, tr = [], None, None
        if eff:
            er = resolve_effort(self.provider_id, eff)
            if er.applied:
                args += ["-c", f"model_reasoning_effort={_toml_inline(er.applied)}"]
        if thk:
            tr = resolve_thinking(self.provider_id, thk)
            if tr.applied:
                args += ["-c", f"model_reasoning_summary={_toml_inline(tr.applied)}"]
        res = ReasoningResolution(effort=er, thinking=tr) if (er or tr) else None
        return args, res
```

- [ ] **Step 5: `_build_cmd` hook (both branches)**

In `_build_cmd` (208), add `reasoning_args: list[str] | None = None` to the signature. In **both** the resume branch and the new-session branch, append the reasoning args right after `cmd += mcp_args` (before the `cmd += ["--", ...]` terminator):
```python
            if reasoning_args:
                cmd += reasoning_args
```
(Two insertion points — resume branch near line 247 and new-session branch near line 266.)

- [ ] **Step 6: Thread through public methods**

In `invoke`/`invoke_async`/`stream_async` (and codex's one-time resume retry path), add `effort`/`thinking` params; compute `reasoning_args, reasoning = self._reasoning_flags(effort, thinking)` once; pass `reasoning_args=reasoning_args` into every `_build_cmd(...)` call (including the retry). Attach `resp.reasoning = reasoning` on output returns; `stream_async` yields the event when `needs_event(reasoning)` (same code as Task 5 Step 6).

- [ ] **Step 7: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest tests/test_codex_provider.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agentcli/providers/codex.py tests/test_codex_provider.py
git commit -m "feat(codex): per-call effort/thinking via -c config with clamp reporting"
```

---

### Task 7: LLMClient first-class `effort`/`thinking` kwargs

**Files:**
- Modify: `agentcli/client.py` (`_invoke_with_alias` 127, `_invoke_async_with_alias` 141, `_stream_with_alias` 156, `chat` 468, `chat_async` 595, `chat_stream` 712)
- Test: `tests/test_client_reasoning.py` (new)

**Interfaces:**
- Consumes: providers now accept `effort`/`thinking` (Tasks 4-6); `_supported_kwargs` (client.py:108) drops them for providers that don't (kiro).
- Produces: `chat`/`chat_async`/`chat_stream` accept `effort: str | None = None`, `thinking: str | None = None`; ambiguous double-specify raises `ValueError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_client_reasoning.py`:
```python
import pytest
from agentcli import LLMClient, MemoryStore

def test_double_specify_effort_raises():
    c = LLMClient(store=MemoryStore())
    with pytest.raises(ValueError):
        # effort as first-class kwarg AND inside provider_options -> ambiguous
        c.chat("hi", provider="claude", effort="high",
               provider_options={"effort": "low"})

def test_effort_threads_into_supported_kwargs():
    # _supported_kwargs must keep 'effort' for a provider that accepts it
    from agentcli.client import _supported_kwargs
    from agentcli.providers.claude import ClaudeProvider
    kw = _supported_kwargs(ClaudeProvider(), "invoke",
                           {"model": "", "effort": "high", "thinking": None})
    assert kw.get("effort") == "high"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `. .venv/bin/activate && pytest tests/test_client_reasoning.py -q`
Expected: FAIL (`chat()` has no `effort` kwarg → `TypeError`; or no `ValueError` raised).

- [ ] **Step 3: Add the conflict guard helper**

In `agentcli/client.py` (module level, near `_merge_options` at line 116), add:
```python
def _reject_reasoning_conflict(effort, thinking, provider_options):
    """effort/thinking 를 1급 kwarg 와 provider_options 양쪽에 주면 모호 → 거부."""
    if not provider_options:
        return
    for name, val in (("effort", effort), ("thinking", thinking)):
        if val is not None and name in provider_options:
            raise ValueError(
                f"{name} given both as a keyword and in provider_options; "
                f"pass it once")
```

- [ ] **Step 4: Thread through the `_*_with_alias` helpers**

For `_invoke_with_alias` (127), `_invoke_async_with_alias` (141), `_stream_with_alias` (156): add `effort=None, thinking=None` to each signature, and add to the base dict passed to `_merge_options`:
```python
        "effort": effort,
        "thinking": thinking,
```
(`_supported_kwargs` then keeps them only for providers whose `invoke`/`stream_async` accept them.)

- [ ] **Step 5: Add kwargs to `chat`/`chat_async`/`chat_stream` and pass down**

For each of `chat` (468), `chat_async` (595), `chat_stream` (712): add to the signature:
```python
                 effort: str | None = None,
                 thinking: str | None = None,
```
Immediately after the signature/docstring, call the guard:
```python
        _reject_reasoning_conflict(effort, thinking, provider_options)
```
and pass `effort=effort, thinking=thinking` down to the `_invoke_with_alias` / `_invoke_async_with_alias` / `_stream_with_alias` call (and any internal `_invoke_with_fallback` path — thread `effort`/`thinking` through to the same helper call).

- [ ] **Step 6: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest tests/test_client_reasoning.py -q`
Expected: PASS.

- [ ] **Step 7: Run the full suite (integration guard)**

Run: `. .venv/bin/activate && pytest -q`
Expected: PASS (existing 668+ tests unchanged; new tests green).

- [ ] **Step 8: Commit**

```bash
git add agentcli/client.py tests/test_client_reasoning.py
git commit -m "feat(client): first-class effort/thinking kwargs with conflict guard"
```

---

### Task 8: Docs — README pair, CHANGELOG, drift fix

**Files:**
- Modify: `README.md`, `README.ko.md` (provider comparison + new "Reasoning controls" section)
- Modify: `CHANGELOG.md` (new entry + fix line 398 copilot-options drift)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add a "Reasoning controls" section to `README.md`**

After the provider comparison table, add:
```markdown
### Reasoning controls

Two independent, normalized controls, per call or as provider defaults:

- `effort` — how hard the model reasons: `minimal · low · medium · high · xhigh · max`.
- `thinking` — reasoning-output visibility: `off · concise · detailed`.

```python
resp = await client.chat_async("…", provider="codex",
                               effort="high", thinking="concise")
print(resp.reasoning.effort.applied)   # native level actually used
```

Unsupported levels clamp to the nearest and are reported (`resp.reasoning`,
and a streaming `event` chunk). Provider specifics: codex caps effort at
`high` (xhigh/max clamp); copilot thinking is boolean (`concise`/`detailed`
both enable summaries); claude has no thinking toggle (reported unsupported).
```

- [ ] **Step 2: Mirror into `README.ko.md`**

Add the equivalent Korean section (same code block; prose translated) at the matching location.

- [ ] **Step 3: Add CHANGELOG entry and fix the drift**

In `CHANGELOG.md`, add a new top entry:
```markdown
## 0.7.0 — unreleased

### Added
- **Normalized reasoning controls.** First-class `effort` (minimal…max) and
  `thinking` (off/concise/detailed) on `chat`/`chat_async`/`chat_stream` and on
  each provider, mapped per provider with clamp-and-report (`LLMResponse.reasoning`
  + a streaming `event` chunk). `capability_matrix()` gains `effort_levels` /
  `thinking_levels`. claude has no thinking toggle (reported unsupported); kiro
  out of scope.
```
Fix the pre-existing drift at line 398 (copilot options list) to match the
current `capabilities().options` (`debug`, `debug_log_path`, and now `effort`,
`thinking`) — remove the stale constructor-param entries wrongly listed as
per-call options.

- [ ] **Step 4: Verify docs build/parity**

Run: `. .venv/bin/activate && pytest -q && python -m build 1>/dev/null && python -m twine check dist/*`
Expected: tests PASS; build + twine check PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md README.ko.md CHANGELOG.md
git commit -m "docs: normalized reasoning controls + fix copilot options drift"
```

---

## Self-Review

**Spec coverage:**
- Two independent controls, canonical scales → Task 1. ✓
- Per-provider mapping + clamp (effort tables, thinking tables) → Task 1 maps; Tasks 4-6 flag shapes. ✓
- First-class per-call kwarg + registry reachability → Task 7 (+ provider params Tasks 4-6 auto-surface in `capabilities().options`). ✓
- Clamp reporting (metadata + event, no new chunk type) → Task 2 (`reasoning` field), Task 4 Step 7 (`needs_event`/`to_dict`, `event` chunk). ✓
- Capability exposure (`effort_levels`/`thinking_levels`, matrix) → Task 3. ✓
- claude thinking no-op, kiro out of scope → Task 4 Step 4 (unsupported), Task 1 empty maps. ✓
- Double-specify rejection → Task 7 Step 3. ✓
- Supersede uncommitted claude.py change → Task 1 Step 1. ✓
- Zero-dep, paired docs, drift fix → Task 1 (stdlib), Task 8. ✓
- Backward compat (no controls → identical argv, `reasoning=None`) → Task 4 Step 6 (early returns leave None), full-suite guard Task 7 Step 7. ✓

**Placeholder scan:** codex `-c` serialization is verified live in Task 6 Step 2 before asserting exact strings (the one spec "open item"). No TBD/TODO. Every code step shows code.

**Type consistency:** `_reasoning_flags(effort, thinking) -> (list[str], ReasoningResolution | None)` identical across Tasks 4-6; `reasoning_args` kwarg name consistent in every `_build_cmd`; `LevelResolution`/`ReasoningResolution`/`needs_event`/`to_dict` names consistent between Task 1 and consumers.

## Open items confirmed during implementation

- Task 6 Step 2 confirms `_toml_inline` output form for the codex `-c` assertions.
- codex `model_reasoning_effort` / `model_reasoning_summary` accepted values: the resolver only emits from the confirmed native sets; if the installed codex/model rejects a value, it surfaces via codex's own error path (unchanged).
