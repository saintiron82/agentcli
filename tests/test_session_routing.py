"""세션 provider 라우팅 회귀 테스트.

핵심 보장:
  1. 세션 provider는 프롬프트에 prev_messages를 주입하지 않는다.
  2. 세션 provider 호출 후 messages 테이블이 비어 있다 (content 미저장).
  3. session_id는 Conversation.metadata에 저장되어 재호출 시 provider에 전달된다.
  4. record_usage는 세션/비세션 무관하게 호출된다.
  5. 실패 호출은 저장소에 흔적을 남기지 않는다.
"""

from agentcli.client import LLMClient, SESSION_KEY_FMT
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import Message, LLMResponse, TokenUsage
from agentcli.store.memory import MemoryStore


class FakeSessionProvider(LLMProvider):
    provider_id = "sessprov"
    supports_sessions = True

    def __init__(self):
        self.call_count = 0
        self.last_messages: list[Message] = []
        self.last_session_id = ""

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_session_id = session_id
        self.last_cwd = cwd
        new_sid = session_id or f"session-{self.call_count}"
        return LLMResponse(
            content=f"reply-{self.call_count}",
            provider=self.provider_id, model=model,
            tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=1,
            session_id=new_sid)

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


class FakeNonSessionProvider(LLMProvider):
    provider_id = "plain"
    supports_sessions = False

    def __init__(self):
        self.last_messages: list[Message] = []
        self.last_session_id = ""

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        self.last_messages = list(messages)
        self.last_session_id = session_id
        self.last_cwd = cwd
        return LLMResponse(
            content="plain-reply", provider=self.provider_id, model=model,
            tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=1)

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


def _make_client(provider):
    reg = ProviderRegistry()
    reg.register(provider)
    reg.set_fallback_order([provider.provider_id])
    return LLMClient(store=MemoryStore(), registry=reg)


# ===== 세션 provider 경로 =====

def test_session_provider_does_not_inject_prev_messages():
    p = FakeSessionProvider()
    client = _make_client(p)

    resp1 = client.chat("first", provider="sessprov", owner="bot1")
    conv_id = resp1.conversation_id

    # 두 번째 호출: 재개
    resp2 = client.chat("second", provider="sessprov", owner="bot1",
                        conversation_id=conv_id)

    # 두 번째 호출 시 provider가 받은 messages에는 이전 턴 내용이 없다.
    # 즉 "first"가 포함되면 안 됨 (세션이 이미 보유).
    contents = [m.content for m in p.last_messages]
    assert "first" not in contents
    assert contents == ["second"]  # 오직 현재 user 메시지만


def test_session_provider_reuses_same_system_prompt_without_reinjecting():
    p = FakeSessionProvider()
    client = _make_client(p)

    resp1 = client.chat("first", provider="sessprov", owner="bot1",
                        system_prompt="GUIDE v1")
    assert [m.role for m in p.last_messages] == ["system", "user"]

    client.chat("second", provider="sessprov", owner="bot1",
                conversation_id=resp1.conversation_id,
                system_prompt="GUIDE v1")
    # 같은 세션이 이미 같은 system hash를 봤으면 최신 user 요청만 보낸다.
    assert [m.content for m in p.last_messages] == ["second"]

    client.chat("third", provider="sessprov", owner="bot1",
                conversation_id=resp1.conversation_id,
                system_prompt="GUIDE v2")
    contents = [m.content for m in p.last_messages]
    assert "GUIDE v2" in contents
    assert "third" in contents
    assert "first" not in contents
    assert "second" not in contents


def test_session_provider_passes_session_id_on_resume():
    p = FakeSessionProvider()
    client = _make_client(p)

    resp1 = client.chat("hi", provider="sessprov", owner="bot1")
    issued_sid = resp1.session_id

    resp2 = client.chat("again", provider="sessprov", owner="bot1",
                        conversation_id=resp1.conversation_id)

    # 두 번째 호출 시 provider.invoke는 첫 호출에서 발급된 session_id를 받아야 한다.
    assert p.last_session_id == issued_sid
    assert resp2.session_id == issued_sid


def test_reset_on_instruction_change_starts_new_session(tmp_path):
    p = FakeSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["sessprov"])
    client = LLMClient(store=store, registry=reg)
    guide = tmp_path / "GUIDE.md"
    guide.write_text("guide v1", encoding="utf-8")

    resp1 = client.chat("hi", provider="sessprov", owner="bot1",
                        conversation_id="conv", cwd=str(tmp_path))
    assert resp1.session_id == "session-1"

    client.chat("again", provider="sessprov", owner="bot1",
                conversation_id="conv", cwd=str(tmp_path),
                reset_on_instruction_change=True)
    assert p.last_session_id == resp1.session_id

    guide.write_text("guide v2", encoding="utf-8")
    resp3 = client.chat("new guide", provider="sessprov", owner="bot1",
                        conversation_id="conv", cwd=str(tmp_path),
                        reset_on_instruction_change=True)

    assert p.last_session_id == ""
    assert resp3.session_id == "session-3"


def test_session_provider_does_not_store_content():
    """핵심: 세션 provider 호출 후 messages 테이블은 비어 있어야 한다."""
    p = FakeSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["sessprov"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hello", provider="sessprov", owner="bot1")
    msgs = store.get_messages(resp.conversation_id)
    # 이중 저장 방지: 라이브러리는 content를 담은 메시지를 저장하지 않는다.
    assert msgs == []


def test_session_provider_persists_session_id_in_metadata():
    p = FakeSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["sessprov"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hi", provider="sessprov", owner="bot1")
    conv = store.get(resp.conversation_id)
    key = SESSION_KEY_FMT.format(provider="sessprov")
    assert conv.metadata[key] == resp.session_id


def test_session_provider_records_usage():
    p = FakeSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["sessprov"])
    client = LLMClient(store=store, registry=reg)

    client.chat("hi", provider="sessprov", owner="bot1")
    stats = store.get_token_stats("bot1")
    assert stats["total_calls"] == 1
    assert stats["total_tokens"] == 15


# ===== 비세션 provider 경로 (기존 동작 유지) =====

def test_non_session_provider_stores_content():
    p = FakeNonSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["plain"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hello", provider="plain", owner="bot1")
    msgs = store.get_messages(resp.conversation_id)
    # 비세션 provider는 현행대로 user+assistant 한 쌍을 저장.
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"


def test_non_session_provider_injects_prev_messages():
    p = FakeNonSessionProvider()
    client = _make_client(p)

    resp1 = client.chat("first", provider="plain", owner="bot1")
    resp2 = client.chat("second", provider="plain", owner="bot1",
                        conversation_id=resp1.conversation_id, context_turns=2)

    # 비세션은 prev_messages를 프롬프트에 담아 provider에게 전달
    contents = [m.content for m in p.last_messages]
    assert "first" in contents
    assert "second" in contents


# ===== 원자성 =====

class AlwaysFailProvider(LLMProvider):
    provider_id = "failprov"
    supports_sessions = True

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        return LLMResponse(content="", provider=self.provider_id, model=model)

    def list_models(self):
        return []

    def is_available(self):
        return True


def test_failed_call_does_not_pollute_store():
    """실패한 신규 호출은 conversation/messages/usage/metadata 어디에도 남지 않음."""
    p = AlwaysFailProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["failprov"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("doomed", provider="failprov", owner="bot1")
    assert resp.content == ""

    assert resp.conversation_id == ""
    assert store.list_by_owner("bot1") == []
    stats = store.get_token_stats("bot1")
    assert stats["total_calls"] == 0


def test_failed_call_does_not_assign_alias_to_existing_conversation():
    """기존 conversation에 새 alias를 붙이는 호출도 성공 전에는 store를 바꾸지 않는다."""
    p = AlwaysFailProvider()
    store = MemoryStore()
    conv = store.create("bot1", "failprov", conversation_id="existing")
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["failprov"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("doomed", provider="failprov", owner="bot1",
                       conversation_id=conv.id, alias="new-alias")
    assert resp.content == ""
    assert resp.conversation_id == conv.id
    assert store.find_by_alias("bot1", "new-alias") is None
    assert store.get(conv.id).alias == ""


def test_stable_conversation_id_preserved():
    """호출자가 지정한 conversation_id가 그대로 보존되어 재호출 시 이어진다."""
    p = FakeSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["sessprov"])
    client = LLMClient(store=store, registry=reg)

    resp1 = client.chat("hi", provider="sessprov", owner="bot1",
                        conversation_id="bot:mirinae-2")
    assert resp1.conversation_id == "bot:mirinae-2"

    resp2 = client.chat("again", provider="sessprov", owner="bot1",
                        conversation_id="bot:mirinae-2")
    assert resp2.conversation_id == "bot:mirinae-2"
    # 두 번째 호출에서 session_id가 재사용되는지
    assert p.last_session_id == resp1.session_id


def test_fallback_does_not_propagate_session_id():
    """원 provider의 session_id는 fallback provider에 넘어가면 안 된다."""
    fail_sess = AlwaysFailProvider()
    ok_plain = FakeNonSessionProvider()

    reg = ProviderRegistry()
    reg.register(fail_sess)
    reg.register(ok_plain)
    reg.set_fallback_order(["failprov", "plain"])

    client = LLMClient(store=MemoryStore(), registry=reg)
    resp = client.chat("x", provider="failprov", owner="bot1",
                       fallback=True)

    # fallback provider가 session_id를 빈 상태로 받았는지 확인
    assert ok_plain.last_session_id == ""
    # 응답은 fallback provider의 것
    assert resp.content == "plain-reply"
    assert resp.provider == "plain"


def test_fallback_receives_system_prompt_even_when_primary_suppresses_it():
    class ToggleSessionProvider(FakeSessionProvider):
        provider_id = "primary"

        def __init__(self):
            super().__init__()
            self.fail = False

        def invoke(self, messages, *, model="", timeout=120,
                   session_id="", cwd=None):
            self.last_messages = list(messages)
            self.last_session_id = session_id
            if self.fail:
                return LLMResponse(content="", provider=self.provider_id,
                                   model=model, error="usage limit",
                                   error_type="usage_limit")
            return super().invoke(messages, model=model, timeout=timeout,
                                  session_id=session_id, cwd=cwd)

    class FallbackSessionProvider(FakeSessionProvider):
        provider_id = "fallback"

    primary = ToggleSessionProvider()
    fallback = FallbackSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(primary)
    reg.register(fallback)
    reg.set_fallback_order(["primary", "fallback"])
    client = LLMClient(store=store, registry=reg)

    resp1 = client.chat("first", provider="primary", owner="bot1",
                        conversation_id="conv", system_prompt="GUIDE v1")
    assert resp1.provider == "primary"
    primary.fail = True

    resp2 = client.chat("second", provider="primary", owner="bot1",
                        conversation_id="conv", system_prompt="GUIDE v1",
                        fallback=True)

    assert resp2.provider == "fallback"
    assert [m.content for m in primary.last_messages] == ["second"]
    fallback_contents = [m.content for m in fallback.last_messages]
    assert "GUIDE v1" in fallback_contents
    assert "second" in fallback_contents
    assert "first" not in fallback_contents


# ===== stores_history=False 비세션 provider 경로 (Windows의 claude 등) =====

class FakeCliStatelessProvider(FakeNonSessionProvider):
    """CLI가 히스토리를 소유하지만 세션 resume은 불가능한 모드.

    Windows의 ClaudeProvider(supports_sessions=False, stores_history=False)와
    동일한 계약. 라이브러리는 내용 저장도, 이전 턴 재주입도 하지 않아야 한다.
    """
    provider_id = "cli-stateless"
    stores_history = False


def test_cli_stateless_provider_stores_no_content():
    p = FakeCliStatelessProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order([p.provider_id])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hello", provider="cli-stateless", owner="bot1")
    assert resp.content == "plain-reply"
    # 내용 미저장: messages 테이블이 비어 있어야 한다.
    assert store.get_messages(resp.conversation_id) == []


def test_cli_stateless_provider_does_not_reinject_prev_turns():
    p = FakeCliStatelessProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order([p.provider_id])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    resp1 = client.chat("first", provider="cli-stateless", owner="bot1")
    client.chat("second", provider="cli-stateless", owner="bot1",
                conversation_id=resp1.conversation_id)
    # 이전 턴이 프롬프트에 재주입되지 않는다 (CLI가 컨텍스트 소유).
    assert [m.content for m in p.last_messages] == ["second"]


def test_custom_non_session_provider_still_stores_content():
    """stores_history 기본값(True)인 custom provider는 기존 동작 유지."""
    p = FakeNonSessionProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order([p.provider_id])
    store = MemoryStore()
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hello", provider="plain", owner="bot1")
    msgs = store.get_messages(resp.conversation_id)
    assert [m.role for m in msgs] == ["user", "assistant"]


# ===== 히스토리 3-모드: 주입 / CLI 네이티브 / 미사용 =====

def test_new_session_param_starts_fresh_and_replaces_tracked_session():
    """미사용 모드: new_session=True 는 이 호출만 새 세션에서 시작하고,
    이후 호출은 그 새 세션을 이어간다."""
    p = FakeSessionProvider()
    client = _make_client(p)

    r1 = client.chat("first", provider="sessprov", owner="o", alias="a")
    sid1 = r1.session_id

    r2 = client.chat("fresh start", provider="sessprov", owner="o", alias="a",
                     new_session=True)
    # provider 는 빈 session_id 를 받아 새 세션을 만들어야 한다.
    assert p.last_session_id == ""
    assert r2.session_id != sid1

    r3 = client.chat("continue", provider="sessprov", owner="o", alias="a")
    # 추적 세션은 new_session 호출이 만든 세션으로 교체된다.
    assert p.last_session_id == r2.session_id
    assert r3.session_id == r2.session_id


def test_inject_context_reaches_session_provider():
    """주입 모드: inject_context 는 세션 provider 호출에도 명시적으로
    포함되어야 한다 (host-curated 컨텍스트 공유)."""
    from datetime import datetime
    p = FakeSessionProvider()
    client = _make_client(p)
    store = client._store

    # 호스트가 직접 큐레이션한 컨텍스트 대화
    ctx_conv = store.create("o", "sessprov")
    store.add_message(ctx_conv.id, Message(
        role="user", content="bull: market is strong",
        timestamp=datetime.now(), agent="bull"))

    client.chat("decide", provider="sessprov", owner="o", alias="trader",
                inject_context=[{"conversation_id": ctx_conv.id, "limit": 10}])

    contents = [m.content for m in p.last_messages]
    assert "bull: market is strong" in contents
    assert "decide" in contents


def test_session_mode_without_injection_stays_minimal():
    """CLI 네이티브 모드(기본): 주입이 없으면 최신 user 만 전달 — 3-모드 중
    어떤 것도 섞이지 않는다."""
    p = FakeSessionProvider()
    client = _make_client(p)

    r1 = client.chat("first", provider="sessprov", owner="o", alias="m")
    client.chat("second", provider="sessprov", owner="o", alias="m")
    assert [m.content for m in p.last_messages] == ["second"]
    assert p.last_session_id == r1.session_id
