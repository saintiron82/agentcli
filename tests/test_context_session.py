"""ContextSession — 큰 컨텍스트 1회 주입 후 다회 질의 (refine/fork).

핵심: "잠시 있다가 다시 요청" 대응 — 세션이 살아있으면 전사록 재전송 없이
이어가고, (시간 경과/프로세스 재시작/만료로) 죽었으면 자동 재시드.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentcli import ContextSession, LLMClient, MemoryStore
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse, TokenUsage


def _ctx(alive, **kw):
    client = MagicMock()
    client.session_alive.return_value = alive
    return ContextSession(client, "BIGCTX", provider="claude", owner="u",
                          alias="mtg", **kw), client


def test_refine_seeds_when_no_session():
    ctx, client = _ctx(alive=False)            # 세션 없음
    ctx.refine("회의록")
    p = client.chat.call_args[0][0]
    assert "BIGCTX" in p and "회의록" in p       # 전사록 시드
    assert client.chat.call_args[1]["alias"] == "mtg"
    assert client.chat.call_args[1].get("new_session") is True


def test_refine_resumes_when_alive():
    ctx, client = _ctx(alive=True)             # 세션 살아있음
    ctx.refine("회의록")
    p = client.chat.call_args[0][0]
    assert "BIGCTX" not in p                    # 전사록 재전송 안 함
    assert p == "회의록"
    assert client.chat.call_args[1].get("new_session") is not True


def test_refine_reseeds_after_session_dies():
    """잠시 있다가 다시 요청 — 세션이 죽었으면 전사록 자동 재시드 + 새 세션."""
    client = MagicMock()
    client.session_alive.side_effect = [False, True, False]
    ctx = ContextSession(client, "BIGCTX", provider="claude", owner="u", alias="mtg")
    ctx.refine("A")
    assert "BIGCTX" in client.chat.call_args[0][0]       # 시드
    ctx.refine("B")
    assert "BIGCTX" not in client.chat.call_args[0][0]   # resume
    ctx.refine("C")
    assert "BIGCTX" in client.chat.call_args[0][0]       # 죽음 → 재시드
    assert client.chat.call_args[1].get("new_session") is True


def test_cross_process_reconstruct_alive_resumes():
    """재시작 후 새 객체 + 같은 alias, 세션 살아있으면 재시드 없이 이어감."""
    client = MagicMock()
    client.session_alive.return_value = True
    ctx = ContextSession(client, "BIGCTX", provider="claude", owner="u", alias="mtg")
    ctx.refine("이어서")                          # _seeded=False(새 프로세스)지만 alive
    assert "BIGCTX" not in client.chat.call_args[0][0]


def test_unknown_liveness_seeds_then_trusts():
    """copilot 등 None: 첫 호출 시드, 이후엔 신뢰 resume."""
    client = MagicMock()
    client.session_alive.return_value = None
    ctx = ContextSession(client, "C", provider="copilot", owner="u", alias="m")
    ctx.refine("A")
    assert "C" in client.chat.call_args[0][0]            # 시드
    ctx.refine("B")
    assert "C" not in client.chat.call_args[0][0]        # 신뢰 resume


def test_fork_reseeds_independent_sessions():
    client = MagicMock()
    client.session_alive.return_value = True
    ctx = ContextSession(client, "BIGCTX", provider="claude", owner="u", alias="mtg")
    ctx.fork("액션아이템")
    a1 = client.chat.call_args
    assert "BIGCTX" in a1[0][0] and a1[1]["new_session"] is True
    ctx.fork("요약", label="sum")
    a2 = client.chat.call_args
    assert "BIGCTX" in a2[0][0] and a2[1]["new_session"] is True
    assert a1[1]["alias"] != a2[1]["alias"]              # 독립 (다른 alias)
    assert "sum" in a2[1]["alias"]


def test_fork_many_parallel_with_concurrency_cap():
    """복수 독립 변형을 병렬 실행 — 순서 보존, 동시 상한 준수, 각 변형 재시드."""
    import asyncio
    client = MagicMock()
    client.session_alive.return_value = True
    state = {"cur": 0, "max": 0}

    async def fake_chat_async(prompt, **kw):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return f"R:{prompt[:8]}"

    client.chat_async = AsyncMock(side_effect=fake_chat_async)
    ctx = ContextSession(client, "CTX", provider="claude", owner="u", alias="m")
    prompts = [f"q{i}" for i in range(6)]
    results = asyncio.run(ctx.fork_many(prompts, concurrency=2))

    assert len(results) == 6
    assert state["max"] <= 2, "동시 실행 상한 준수"
    # 결과는 입력 순서대로 (q0..q5 → R:CTX...)
    assert all(r.startswith("R:CTX") for r in results)  # 각 prompt 재시드(CTX 로 시작)
    # alias 가 모두 고유 (독립 세션)
    aliases = [c.kwargs["alias"] for c in client.chat_async.call_args_list]
    assert len(set(aliases)) == 6
    assert all(c.kwargs.get("new_session") is True
               for c in client.chat_async.call_args_list)


def test_fork_many_mismatched_labels_raises():
    """labels 길이가 prompts 와 다르면 zip 절단으로 누락되므로 명시적 거부."""
    import asyncio
    ctx = ContextSession(MagicMock(), "C", provider="claude", owner="u", alias="m")
    with pytest.raises(ValueError):
        asyncio.run(ctx.fork_many(["a", "b", "c"], labels=["only-one"]))


def test_fork_many_respects_explicit_labels():
    import asyncio
    client = MagicMock()
    client.chat_async = AsyncMock(return_value="ok")
    ctx = ContextSession(client, "CTX", provider="claude", owner="u", alias="m")
    asyncio.run(ctx.fork_many(["a", "b"], labels=["formal", "casual"]))
    aliases = [c.kwargs["alias"] for c in client.chat_async.call_args_list]
    assert any("formal" in a for a in aliases)
    assert any("casual" in a for a in aliases)


def test_is_alive_delegates_with_cwd():
    client = MagicMock()
    client.session_alive.return_value = True
    ctx = ContextSession(client, "X", provider="claude", owner="u", alias="a", cwd="/c")
    assert ctx.is_alive() is True
    client.session_alive.assert_called_with("claude", owner="u", alias="a", cwd="/c")


def test_empty_context_raises():
    with pytest.raises(ValueError):
        ContextSession(MagicMock(), "", provider="claude")


def test_pin_context_factory_and_auto_alias():
    client = LLMClient(MemoryStore())
    ctx = client.pin_context("X", provider="claude", owner="u")
    assert isinstance(ctx, ContextSession)
    assert ctx.alias.startswith("ctx-")


def test_refine_async_and_stream_passthrough():
    import asyncio
    client = MagicMock()
    client.session_alive.return_value = True
    client.chat_async = AsyncMock(return_value="RESP")
    ctx = ContextSession(client, "X", provider="claude", owner="u", alias="a")
    assert asyncio.run(ctx.refine_async("q")) == "RESP"
    ctx.refine_stream("q2")
    assert client.chat_stream.called


# ===== 통합: 실제 LLMClient + 페이크 세션 provider =====

class _FakeSessionProvider(LLMProvider):
    provider_id = "fake"
    supports_sessions = True
    supports_session_liveness = True

    def __init__(self):
        self.prompts = []     # (prompt, session_id_in)
        self.alive = False

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        self.prompts.append((messages[-1].content, session_id))
        return LLMResponse(content="ok", provider="fake", model=model,
                           session_id=session_id or "SID-1", tokens=TokenUsage())

    def session_alive(self, session_id, *, cwd=None):
        return bool(session_id) and self.alive

    def list_models(self):
        return []

    def is_available(self):
        return True


def test_integration_refine_reuses_then_reseeds_on_death():
    reg = ProviderRegistry()
    prov = _FakeSessionProvider()
    reg.register(prov)
    reg.set_fallback_order(["fake"])
    client = LLMClient(MemoryStore(), registry=reg)
    ctx = client.pin_context("TRANSCRIPT", provider="fake", owner="u", alias="m")

    ctx.refine("회의록")                             # 세션 없음 → 시드
    assert "TRANSCRIPT" in prov.prompts[-1][0]

    prov.alive = True
    ctx.refine("수정")                               # 살아있음 → resume(전사록 X)
    assert "TRANSCRIPT" not in prov.prompts[-1][0]
    assert prov.prompts[-1][1] == "SID-1"           # 저장된 sid 로 resume

    prov.alive = False
    ctx.refine("다시")                               # 죽음 → 재시드(전사록 다시)
    assert "TRANSCRIPT" in prov.prompts[-1][0]
