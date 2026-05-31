"""3-provider normalization contract regression tests.

이 테스트는 `ClaudeProvider` / `CodexProvider` / `CopilotProvider` 의 정규화
계약을 잠근다. providers/ shared helper 추출 리팩토링 (issue #7) 중에도 이
invariant 가 깨지지 않아야 한다.

잠그는 계약:

- 동일 declarative attribute (`provider_id`, `supports_sessions`,
  `supports_streaming`)
- 동일 메서드 시그니처 (`invoke` / `invoke_async` / `stream_async` /
  `health_check` / `list_models`)
- `stream_async` 추가 keyword (`idle_timeout`, `wall_timeout`) 지원
- `list_models()` 결과는 `[{"id": str, "name": str, "aliases"?: [str]}, ...]`
- 빈 selector → `""` (기본 model), strict unknown → `ValueError`
- 바이너리 부재 시 동일 패턴: `health_check.status="binary_missing"`,
  `invoke` 는 빈 content + `exit_code=127`, `stream_async` 는 `error` chunk
- 모든 yield 되는 chunk 타입은 normalized set 안에 있다.
"""

import asyncio
import inspect

import pytest

from agentcli.providers.base import LLMProvider, build_session_prompt
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.types import LLMResponse, Message, ProviderHealth


PROVIDERS = [ClaudeProvider, CodexProvider, CopilotProvider]
PROVIDER_IDS = ["claude", "codex", "copilot"]

ALLOWED_CHUNK_TYPES = {
    "text", "thinking", "tool_use", "tool_result",
    "event", "error", "done",
}

COMMON_KWARGS = {"messages", "model", "timeout", "session_id", "cwd"}
STREAM_EXTRA_KWARGS = {"idle_timeout", "wall_timeout"}
HEALTH_KWARGS = {"timeout", "cwd", "probe"}


# ============================================================
# A. Declarative class attributes
# ============================================================

@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_provider_has_required_class_attributes(cls):
    """모든 provider 는 provider_id(non-empty str) + supports_sessions/streaming(bool)."""
    p = cls()
    assert isinstance(p.provider_id, str) and p.provider_id, (
        f"{cls.__name__}.provider_id must be a non-empty string")
    assert isinstance(p.supports_sessions, bool), (
        f"{cls.__name__}.supports_sessions must be bool")
    assert isinstance(p.supports_streaming, bool), (
        f"{cls.__name__}.supports_streaming must be bool")


def test_provider_ids_are_distinct_and_known():
    """3 provider 가 서로 다른 provider_id 를 가짐 (claude/codex/copilot)."""
    ids = {cls().provider_id for cls in PROVIDERS}
    assert ids == {"claude", "codex", "copilot"}


# ============================================================
# B. Method signature consistency
# ============================================================

@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_invoke_signature(cls):
    params = set(inspect.signature(cls.invoke).parameters)
    missing = COMMON_KWARGS - params
    assert not missing, f"{cls.__name__}.invoke missing kwargs: {missing}"


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_invoke_async_is_coroutine_and_signature(cls):
    method = cls.invoke_async
    assert inspect.iscoroutinefunction(method), (
        f"{cls.__name__}.invoke_async must be an async function")
    params = set(inspect.signature(method).parameters)
    missing = COMMON_KWARGS - params
    assert not missing, (
        f"{cls.__name__}.invoke_async missing kwargs: {missing}")


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_stream_async_is_async_generator_and_signature(cls):
    method = cls.stream_async
    assert inspect.isasyncgenfunction(method), (
        f"{cls.__name__}.stream_async must be an async generator function")
    params = set(inspect.signature(method).parameters)
    missing_common = COMMON_KWARGS - params
    assert not missing_common, (
        f"{cls.__name__}.stream_async missing common kwargs: {missing_common}")
    missing_stream = STREAM_EXTRA_KWARGS - params
    assert not missing_stream, (
        f"{cls.__name__}.stream_async missing stream kwargs: {missing_stream}")


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_health_check_signature(cls):
    params = set(inspect.signature(cls.health_check).parameters)
    missing = HEALTH_KWARGS - params
    assert not missing, (
        f"{cls.__name__}.health_check missing kwargs: {missing}")


# ============================================================
# C. list_models output shape
# ============================================================

@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_list_models_shape(cls):
    models = cls().list_models()
    assert isinstance(models, list) and models, (
        f"{cls.__name__}.list_models() must return a non-empty list")
    for m in models:
        assert isinstance(m, dict), f"{cls.__name__}: model entry must be dict"
        assert isinstance(m.get("id"), str), (
            f"{cls.__name__}: model 'id' must be a string")
        assert isinstance(m.get("name"), str) and m["name"], (
            f"{cls.__name__}: model 'name' must be a non-empty string")
        aliases = m.get("aliases")
        if aliases is not None:
            assert isinstance(aliases, list)
            assert all(isinstance(a, str) for a in aliases)


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_list_models_has_default_entry(cls):
    """첫 번째 항목은 빈 id 의 '기본' 모델 (사용자가 model 미지정 시 사용)."""
    models = cls().list_models()
    assert models[0]["id"] == "", (
        f"{cls.__name__}.list_models()[0] must have id='' (default model)")


# ============================================================
# D. resolve_model behavior
# ============================================================

@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_resolve_model_empty_selector_returns_empty(cls):
    """빈 selector → '' (기본 model)."""
    assert cls().resolve_model("") == ""


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_resolve_model_strict_unknown_raises(cls):
    """strict=True 에서 알 수 없는 selector 는 ValueError."""
    with pytest.raises(ValueError):
        cls().resolve_model("__nonexistent_model_xyz__", strict=True)


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_resolve_model_lenient_passes_unknown_through(cls):
    """strict=False (기본) 에서 알 수 없는 selector 는 그대로 통과."""
    assert cls().resolve_model("custom-future-model") == "custom-future-model"


# ============================================================
# E. Binary-missing path (CLI 부재 시 정규화된 실패)
# ============================================================

@pytest.fixture
def no_cli_binary(monkeypatch):
    """모든 `shutil.which` 호출이 None 을 반환하도록 monkeypatch."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_is_available_returns_bool(cls):
    assert isinstance(cls().is_available(), bool)


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_is_available_false_when_binary_missing(cls, no_cli_binary):
    assert cls().is_available() is False


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_health_check_binary_missing(cls, no_cli_binary):
    health = cls().health_check()
    assert isinstance(health, ProviderHealth)
    assert health.ok is False
    assert health.available is False
    assert health.status == "binary_missing"


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_invoke_binary_missing(cls, no_cli_binary):
    """바이너리 없을 때 invoke 는 빈 content + exit_code=127."""
    resp = cls().invoke([Message(role="user", content="ping")])
    assert isinstance(resp, LLMResponse)
    assert resp.content == ""
    assert resp.exit_code == 127
    assert resp.error  # non-empty error message


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_invoke_async_binary_missing(cls, no_cli_binary):
    """바이너리 없을 때 invoke_async 도 동일 정규화 실패."""
    resp = asyncio.run(
        cls().invoke_async([Message(role="user", content="ping")]))
    assert isinstance(resp, LLMResponse)
    assert resp.content == ""
    assert resp.exit_code == 127
    assert resp.error


@pytest.mark.parametrize("cls", PROVIDERS, ids=PROVIDER_IDS)
def test_stream_async_binary_missing_yields_error_chunk(cls, no_cli_binary):
    """바이너리 없을 때 stream_async 는 error chunk 를 yield 하고 종료."""

    async def collect():
        return [
            c async for c in cls().stream_async(
                [Message(role="user", content="ping")])
        ]

    chunks = asyncio.run(collect())
    assert chunks, "stream_async must yield at least one chunk on failure"
    for c in chunks:
        assert c.type in ALLOWED_CHUNK_TYPES, (
            f"{cls.__name__} yielded non-normalized chunk type: {c.type!r}")
    assert any(c.type == "error" for c in chunks), (
        f"{cls.__name__}.stream_async must yield an 'error' chunk "
        f"when the CLI binary is missing")


# ============================================================
# F. Shared helper usage (선언적 import 검증)
# ============================================================

def test_all_providers_share_build_session_prompt():
    """3 provider 모두 base.build_session_prompt 를 그대로 사용한다.

    리팩토링 중 누가 자기 버전을 만들면 이 invariant 가 깨진다.
    """
    import agentcli.providers.claude as claude_mod
    import agentcli.providers.codex as codex_mod
    import agentcli.providers.copilot as copilot_mod
    for mod in (claude_mod, codex_mod, copilot_mod):
        bound = getattr(mod, "build_session_prompt", None)
        assert bound is build_session_prompt, (
            f"{mod.__name__} must import build_session_prompt from base, "
            f"got {bound!r}")


def test_base_remains_abstract():
    """LLMProvider 는 직접 인스턴스화할 수 없다 (ABC)."""
    with pytest.raises(TypeError):
        LLMProvider()
