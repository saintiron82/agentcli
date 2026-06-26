"""Behavior coverage for agentcli/providers/base.py and agentcli/client.py.

이 파일은 잔여(residual) 미커버 동작을 좁혀서 다룬다 — 방어용 unreachable
브랜치가 아니라 관측 가능한 행동만 검증한다.
"""

import asyncio

import pytest

from agentcli.client import (
    LLMClient, DRIFT_KEY, SESSION_KEY_FMT, SYSTEM_PROMPT_HASH_KEY_FMT,
    _compute_instructions_hashes)
from agentcli.providers.base import (
    LLMProvider, build_session_prompt, estimate_payload_prompt_tokens,
    health_from_response, run_health_command)
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import (
    LLMResponse, Message, ProviderHealth, StreamChunk, TokenUsage,
    ERROR_AUTH, ERROR_TIMEOUT)


# ============================================================
# base.py — pure helpers
# ============================================================

def test_build_session_prompt_empty_returns_empty_string():
    assert build_session_prompt([]) == ""


def test_build_session_prompt_no_user_falls_back_to_last_message():
    """user 역할이 하나도 없으면 마지막 메시지를 최신 요청으로 취급."""
    prompt = build_session_prompt([
        Message(role="system", content="be terse"),
        Message(role="assistant", content="final note"),
    ])
    # system 은 따로 분리되고, 마지막(assistant) 이 user request 자리에 온다.
    assert "be terse" in prompt
    assert "final note" in prompt
    # assistant 가 최신 요청이 되었으므로 Context 블록으로 다시 직렬화되지 않음.
    assert "Context (injected by host application):" not in prompt


def test_estimate_payload_prompt_tokens_empty_is_zero():
    assert estimate_payload_prompt_tokens("") == 0
    assert estimate_payload_prompt_tokens("   ") == 0


def test_estimate_payload_prompt_tokens_rounds_up():
    # (len + 3)//4, 최소 1.
    assert estimate_payload_prompt_tokens("a") == 1
    assert estimate_payload_prompt_tokens("abcd") == 1
    assert estimate_payload_prompt_tokens("abcde") == 2


# ============================================================
# base.py — health_from_response variants
# ============================================================

def test_health_from_response_success():
    resp = LLMResponse(content="pong", provider="fake", model="m",
                       tokens=TokenUsage(), exit_code=0)
    h = health_from_response("fake", resp, binary="/bin/fake", version="1.0")
    assert h.ok is True
    assert h.status == "ok"
    assert h.available is True
    assert h.auth_ok is True
    assert h.binary == "/bin/fake"
    assert h.version == "1.0"
    assert h.exit_code == 0


def test_health_from_response_auth_failure():
    resp = LLMResponse(content="", provider="fake", model="m",
                       error="HTTP 401 unauthorized", error_type=ERROR_AUTH,
                       exit_code=1)
    h = health_from_response("fake", resp, binary="/bin/fake")
    assert h.ok is False
    assert h.status == ERROR_AUTH
    assert h.auth_ok is False
    # binary 가 있으면 available=True (이진은 있으나 인증 실패).
    assert h.available is True
    assert h.error_type == ERROR_AUTH


def test_health_from_response_timeout_maps_status():
    resp = LLMResponse(content="", provider="fake", model="m",
                       error="request timed out", error_type=ERROR_TIMEOUT)
    h = health_from_response("fake", resp)
    assert h.ok is False
    assert h.status == "timeout"
    # timeout 은 auth 실패가 아니므로 auth_ok=True 로 남는다.
    assert h.auth_ok is True
    # binary 미지정 → available=False.
    assert h.available is False


def test_health_from_response_unknown_error_status():
    resp = LLMResponse(content="", provider="fake", model="m")
    # error 없음 → error_type 없음 → status "unknown".
    h = health_from_response("fake", resp)
    assert h.ok is False
    assert h.status == "unknown"


# ============================================================
# base.py — run_health_command timeout / not-found
# ============================================================

def test_run_health_command_success():
    cp = run_health_command(["python3", "-c", "print('hi')"], timeout=10)
    assert cp.returncode == 0
    assert "hi" in cp.stdout


def test_run_health_command_timeout_normalized():
    cp = run_health_command(
        ["python3", "-c", "import time; time.sleep(5)"], timeout=1)
    assert cp.returncode == 124
    assert "timeout" in cp.stderr


def test_run_health_command_binary_not_found():
    cp = run_health_command(["definitely-not-a-real-binary-xyz"], timeout=5)
    assert cp.returncode == 127
    assert "not found" in cp.stderr


# ============================================================
# base.py — default health_check (available branch)
# ============================================================

class _AvailableProvider(LLMProvider):
    provider_id = "avail"

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        return LLMResponse(content="ok", provider=self.provider_id,
                           model=model, tokens=TokenUsage())

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


def test_default_health_check_available_reports_ok():
    h = _AvailableProvider().health_check()
    assert h.ok is True
    assert h.status == "ok"
    assert h.available is True
    assert h.auth_ok is None


# ============================================================
# base.py — resolve_model strict error message
# ============================================================

class _ModelProvider(LLMProvider):
    provider_id = "mp"

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        return LLMResponse(content="ok", provider=self.provider_id,
                           model=model, tokens=TokenUsage())

    def list_models(self):
        return [{"id": "real-id", "name": "Real", "aliases": ["r"]}]

    def is_available(self):
        return True


def test_resolve_model_empty_selector_returns_empty():
    assert _ModelProvider().resolve_model("") == ""
    assert _ModelProvider().resolve_model("   ") == ""


def test_resolve_model_strict_lists_supported_selectors():
    p = _ModelProvider()
    with pytest.raises(ValueError) as exc:
        p.resolve_model("nope", strict=True)
    msg = str(exc.value)
    assert "unsupported model for mp" in msg
    # 알려진 selector 들이 메시지에 나열된다.
    assert "real-id" in msg
    assert "Real" in msg
    assert "r" in msg


def test_resolve_model_nonstrict_passthrough_unknown():
    assert _ModelProvider().resolve_model("anything", strict=False) == "anything"


# ============================================================
# base.py — default session_alive + default _dispatch_stream_event
# ============================================================

def test_default_session_alive_is_none():
    assert _AvailableProvider().session_alive("sid") is None


def test_default_dispatch_stream_event_yields_raw_event():
    p = _AvailableProvider()

    async def collect():
        from agentcli.providers.base import StreamState
        out = []
        async for c in p._dispatch_stream_event({"k": "v"}, StreamState()):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0].type == "event"
    assert chunks[0].data == {"k": "v"}


# ============================================================
# base.py — default stream_async fallback (non-streaming provider)
# ============================================================

class _NonStreamProvider(LLMProvider):
    """supports_streaming=False — 기본 stream_async (invoke_async 후 일괄 방출)."""
    provider_id = "nostream"
    supports_sessions = True

    def __init__(self, content="streamed-ok"):
        self._content = content

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        return LLMResponse(
            content=self._content, provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=7), latency_ms=5,
            session_id=session_id or "sid-x")

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


def test_default_stream_async_emits_text_then_done():
    p = _NonStreamProvider()

    async def collect():
        out = []
        async for c in p.stream_async([Message(role="user", content="hi")],
                                      model="m"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    types = [c.type for c in chunks]
    assert types == ["text", "done"]
    assert chunks[0].content == "streamed-ok"
    done = chunks[1]
    assert done.content == "streamed-ok"
    assert done.session_id == "sid-x"
    assert done.usage.total_tokens == 7
    assert done.data["provider"] == "nostream"


def test_default_stream_async_skips_text_chunk_when_empty():
    p = _NonStreamProvider(content="")

    async def collect():
        out = []
        async for c in p.stream_async([Message(role="user", content="hi")]):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    # 빈 content 면 text chunk 없이 done 만.
    assert [c.type for c in chunks] == ["done"]
    assert chunks[0].content == ""


# ============================================================
# client.py — helper providers
# ============================================================

class FakeSessionProvider(LLMProvider):
    provider_id = "sess"
    supports_sessions = True

    def __init__(self):
        self.call_count = 0
        self.last_messages: list[Message] = []
        self.last_session_id = ""

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_session_id = session_id
        sid = session_id or f"sid-{self.call_count}"
        return LLMResponse(
            content=f"reply-{self.call_count}", provider=self.provider_id,
            model=model, tokens=TokenUsage(total_tokens=3), latency_ms=1,
            session_id=sid)

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


class FakeNonSessionProvider(LLMProvider):
    provider_id = "plain"
    supports_sessions = False

    def __init__(self, content="plain-reply", fail=False):
        self._content = content
        self._fail = fail
        self.last_messages: list[Message] = []
        self.last_session_id = ""

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        self.last_messages = list(messages)
        self.last_session_id = session_id
        if self._fail:
            return LLMResponse(content="", provider=self.provider_id,
                               model=model, error="boom", error_type="unknown")
        return LLMResponse(
            content=self._content, provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=4), latency_ms=1)

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


class StreamProvider(LLMProvider):
    """진짜 증분 청크를 내는 provider — chat_stream 경로 테스트용."""
    provider_id = "streamer"
    supports_sessions = True
    supports_streaming = True

    def __init__(self, *, text="hello", fail_before_output=False,
                 fail_after_output=False, empty=False):
        self._text = text
        self._fail_before = fail_before_output
        self._fail_after = fail_after_output
        self._empty = empty
        self.last_session_id = ""
        self.last_messages: list[Message] = []

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        return LLMResponse(content=self._text, provider=self.provider_id,
                           model=model, tokens=TokenUsage())

    async def stream_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None, idle_timeout=None,
                           wall_timeout=None):
        self.last_session_id = session_id
        self.last_messages = list(messages)
        if self._fail_before:
            yield StreamChunk(type="error", content="primary boom")
            return
        if self._empty:
            yield StreamChunk(type="done", content="", session_id="s-empty")
            return
        yield StreamChunk(type="text", content=self._text)
        if self._fail_after:
            yield StreamChunk(type="error", content="mid-stream boom")
            return
        yield StreamChunk(
            type="done", content=self._text, session_id="s-ok",
            usage=TokenUsage(total_tokens=9),
            data={"latency_ms": 12})

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


def _client(*providers, fallback=None):
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    reg.set_fallback_order(fallback or [p.provider_id for p in providers])
    return LLMClient(store=MemoryStore(), registry=reg)


# ============================================================
# client.py — unknown provider paths (sync + async)
# ============================================================

def test_chat_unknown_provider_returns_error_response():
    client = _client(FakeNonSessionProvider())
    resp = client.chat("hi", provider="ghost", owner="u")
    assert resp.content == ""
    assert resp.error_type == "unknown"
    assert "unknown provider" in resp.error


def test_chat_async_unknown_provider_returns_error_response():
    client = _client(FakeNonSessionProvider())
    resp = asyncio.run(client.chat_async("hi", provider="ghost", owner="u"))
    assert resp.content == ""
    assert "unknown provider" in resp.error


def test_chat_stream_unknown_provider_emits_error_then_done():
    client = _client(FakeNonSessionProvider())

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="ghost", owner="u"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    types = [c.type for c in chunks]
    assert types == ["error", "done"]
    assert "unknown provider" in chunks[0].content


# ============================================================
# client.py — no-fallback failure returns last_resp (sync + async)
# ============================================================

def test_chat_no_fallback_returns_primary_failure():
    fail = FakeNonSessionProvider(fail=True)
    fail.provider_id = "f1"
    client = _client(fail)
    resp = client.chat("hi", provider="f1", owner="u")
    assert resp.content == ""
    assert resp.error == "boom"
    # 실패 신규 호출은 conversation_id 를 되돌린다.
    assert resp.conversation_id == ""


def test_chat_async_no_fallback_returns_primary_failure():
    fail = FakeNonSessionProvider(fail=True)
    fail.provider_id = "f1"
    client = _client(fail)
    resp = asyncio.run(client.chat_async("hi", provider="f1", owner="u"))
    assert resp.content == ""
    assert resp.error == "boom"


# ============================================================
# client.py — alias / conversation_id conflict (async path)
# ============================================================

def test_chat_async_alias_conflict_rejected():
    client = _client(FakeNonSessionProvider())
    asyncio.run(client.chat_async(
        "one", provider="plain", owner="team",
        conversation_id="c-one", alias="collector"))

    async def conflicting():
        return await client.chat_async(
            "two", provider="plain", owner="team",
            conversation_id="c-two", alias="collector")

    with pytest.raises(ValueError) as exc:
        asyncio.run(conflicting())
    assert "alias conflict" in str(exc.value)


# ============================================================
# client.py — new_session resets tracked session (async)
# ============================================================

def test_chat_async_new_session_starts_fresh():
    p = FakeSessionProvider()
    client = _client(p)
    r1 = asyncio.run(client.chat_async(
        "first", provider="sess", owner="o", alias="a"))
    sid1 = r1.session_id

    r2 = asyncio.run(client.chat_async(
        "fresh", provider="sess", owner="o", alias="a", new_session=True))
    assert p.last_session_id == ""
    assert r2.session_id != sid1


# ============================================================
# client.py — reset_on_instruction_change via system_prompt hash drift
# ============================================================

def test_reset_on_system_prompt_change_starts_new_session():
    p = FakeSessionProvider()
    client = _client(p)
    r1 = client.chat("hi", provider="sess", owner="o", alias="a",
                     system_prompt="GUIDE v1",
                     reset_on_instruction_change=True)
    sid1 = r1.session_id

    # 같은 system_prompt → 세션 유지.
    client.chat("again", provider="sess", owner="o", alias="a",
                system_prompt="GUIDE v1",
                reset_on_instruction_change=True)
    assert p.last_session_id == sid1

    # system_prompt 변경 → 새 세션 강제.
    client.chat("changed", provider="sess", owner="o", alias="a",
                system_prompt="GUIDE v2",
                reset_on_instruction_change=True)
    assert p.last_session_id == ""


# ============================================================
# client.py — drift detection logs and stores hashes
# ============================================================

def test_drift_detection_logs_and_updates_hashes(tmp_path, caplog):
    import logging
    p = FakeNonSessionProvider()
    client = _client(p)
    guide = tmp_path / "AGENTS.md"
    guide.write_text("v1", encoding="utf-8")

    r1 = client.chat("hi", provider="plain", owner="o",
                     conversation_id="cdrift", cwd=str(tmp_path))
    conv = client._store.get(r1.conversation_id)
    stored = conv.metadata.get(DRIFT_KEY)
    assert stored and "AGENTS.md" in stored

    guide.write_text("v2-changed", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="agentcli.client"):
        client.chat("again", provider="plain", owner="o",
                    conversation_id="cdrift", cwd=str(tmp_path))
    assert any("드리프트" in rec.message or "드리프트" in rec.getMessage()
               for rec in caplog.records)
    # 해시가 갱신되어 저장된다.
    conv2 = client._store.get("cdrift")
    assert conv2.metadata[DRIFT_KEY]["AGENTS.md"] != stored["AGENTS.md"]


def test_compute_instructions_hashes_no_cwd_is_empty():
    assert _compute_instructions_hashes(None) == {}
    assert _compute_instructions_hashes("") == {}


# ============================================================
# client.py — get_token_stats passthrough + fallback
# ============================================================

def test_get_token_stats_passthrough():
    p = FakeNonSessionProvider()
    client = _client(p)
    client.chat("hi", provider="plain", owner="acct1")
    stats = client.get_token_stats("acct1")
    assert stats["total_calls"] == 1
    assert stats["total_tokens"] == 4


def test_get_token_stats_falls_back_when_store_lacks_method():
    class BareStore:
        # ConversationStore 의 일부만 흉내 — get_token_stats 없음.
        pass

    client = LLMClient(store=BareStore(), registry=_client(
        FakeNonSessionProvider())._registry)
    stats = client.get_token_stats("anyone")
    assert stats == {"total_tokens": 0, "total_calls": 0}


# ============================================================
# client.py — session_alive routing
# ============================================================

def test_session_alive_unknown_provider_raises():
    client = _client(FakeNonSessionProvider())
    with pytest.raises(ValueError):
        client.session_alive("ghost", owner="o")


def test_session_alive_no_tracked_conversation_returns_false():
    client = _client(FakeSessionProvider())
    # 추적 중인 conversation 없음.
    assert client.session_alive("sess", owner="o", alias="missing") is False


def test_session_alive_no_session_id_returns_false():
    p = FakeSessionProvider()
    client = _client(p)
    # alias 만 만들고 session 메타데이터 없는 conv 를 직접 생성.
    conv = client._store.create("o", "sess", alias="a")
    # session_id 메타데이터가 비어 있으므로 False.
    assert client.session_alive("sess", owner="o",
                                conversation_id=conv.id) is False


def test_session_alive_delegates_to_provider():
    class LiveProvider(FakeSessionProvider):
        provider_id = "live"

        def session_alive(self, session_id, *, cwd=None):
            return True

    p = LiveProvider()
    client = _client(p)
    r = client.chat("hi", provider="live", owner="o", alias="a")
    assert r.session_id
    assert client.session_alive("live", owner="o", alias="a") is True


# ============================================================
# client.py — resolve_model / select_model meta API
# ============================================================

def test_client_resolve_model_unknown_provider_raises():
    client = _client(FakeNonSessionProvider())
    with pytest.raises(ValueError):
        client.resolve_model("ghost", "m")


def test_client_capabilities_unknown_provider_raises():
    client = _client(FakeNonSessionProvider())
    with pytest.raises(ValueError):
        client.capabilities("ghost")


# ============================================================
# client.py — chat_stream fallback chain behaviors
# ============================================================

def test_chat_stream_success_persists_and_emits_done():
    p = StreamProvider(text="streamed")
    client = _client(p)

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="streamer",
                                          owner="o", alias="a"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    types = [c.type for c in chunks]
    assert "text" in types
    assert types[-1] == "done"
    done = chunks[-1]
    assert done.content == "streamed"
    assert done.data["conversation_id"]
    # 세션 metadata 가 저장되었는지.
    conv = client._store.get(done.data["conversation_id"])
    assert conv.metadata[SESSION_KEY_FMT.format(provider="streamer")] == "s-ok"


def test_chat_stream_fallback_before_output_switches_provider():
    primary = StreamProvider(text="x", fail_before_output=True)
    primary.provider_id = "p1"
    secondary = StreamProvider(text="from-secondary")
    secondary.provider_id = "p2"
    client = _client(primary, secondary, fallback=["p1", "p2"])

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="p1", owner="o",
                                          fallback=True):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    done = chunks[-1]
    assert done.type == "done"
    assert done.content == "from-secondary"
    assert done.data["provider"] == "p2"


def test_chat_stream_no_fallback_failure_before_output_yields_error_done():
    primary = StreamProvider(text="x", fail_before_output=True)
    primary.provider_id = "p1"
    client = _client(primary)

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="p1", owner="o"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    types = [c.type for c in chunks]
    assert types[-1] == "done"
    assert chunks[-1].data.get("error") is True
    assert any(c.type == "error" for c in chunks)


def test_chat_stream_error_after_output_stops_and_marks_done_error():
    primary = StreamProvider(text="partial", fail_after_output=True)
    primary.provider_id = "p1"
    secondary = StreamProvider(text="should-not-run")
    secondary.provider_id = "p2"
    client = _client(primary, secondary, fallback=["p1", "p2"])

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="p1", owner="o",
                                          fallback=True):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    # 출력이 나간 뒤 실패 → fallback 안 함, error done.
    text_chunks = [c for c in chunks if c.type == "text"]
    assert text_chunks and text_chunks[0].content == "partial"
    done = chunks[-1]
    assert done.type == "done"
    assert done.data.get("error") is True
    assert secondary.last_session_id == ""  # 두 번째 provider 미실행


def test_chat_stream_empty_response_no_fallback_yields_empty_error():
    primary = StreamProvider(empty=True)
    primary.provider_id = "p1"
    client = _client(primary)

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="p1", owner="o"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    assert any(c.type == "error" and "empty stream response" in c.content
               for c in chunks)
    assert chunks[-1].type == "done"
    assert chunks[-1].data.get("error") is True


def test_chat_stream_empty_response_falls_back():
    primary = StreamProvider(empty=True)
    primary.provider_id = "p1"
    secondary = StreamProvider(text="recovered")
    secondary.provider_id = "p2"
    client = _client(primary, secondary, fallback=["p1", "p2"])

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="p1", owner="o",
                                          fallback=True):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    done = chunks[-1]
    assert done.type == "done"
    assert done.content == "recovered"
    assert done.data["provider"] == "p2"


def test_chat_stream_exception_in_provider_is_caught():
    class ExplodingProvider(StreamProvider):
        provider_id = "boom"

        async def stream_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None, idle_timeout=None,
                               wall_timeout=None):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    p = ExplodingProvider()
    client = _client(p)

    async def collect():
        out = []
        async for c in client.chat_stream("hi", provider="boom", owner="o"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    assert any(c.type == "error" and "kaboom" in c.content for c in chunks)
    assert chunks[-1].type == "done"


# ============================================================
# client.py — _discard_failed_prepare on existing conversation
# ============================================================

def test_failed_call_on_existing_conversation_is_preserved():
    """기존 conversation 에 대한 실패 호출은 그 conversation 을 삭제하지 않는다."""
    fail = FakeNonSessionProvider(fail=True)
    fail.provider_id = "f1"
    client = _client(fail)
    conv = client._store.create("o", "f1", conversation_id="keep-me")

    resp = client.chat("doomed", provider="f1", owner="o",
                       conversation_id="keep-me")
    assert resp.content == ""
    # created_conversation=False 였으므로 conversation 은 보존된다.
    assert resp.conversation_id == "keep-me"
    assert client._store.get("keep-me") is not None


# ============================================================
# client.py — health_check (all providers) + capability_matrix
# ============================================================

def test_health_check_all_providers_returns_dict():
    a = _AvailableProvider()
    b = FakeSessionProvider()
    client = _client(a, b)
    result = client.health_check()  # provider 미지정 → 전체 dict.
    assert set(result.keys()) == {"avail", "sess"}
    assert all(isinstance(h, ProviderHealth) for h in result.values())


def test_capability_matrix_covers_registered_providers():
    client = _client(FakeSessionProvider(), FakeNonSessionProvider())
    matrix = client.capability_matrix()
    assert set(matrix.keys()) == {"sess", "plain"}
    assert matrix["sess"]["sessions"] is True
    assert matrix["plain"]["sessions"] is False


def test_unsupported_options_lists_unknown_keys():
    client = _client(FakeSessionProvider())
    # FakeSessionProvider.invoke 는 mcp_config 같은 옵션을 받지 않는다.
    unsupported = client.unsupported_options(
        "sess", {"mcp_config": {}, "model": "x"})
    assert "mcp_config" in unsupported
    # model 은 공통 인자라 옵션 집합에 없으므로 unsupported 로 보고된다.
    assert client.unsupported_options("sess", None) == []


def test_supports_feature_query():
    client = _client(FakeSessionProvider())
    assert client.supports("sess", "sessions") is True
    assert client.supports("sess", "streaming") is False


# ============================================================
# client.py — get_alias_status
# ============================================================

def test_get_alias_status_missing_alias():
    client = _client(FakeSessionProvider())
    status = client.get_alias_status("o", "nope")
    assert status["exists"] is False
    assert status["status"] == "missing"
    assert status["conversation_id"] == ""


def test_get_alias_status_fresh_then_stale(tmp_path):
    p = FakeSessionProvider()
    client = _client(p)
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("v1", encoding="utf-8")

    client.chat("hi", provider="sess", owner="o", alias="a",
                cwd=str(tmp_path))
    fresh = client.get_alias_status("o", "a", cwd=str(tmp_path))
    assert fresh["exists"] is True
    assert fresh["status"] == "fresh"
    assert fresh["fresh"] is True
    assert "sess" in fresh["session_providers"]

    guide.write_text("v2-different", encoding="utf-8")
    stale = client.get_alias_status("o", "a", cwd=str(tmp_path))
    assert stale["status"] == "stale"
    assert stale["stale"] is True


def test_get_alias_status_unknown_when_no_hashes():
    p = FakeSessionProvider()
    client = _client(p)
    # cwd 없이 호출 → instruction 해시 없음 → status "unknown".
    client.chat("hi", provider="sess", owner="o", alias="a")
    status = client.get_alias_status("o", "a")
    assert status["status"] == "unknown"


# ============================================================
# client.py — list_drifts
# ============================================================

def test_list_drifts_without_owner_is_empty():
    client = _client(FakeSessionProvider())
    assert client.list_drifts() == []


def test_list_drifts_reports_tracked_hashes(tmp_path):
    p = FakeNonSessionProvider()
    client = _client(p)
    guide = tmp_path / "GUIDE.md"
    guide.write_text("g1", encoding="utf-8")
    client.chat("hi", provider="plain", owner="o", alias="a",
                cwd=str(tmp_path))

    drifts = client.list_drifts(owner="o")
    assert len(drifts) == 1
    assert drifts[0]["alias"] == "a"
    assert "GUIDE.md" in drifts[0]["cwd_hashes"]

    # alias 필터로 매칭되지 않으면 잔여(residual) 결과 없음.
    assert client.list_drifts(owner="o", alias="other") == []


# ============================================================
# client.py — clear_session_metadata
# ============================================================

def test_clear_session_metadata_by_alias_blanks_keys():
    p = FakeSessionProvider()
    client = _client(p)
    r = client.chat("hi", provider="sess", owner="o", alias="a",
                    system_prompt="GUIDE")
    conv = client._store.get(r.conversation_id)
    skey = SESSION_KEY_FMT.format(provider="sess")
    hkey = SYSTEM_PROMPT_HASH_KEY_FMT.format(provider="sess")
    assert conv.metadata.get(skey)
    assert conv.metadata.get(hkey)

    rows = client.clear_session_metadata(owner="o", alias="a")
    assert len(rows) == 1
    cleared = set(rows[0]["cleared_keys"])
    assert skey in cleared
    assert hkey in cleared
    conv2 = client._store.get(r.conversation_id)
    assert conv2.metadata.get(skey) == ""
    assert conv2.metadata.get(hkey) == ""


def test_clear_session_metadata_provider_filter():
    p = FakeSessionProvider()
    client = _client(p)
    r = client.chat("hi", provider="sess", owner="o", alias="a")
    # 다른 provider 로 필터하면 sess 키는 건드리지 않는다.
    rows = client.clear_session_metadata(owner="o", provider="other")
    assert rows == []
    conv = client._store.get(r.conversation_id)
    assert conv.metadata.get(SESSION_KEY_FMT.format(provider="sess"))


def test_clear_session_metadata_all_owner_conversations():
    p = FakeSessionProvider()
    client = _client(p)
    client.chat("a", provider="sess", owner="o", alias="one")
    client.chat("b", provider="sess", owner="o", alias="two")
    # alias 미지정 → owner 의 모든 conversation 순회.
    rows = client.clear_session_metadata(owner="o")
    assert len(rows) == 2
