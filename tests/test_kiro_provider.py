from unittest.mock import patch
from agentcli.providers.kiro import KiroProvider
from agentcli.types import TokenUsage
from agentcli.providers.kiro import _map_session_update


def test_provider_id_and_capabilities():
    p = KiroProvider()
    assert p.provider_id == "kiro"
    assert p.supports_sessions is True
    assert p.supports_streaming is True
    assert p.stores_history is False


def test_list_models_has_default_passthrough():
    models = KiroProvider().list_models()
    assert any(m["id"] == "" for m in models)  # 빈 id = 기본
    # resolve_model 은 알 수 없는 selector 를 그대로 통과 (비-strict).
    assert KiroProvider().resolve_model("kiro-some-model") == "kiro-some-model"


@patch("agentcli.providers.kiro.shutil.which", return_value=None)
def test_health_check_binary_missing(mock_which):
    h = KiroProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"
    assert h.error_type == "binary_missing"


def test_map_agent_message_chunk_to_text():
    u = TokenUsage()
    chunks = _map_session_update(
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hello"}}, u)
    assert len(chunks) == 1
    assert chunks[0].type == "text" and chunks[0].content == "Hello"


def test_map_thought_and_tool_variants():
    u = TokenUsage()
    assert _map_session_update(
        {"sessionUpdate": "agent_thought_chunk",
         "content": {"type": "text", "text": "thinking..."}}, u)[0].type == "thinking"
    assert _map_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "t1",
         "title": "read"}, u)[0].type == "tool_use"
    assert _map_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
         "status": "completed"}, u)[0].type == "tool_result"


def test_map_usage_update_accumulates_and_emits_nothing():
    u = TokenUsage()
    out = _map_session_update(
        {"sessionUpdate": "usage_update", "used": 1500, "size": 200000}, u)
    assert out == []
    assert u.prompt_tokens == 1500
    assert u.prompt_tokens_source == "kiro_cli_reported"
    assert u.prompt_tokens_reliable is False
