"""같은 conversation 동시 호출 직렬화 회귀 테스트.

직렬화 없이는: 두 호출이 같은 (비어 있는) session_id 를 읽고 각자 새 CLI
세션을 만든 뒤, 마지막 set_metadata 가 다른 쪽 세션을 덮어써 한쪽 세션이
참조 없는 상태로 남는다. per-conversation 잠금 + 잠금 후 session_id 재조회로
두 번째 호출이 첫 호출의 세션을 resume 하도록 보장한다.
"""

import asyncio
import threading
import time

from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import LLMResponse, TokenUsage


class SlowSessionProvider(LLMProvider):
    provider_id = "slowsess"
    supports_sessions = True

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.received_sids: list[str] = []
        self._counter = 0
        self._counter_lock = threading.Lock()

    def _respond(self, session_id: str, model: str) -> LLMResponse:
        with self._counter_lock:
            self._counter += 1
            n = self._counter
        sid = session_id or f"sid-{n}"
        return LLMResponse(
            content=f"reply-{n}", provider=self.provider_id, model=model,
            tokens=TokenUsage(prompt_tokens=1, completion_tokens=1,
                              total_tokens=2),
            latency_ms=1, session_id=sid)

    def invoke(self, messages, *, model="", timeout=120,
               session_id="", cwd=None):
        self.received_sids.append(session_id)
        time.sleep(self.delay)
        return self._respond(session_id, model)

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None):
        self.received_sids.append(session_id)
        await asyncio.sleep(self.delay)
        return self._respond(session_id, model)

    def list_models(self):
        return [{"id": "", "name": "default"}]

    def is_available(self):
        return True


def _make_client(provider) -> LLMClient:
    reg = ProviderRegistry()
    reg.register(provider)
    reg.set_fallback_order([provider.provider_id])
    return LLMClient(store=MemoryStore(), registry=reg)


def test_async_same_alias_concurrent_calls_share_one_session():
    p = SlowSessionProvider()
    client = _make_client(p)

    async def run():
        return await asyncio.gather(
            client.chat_async("first", provider="slowsess",
                              owner="o", alias="agent-x"),
            client.chat_async("second", provider="slowsess",
                              owner="o", alias="agent-x"))

    r1, r2 = asyncio.run(run())
    assert r1.content and r2.content
    # 같은 alias 의 동시 호출은 하나의 세션을 공유해야 한다.
    assert r1.session_id == r2.session_id
    # 첫 호출은 새 세션(""), 두 번째 호출은 첫 호출의 sid 로 resume.
    assert p.received_sids == ["", "sid-1"]


def test_sync_same_alias_concurrent_threads_share_one_session():
    p = SlowSessionProvider()
    client = _make_client(p)
    results: list[LLMResponse] = []
    errors: list[Exception] = []

    def call(prompt: str):
        try:
            results.append(client.chat(
                prompt, provider="slowsess", owner="o", alias="agent-y"))
        except Exception as exc:  # noqa: BLE001 — 테스트 수집용
            errors.append(exc)

    t1 = threading.Thread(target=call, args=("first",))
    t2 = threading.Thread(target=call, args=("second",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert len(results) == 2
    assert results[0].session_id == results[1].session_id
    assert p.received_sids == ["", "sid-1"]


def test_async_different_aliases_run_concurrently():
    """다른 conversation 끼리는 직렬화되지 않는다 (병렬성 유지)."""
    p = SlowSessionProvider(delay=0.1)
    client = _make_client(p)

    async def run():
        start = time.monotonic()
        await asyncio.gather(
            client.chat_async("a", provider="slowsess",
                              owner="o", alias="agent-1"),
            client.chat_async("b", provider="slowsess",
                              owner="o", alias="agent-2"))
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    # 직렬이면 ≥0.2s — 병렬이면 ~0.1s. 여유를 두고 0.18s 미만 확인.
    assert elapsed < 0.18, f"다른 alias 호출이 직렬화됨: {elapsed:.3f}s"


def test_async_lock_does_not_leak_across_event_loops():
    """asyncio.run 을 반복 호출해도 (loop 교체) 잠금이 재사용 가능해야 한다."""
    p = SlowSessionProvider(delay=0.0)
    client = _make_client(p)

    r1 = asyncio.run(client.chat_async(
        "first", provider="slowsess", owner="o", alias="agent-z"))
    r2 = asyncio.run(client.chat_async(
        "second", provider="slowsess", owner="o", alias="agent-z"))
    assert r1.session_id == r2.session_id
    assert p.received_sids == ["", "sid-1"]
