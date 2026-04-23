from unittest.mock import patch, MagicMock
import subprocess
from agentcli.providers.copilot import CopilotProvider, _parse_copilot_jsonl
from agentcli.types import Message


def test_provider_id():
    p = CopilotProvider()
    assert p.provider_id == "copilot"
    assert p.supports_sessions is True
    assert p.supports_streaming is True


def test_list_models():
    p = CopilotProvider()
    models = p.list_models()
    assert any(m["id"] == "gpt-4o" for m in models)


# ===== JSONL 파싱 =====

_SAMPLE_STDOUT = '\n'.join([
    '{"type":"session.mcp_servers_loaded","data":{"servers":[]},"id":"a","timestamp":"T"}',
    '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"Hello "},"id":"d1","timestamp":"T"}',
    '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"world"},"id":"d2","timestamp":"T"}',
    '{"type":"assistant.message","data":{"messageId":"m1","content":"Hello world","outputTokens":2}}',
    '{"type":"assistant.turn_end","data":{"turnId":"0"}}',
    '{"type":"result","sessionId":"SID-COPILOT-1","exitCode":0,"usage":{"premiumRequests":1}}',
])


def test_parse_extracts_text_session_id_and_tokens():
    r = _parse_copilot_jsonl(_SAMPLE_STDOUT)
    assert r["text"] == "Hello world"
    assert r["session_id"] == "SID-COPILOT-1"
    assert r["usage"].completion_tokens == 2
    assert r["usage"].total_tokens == 2


def test_parse_fallback_to_final_message_when_no_delta():
    stdout = '\n'.join([
        '{"type":"assistant.message","data":{"content":"final only","outputTokens":5}}',
        '{"type":"result","sessionId":"S2","exitCode":0}',
    ])
    r = _parse_copilot_jsonl(stdout)
    assert r["text"] == "final only"
    assert r["usage"].completion_tokens == 5


def test_parse_ignores_malformed_lines():
    stdout = (
        'random noise\n'
        '{"type":"result","sessionId":"S3"}\n'
    )
    r = _parse_copilot_jsonl(stdout)
    assert r["session_id"] == "S3"


# ===== invoke (신규 세션) =====

@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_new_session_parses_result_sessionid(mock_find, mock_env, mock_run):
    """sessionId가 없는 호출 → result 이벤트의 sessionId를 session_id로 반환."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="hi")])

    assert resp.content == "Hello world"
    assert resp.session_id == "SID-COPILOT-1"
    assert resp.tokens.completion_tokens == 2

    cmd = mock_run.call_args[0][0]
    # --output-format json 적용
    assert "--output-format" in cmd
    assert "json" in cmd
    # 신규 세션은 --resume 없음
    assert not any(c.startswith("--resume") for c in cmd)


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_resume_returns_real_session_id(mock_find, mock_env, mock_run):
    """사용자가 준 session_id로 resume → result의 sessionId (동일/갱신)를 반환."""
    stdout = '\n'.join([
        '{"type":"assistant.message","data":{"content":"back","outputTokens":1}}',
        '{"type":"result","sessionId":"SID-COPILOT-1"}',
    ])
    mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="x")],
                     session_id="SID-COPILOT-1")
    assert resp.session_id == "SID-COPILOT-1"
    cmd = mock_run.call_args[0][0]
    assert "--resume=SID-COPILOT-1" in cmd


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_alias_becomes_name_and_resume(mock_find, mock_env, mock_run):
    """alias만 있을 때: --name + --resume=<alias> 둘 다 전달."""
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider()
    p.invoke([Message(role="user", content="hi")], alias="bull-agent")
    cmd = mock_run.call_args[0][0]
    assert "--name=bull-agent" in cmd
    # alias 기반 resume 시도
    assert "--resume=bull-agent" in cmd


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_tools_and_cwd(mock_find, mock_env, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider(
        allow_all_tools=False,
        allowed_tools=["Read", "Grep"],
        disallowed_tools=["Bash"],
        add_dirs=["/tmp"],
        effort="medium")
    p.invoke([Message(role="user", content="hi")], cwd="/repo")
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert "--allow-all-tools" not in cmd
    assert "Read" in cmd and "Grep" in cmd
    assert "--deny-tool" in cmd and "Bash" in cmd
    assert "/tmp" in cmd
    assert "--effort" in cmd and "medium" in cmd
    assert kwargs.get("cwd") == "/repo"


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_invoke_not_found(mock_find):
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""


@patch("agentcli.providers.copilot.subprocess.run",
       side_effect=subprocess.TimeoutExpired("cmd", 120))
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_timeout(mock_find, mock_env, mock_run):
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
