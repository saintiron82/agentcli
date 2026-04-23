from datetime import datetime
from agentcli.types import TokenUsage, Message, LLMResponse, Conversation


def test_token_usage_defaults():
    t = TokenUsage()
    assert t.prompt_tokens == 0
    assert t.completion_tokens == 0
    assert t.total_tokens == 0


def test_token_usage_with_values():
    t = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert t.total_tokens == 150


def test_message_creation():
    now = datetime.now()
    m = Message(role="user", content="hello", timestamp=now)
    assert m.role == "user"
    assert m.content == "hello"
    assert m.metadata == {}


def test_message_with_metadata():
    m = Message(role="assistant", content="hi",
                timestamp=datetime.now(),
                metadata={"provider": "claude", "tokens": 100})
    assert m.metadata["provider"] == "claude"


def test_llm_response():
    tokens = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    r = LLMResponse(content="answer", provider="claude", model="sonnet",
                    tokens=tokens, latency_ms=500)
    assert r.content == "answer"
    assert r.tokens.total_tokens == 150
    assert r.raw_stderr == ""
    assert r.conversation_id == ""


def test_conversation():
    now = datetime.now()
    c = Conversation(id="abc", owner="bot1", provider="claude", model="sonnet",
                     created_at=now, updated_at=now)
    assert c.id == "abc"
    assert c.messages == []
    assert c.metadata == {}
    c.messages.append(Message(role="user", content="hi", timestamp=now))
    assert len(c.messages) == 1
