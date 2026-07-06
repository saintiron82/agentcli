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

def test_llmresponse_has_reasoning_field_defaulting_none():
    from agentcli.types import LLMResponse
    r = LLMResponse(content="hi", provider="claude", model="sonnet")
    assert r.reasoning is None


# ===== map completeness guard (merge-gate Fix E) =====
#
# A partial (non-empty but not-full-scale) provider map would make
# `_resolve`'s `pmap[value]` raise a bare KeyError at call time instead of
# failing loudly at import time. `_assert_maps_cover_full_scale` guards
# against that for the shipped maps (called at module load) -- this test
# re-runs the same guard against a deliberately partial copy to prove it
# actually raises.

def test_shipped_maps_pass_the_completeness_guard():
    from agentcli.reasoning import (
        _EFFORT_MAP, _THINKING_MAP, _assert_maps_cover_full_scale)
    # Must not raise for the maps actually shipped.
    _assert_maps_cover_full_scale(EFFORT, _EFFORT_MAP, "effort")
    _assert_maps_cover_full_scale(THINKING, _THINKING_MAP, "thinking")


def test_partial_map_fails_the_completeness_guard():
    from agentcli.reasoning import _assert_maps_cover_full_scale
    partial = {"claude": {"minimal": ("low", True), "low": ("low", False)}}
    with pytest.raises(AssertionError, match="missing levels"):
        _assert_maps_cover_full_scale(EFFORT, partial, "effort")
