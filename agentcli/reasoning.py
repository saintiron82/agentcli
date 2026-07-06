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


def _assert_maps_cover_full_scale(scale, table, label) -> None:
    """비어있지 않은 provider 항목은 scale 전체를 커버해야 한다.

    부분(partial) 맵을 그대로 두면 `_resolve`의 `pmap[value]`가 호출 시점에
    KeyError 로 터진다 — import 시점에 assert 로 걸러 "부분 맵 → 깨끗한
    실패"를 보장한다 (호출 시 KeyError 대신)."""
    full = set(scale)
    for provider_id, pmap in table.items():
        if not pmap:
            continue  # 완전 미지원 provider (예: claude thinking) 는 허용.
        missing = full - set(pmap)
        assert not missing, (
            f"{label} map for {provider_id!r} is missing levels {sorted(missing)}; "
            f"a non-empty provider map must cover the full scale {scale}")


_assert_maps_cover_full_scale(EFFORT, _EFFORT_MAP, "effort")
_assert_maps_cover_full_scale(THINKING, _THINKING_MAP, "thinking")


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
