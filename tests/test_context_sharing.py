"""컨텍스트 공유 기능 테스트."""
from datetime import datetime
from agentcli.types import Message
from agentcli.store.memory import MemoryStore
from agentcli.store.sqlite import SQLiteStore
from agentcli.client import LLMClient
from tests.conftest import MockProvider
from agentcli.providers.registry import ProviderRegistry


# --- Message.agent ---

def test_message_agent_field():
    m = Message(role="user", content="hello", agent="bull-analyst")
    assert m.agent == "bull-analyst"


def test_message_agent_default():
    m = Message(role="user", content="hello")
    assert m.agent == ""


# --- MemoryStore agent filter ---

def test_memory_store_agent_filter():
    store = MemoryStore()
    conv = store.create("team", "claude")
    store.add_message(conv.id, Message(role="user", content="강세", agent="bull"))
    store.add_message(conv.id, Message(role="user", content="약세", agent="bear"))
    store.add_message(conv.id, Message(role="user", content="결정", agent="trader"))

    bull_msgs = store.get_messages(conv.id, agent="bull")
    assert len(bull_msgs) == 1
    assert bull_msgs[0].content == "강세"

    all_msgs = store.get_messages(conv.id)
    assert len(all_msgs) == 3


def test_memory_store_agent_filter_with_limit():
    store = MemoryStore()
    conv = store.create("team", "claude")
    for i in range(5):
        store.add_message(conv.id, Message(role="user", content=f"msg-{i}", agent="a"))
    store.add_message(conv.id, Message(role="user", content="other", agent="b"))

    msgs = store.get_messages(conv.id, limit=3, agent="a")
    assert len(msgs) == 3
    assert all(m.agent == "a" for m in msgs)


# --- SQLiteStore agent filter ---

def test_sqlite_store_agent_column():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.add_message(conv.id, Message(role="user", content="hello", agent="agent-1"))

    msgs = store.get_messages(conv.id)
    assert len(msgs) == 1
    assert msgs[0].agent == "agent-1"


def test_sqlite_store_agent_filter():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.add_message(conv.id, Message(role="user", content="A says", agent="a"))
    store.add_message(conv.id, Message(role="user", content="B says", agent="b"))

    a_msgs = store.get_messages(conv.id, agent="a")
    assert len(a_msgs) == 1
    assert a_msgs[0].content == "A says"


# --- LLMClient context sharing ---

def _make_client(store):
    provider = MockProvider()
    reg = ProviderRegistry()
    reg.register(provider)
    reg.set_fallback_order(["mock"])
    return LLMClient(store=store, registry=reg), provider


def test_client_chat_with_agent():
    store = MemoryStore()
    client, provider = _make_client(store)
    resp = client.chat("hello", provider="mock", owner="team", agent="bull")

    msgs = store.get_messages(resp.conversation_id)
    assert msgs[0].agent == "bull"  # user message
    assert msgs[1].agent == "bull"  # assistant response


def test_client_inject_context():
    store = MemoryStore()
    client, provider = _make_client(store)

    # 대화 A: bull이 분석
    resp_a = client.chat("시장 강세", provider="mock", owner="team", agent="bull")
    conv_a = resp_a.conversation_id

    # 대화 B: bear가 bull의 대화를 참조
    resp_b = client.chat("시장 약세", provider="mock", owner="team", agent="bear",
                         inject_context=[{"conversation_id": conv_a, "limit": 10}])

    # bear의 프롬프트에 bull의 메시지가 포함되었는지 확인
    sent_to_provider = provider.last_messages
    contents = [m.content for m in sent_to_provider]
    assert "시장 강세" in contents  # bull의 메시지가 주입됨
    assert "시장 약세" in contents  # bear 자신의 메시지


def test_client_inject_context_with_agent_filter():
    store = MemoryStore()
    client, provider = _make_client(store)

    # 공유 대화에 두 에이전트가 참여
    resp1 = client.chat("강세 분석", provider="mock", owner="team", agent="bull")
    conv_id = resp1.conversation_id
    client.chat("약세 분석", provider="mock", owner="team", agent="bear",
                conversation_id=conv_id)

    # 새 대화에서 bull의 메시지만 주입
    resp3 = client.chat("결정", provider="mock", owner="team", agent="trader",
                        inject_context=[{"conversation_id": conv_id, "agent": "bull", "limit": 10}])

    sent = provider.last_messages
    injected_contents = [m.content for m in sent if m.agent == "bull"]
    assert len(injected_contents) > 0  # bull 메시지가 주입됨


def test_shared_conversation_multiple_agents():
    """여러 에이전트가 하나의 대화에 참여."""
    store = MemoryStore()
    client, provider = _make_client(store)

    # 같은 conversation_id로 여러 에이전트가 메시지 추가
    resp1 = client.chat("강세입니다", provider="mock", owner="team", agent="bull")
    conv_id = resp1.conversation_id

    client.chat("약세입니다", provider="mock", owner="team", agent="bear",
                conversation_id=conv_id)
    client.chat("매수합니다", provider="mock", owner="team", agent="trader",
                conversation_id=conv_id)

    all_msgs = store.get_messages(conv_id)
    agents = [m.agent for m in all_msgs]
    assert "bull" in agents
    assert "bear" in agents
    assert "trader" in agents

    # 에이전트별 필터링
    bull_only = store.get_messages(conv_id, agent="bull")
    assert all(m.agent == "bull" for m in bull_only)
