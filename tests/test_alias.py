"""Conversation alias 회귀 테스트."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock
from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.providers.copilot import CopilotProvider
from agentcli.store.memory import MemoryStore
from agentcli.store.sqlite import SQLiteStore
from agentcli.types import Message, LLMResponse, TokenUsage


# ===== MemoryStore =====

def test_memory_create_with_alias():
    store = MemoryStore()
    conv = store.create("team", "claude", alias="bull-analyst")
    assert conv.alias == "bull-analyst"
    assert conv.owner == "team"


def test_memory_find_by_alias():
    store = MemoryStore()
    conv = store.create("team", "claude", alias="bull")
    found = store.find_by_alias("team", "bull")
    assert found is not None
    assert found.id == conv.id


def test_memory_alias_unique_per_owner():
    """동명 alias가 다른 owner에서는 별개로 존재."""
    store = MemoryStore()
    c1 = store.create("teamA", "claude", alias="bull")
    c2 = store.create("teamB", "claude", alias="bull")
    assert c1.id != c2.id
    assert store.find_by_alias("teamA", "bull").id == c1.id
    assert store.find_by_alias("teamB", "bull").id == c2.id


def test_memory_same_alias_same_owner_returns_existing():
    """같은 owner + 같은 alias로 create 두 번 하면 기존 반환."""
    store = MemoryStore()
    c1 = store.create("team", "claude", alias="bull")
    c2 = store.create("team", "claude", alias="bull")
    assert c1.id == c2.id


def test_memory_set_alias():
    store = MemoryStore()
    conv = store.create("team", "claude")
    store.set_alias(conv.id, "newname")
    got = store.find_by_alias("team", "newname")
    assert got is not None
    assert got.id == conv.id


def test_memory_set_alias_steals_from_other():
    """같은 owner의 다른 conversation이 이 alias를 쓰고 있으면 박탈."""
    store = MemoryStore()
    c1 = store.create("team", "claude", alias="bull")
    c2 = store.create("team", "claude")
    store.set_alias(c2.id, "bull")
    # c1의 alias는 빼앗김
    assert store.get(c1.id).alias == ""
    # 새 bull은 c2
    assert store.find_by_alias("team", "bull").id == c2.id


def test_memory_delete_clears_alias_index():
    store = MemoryStore()
    c = store.create("team", "claude", alias="bull")
    store.delete(c.id)
    assert store.find_by_alias("team", "bull") is None


# ===== SQLiteStore =====

def test_sqlite_create_with_alias():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude", alias="trader")
    assert conv.alias == "trader"
    found = store.find_by_alias("team", "trader")
    assert found is not None
    assert found.id == conv.id


def test_sqlite_alias_unique_per_owner():
    store = SQLiteStore(":memory:")
    store.create("A", "claude", alias="x")
    store.create("B", "claude", alias="x")
    assert store.find_by_alias("A", "x").owner == "A"
    assert store.find_by_alias("B", "x").owner == "B"


def test_sqlite_set_alias_steals():
    store = SQLiteStore(":memory:")
    c1 = store.create("team", "claude", alias="bull")
    c2 = store.create("team", "claude")
    store.set_alias(c2.id, "bull")
    assert store.get(c1.id).alias == ""
    assert store.find_by_alias("team", "bull").id == c2.id


# ===== LLMClient alias resolution =====

class AliasTrackingProvider(LLMProvider):
    provider_id = "atp"
    supports_sessions = True

    def __init__(self):
        self.last_alias = None
        self.last_session_id = None
        self.call_count = 0

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None, alias=""):
        self.call_count += 1
        self.last_alias = alias
        self.last_session_id = session_id
        return LLMResponse(
            content=f"ok-{self.call_count}", provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=5),
            session_id=session_id or f"sid-{self.call_count}")

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None, alias=""):
        return self.invoke(messages, model=model, timeout=timeout,
                           session_id=session_id, cwd=cwd, alias=alias)

    def list_models(self): return []
    def is_available(self): return True


def test_chat_with_alias_resolves_and_reuses():
    p = AliasTrackingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["atp"])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    r1 = client.chat("hi", provider="atp", owner="team", alias="bull")
    r2 = client.chat("hi again", provider="atp", owner="team", alias="bull")
    # 같은 conversation id로 이어져야 함
    assert r1.conversation_id == r2.conversation_id
    # provider에게 alias가 전달됐는지
    assert p.last_alias == "bull"
    # 세션 재사용
    assert p.last_session_id == r1.session_id


def test_chat_with_alias_different_owners_are_independent():
    p = AliasTrackingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["atp"])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    rA = client.chat("a", provider="atp", owner="A", alias="analyst")
    rB = client.chat("b", provider="atp", owner="B", alias="analyst")
    assert rA.conversation_id != rB.conversation_id


def test_chat_alias_takes_precedence_over_bot_key_not_set():
    """conversation_id 없이 alias만 주면 alias로 resolve."""
    p = AliasTrackingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["atp"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    r = client.chat("x", provider="atp", owner="team", alias="trader")
    # conversation_id는 내부 UUID (alias 기반 신규 conv). resp에 alias는 유지.
    assert r.conversation_id
    # Provider는 alias를 수신
    assert p.last_alias == "trader"


def test_chat_async_with_alias():
    p = AliasTrackingProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["atp"])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    r1 = asyncio.run(client.chat_async(
        "hi", provider="atp", owner="team", alias="a1"))
    r2 = asyncio.run(client.chat_async(
        "again", provider="atp", owner="team", alias="a1"))
    assert r1.conversation_id == r2.conversation_id
    assert p.last_alias == "a1"


# ===== Provider가 alias를 안 받는 경우도 깨지지 않음 =====

class NoAliasProvider(LLMProvider):
    provider_id = "noalias"
    supports_sessions = False

    def __init__(self):
        self.called = False

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        # alias 파라미터 없음 — helper가 감지해서 전달 안 해야 함
        self.called = True
        return LLMResponse(
            content="no-alias-ok", provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=3))

    def list_models(self): return []
    def is_available(self): return True


def test_provider_without_alias_param_still_works():
    """alias 미지원 provider도 정상 호출 (호환성)."""
    p = NoAliasProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["noalias"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    r = client.chat("hi", provider="noalias", owner="team", alias="some-alias")
    assert r.content == "no-alias-ok"
    assert p.called


# ===== Copilot --name 전달 =====

@patch("agentcli.providers.copilot.run_owned_command")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_copilot_alias_becomes_name_flag(mock_find, mock_env, mock_run):
    """CopilotProvider.invoke(alias=)는 --name=<alias>로 CLI에 전달된다."""
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    p = CopilotProvider()
    p.invoke([Message(role="user", content="hi")], alias="bull-agent")
    cmd = mock_run.call_args[0][0]
    assert "--name=bull-agent" in cmd
    # session_id가 없을 때 alias로 resume 시도
    assert "--resume=bull-agent" in cmd


# ===== ai_caller 브릿지 통합 =====
# src.bots 의존성이 있어 이 테스트는 skip 조건부로 — project config 필요
try:
    from src.bots.ai_caller import invoke_ai  # noqa: F401
    HAS_AI_CALLER = True
except Exception:
    HAS_AI_CALLER = False


@pytest.mark.skipif(not HAS_AI_CALLER, reason="src.bots not importable")
def test_ai_caller_accepts_alias_param():
    import inspect
    from src.bots.ai_caller import invoke_ai, invoke_ai_async, invoke_ai_stream
    for fn in (invoke_ai, invoke_ai_async, invoke_ai_stream):
        sig = inspect.signature(fn)
        assert "alias" in sig.parameters, f"{fn.__name__} missing alias"
