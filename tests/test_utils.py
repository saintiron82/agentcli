from datetime import datetime
from unittest.mock import patch, MagicMock
from agentcli.utils import (
    parse_tokens, serialize_messages, build_env, clear_gh_token_cache)
from agentcli.types import Message


def test_parse_tokens_codex_format():
    assert parse_tokens("tokens used\n10,798") == 10798


def test_parse_tokens_claude_format():
    assert parse_tokens("Total tokens: 12345") == 12345


def test_parse_tokens_empty():
    assert parse_tokens("") == 0
    assert parse_tokens("random text") == 0


def test_serialize_messages():
    msgs = [
        Message(role="system", content="You are a trader", timestamp=datetime.now()),
        Message(role="user", content="분석해줘", timestamp=datetime.now()),
        Message(role="assistant", content="시장 상승세입니다", timestamp=datetime.now()),
    ]
    text = serialize_messages(msgs)
    assert "[system] You are a trader" in text
    assert "[user] 분석해줘" in text
    assert "[assistant] 시장 상승세입니다" in text


def test_serialize_empty():
    assert serialize_messages([]) == ""


def test_build_env():
    env = build_env()
    assert isinstance(env, dict)
    assert "PATH" in env


def test_serialize_messages_with_agent():
    """agent 태그가 있으면 [role:agent] 형식으로 직렬화."""
    msgs = [
        Message(role="user", content="강세 논거", timestamp=datetime.now(),
                agent="bull"),
        Message(role="user", content="약세 논거", timestamp=datetime.now(),
                agent="bear"),
    ]
    text = serialize_messages(msgs)
    assert "[user:bull] 강세 논거" in text
    assert "[user:bear] 약세 논거" in text


def test_build_env_caches_gh_token(monkeypatch):
    """gh auth token 은 프로세스 수명 동안 1회만 호출된다."""
    clear_gh_token_cache()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    call_count = {"n": 0}
    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        return MagicMock(stdout="ghp_fake_token\n", returncode=0)

    with patch("agentcli.utils.subprocess.run", side_effect=fake_run):
        env1 = build_env()
        env2 = build_env()
        env3 = build_env()

    assert call_count["n"] == 1
    assert env1.get("GITHUB_TOKEN") == "ghp_fake_token"
    assert env2.get("GITHUB_TOKEN") == "ghp_fake_token"
    assert env3.get("GITHUB_TOKEN") == "ghp_fake_token"
    clear_gh_token_cache()
