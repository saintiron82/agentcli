from datetime import datetime
import agentcli
from agentcli.types import (ERROR_AUTH, ERROR_USAGE_LIMIT, Conversation,
                            LLMResponse, Message, ProviderHealth, StreamChunk,
                            TokenUsage, classify_error, make_error_chunk,
                            standardize_error_chunk)


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
    assert r.exit_code is None
    assert r.recoverable is False
    assert r.suggested_action == ""


def test_llm_response_structured_error_defaults():
    r = LLMResponse(content="", provider="claude", model="",
                    error="HTTP 401 Unauthorized")
    assert r.error_type == ERROR_AUTH
    assert r.recoverable is False
    assert "claude auth login" in r.suggested_action


def test_llm_response_usage_limit_is_recoverable():
    r = LLMResponse(content="", provider="codex", model="",
                    error="rate limit exceeded")
    assert r.error_type == ERROR_USAGE_LIMIT
    assert r.recoverable is True
    assert "quota" in r.suggested_action


def test_provider_health_suggests_auth_action():
    h = ProviderHealth(provider="codex", ok=False, status="auth_required",
                       error_type=ERROR_AUTH, message="not logged in")
    assert h.suggested_action


def test_provider_health_public_dict_masks_raw_payload():
    h = ProviderHealth(
        provider="claude", ok=False, status="auth_required",
        available=True, binary="/Users/me/.local/bin/claude",
        version="claude 1.0 user@example.com",
        auth_ok=False, error_type=ERROR_AUTH,
        message="auth failed for user@example.com token pypi-abcdef",
        raw_stdout='{"email":"user@example.com"}',
        raw_stderr="secret")

    public = h.public_dict()
    assert "raw_stdout" not in public
    assert "raw_stderr" not in public
    assert public["binary"] == "claude"
    assert "user@example.com" not in public["message"]
    assert "pypi-abcdef" not in public["message"]


def test_stream_error_helper_standardizes_required_fields():
    chunk = make_error_chunk("please log in", provider="claude")
    assert chunk.type == "error"
    assert chunk.data["provider"] == "claude"
    assert chunk.data["error_type"] == ERROR_AUTH
    assert chunk.data["recoverable"] is False
    assert chunk.data["suggested_action"]
    assert "exit_code" in chunk.data

    raw = StreamChunk(type="error", content="rate limit")
    normalized = standardize_error_chunk(raw, provider="codex")
    assert normalized.data["provider"] == "codex"
    assert normalized.data["error_type"] == ERROR_USAGE_LIMIT


def test_classify_login_required_as_auth():
    assert classify_error("not logged in") == ERROR_AUTH
    assert classify_error("please log in to continue") == ERROR_AUTH


def test_package_exposes_version():
    assert agentcli.__version__


def test_conversation():
    now = datetime.now()
    c = Conversation(id="abc", owner="bot1", provider="claude", model="sonnet",
                     created_at=now, updated_at=now)
    assert c.id == "abc"
    assert c.messages == []
    assert c.metadata == {}
    c.messages.append(Message(role="user", content="hi", timestamp=now))
    assert len(c.messages) == 1
