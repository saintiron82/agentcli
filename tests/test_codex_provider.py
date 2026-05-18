from unittest.mock import patch, MagicMock
import subprocess
from agentcli.providers.codex import CodexProvider, _parse_jsonl_events
from agentcli.types import Message


def test_provider_id_and_capabilities():
    p = CodexProvider()
    assert p.provider_id == "codex"
    assert p.supports_sessions is True
    assert p.supports_streaming is True


def test_list_models():
    p = CodexProvider()
    models = p.list_models()
    assert any(m["id"] == "gpt-5.3-codex" for m in models)
    assert any(m["id"] == "gpt-5.2-codex" for m in models)
    assert any(m["id"] == "gpt-5.1-codex-max" for m in models)
    assert any(m["id"] == "gpt-5.5" for m in models)
    assert any(m["id"] == "gpt-5.4-mini" for m in models)
    assert any(m["id"] == "o4-mini" for m in models)


@patch("agentcli.providers.codex.shutil.which", return_value=None)
def test_health_check_binary_missing(mock_which):
    h = CodexProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"


@patch("agentcli.providers.codex.run_health_command")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.codex.shutil.which", return_value="/usr/bin/codex")
def test_health_check_ok(mock_which, mock_env, mock_run_health):
    mock_run_health.side_effect = [
        subprocess.CompletedProcess(["codex", "--version"], 0,
                                    stdout="codex-cli 0.128.0", stderr=""),
        subprocess.CompletedProcess(["codex", "login", "status"], 0,
                                    stdout="Logged in", stderr=""),
    ]
    h = CodexProvider().health_check()
    assert h.ok is True
    assert h.auth_ok is True


# ===== JSONL 파싱 =====

def test_parse_jsonl_events_extracts_all():
    stdout = '\n'.join([
        '{"type":"thread.started","thread_id":"abc-123"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Hello "}}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"world"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":50}}',
    ])
    result = _parse_jsonl_events(stdout)
    assert result["thread_id"] == "abc-123"
    assert result["text"] == "Hello world"
    assert result["usage"].prompt_tokens == 100
    assert result["usage"].completion_tokens == 50
    assert result["usage"].total_tokens == 150
    assert result["usage"].cached_tokens == 20


def test_parse_jsonl_events_ignores_non_json_lines():
    stdout = (
        'Reading additional input from stdin...\n'
        '{"type":"thread.started","thread_id":"xyz"}\n'
        'random log line\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
    )
    result = _parse_jsonl_events(stdout)
    assert result["thread_id"] == "xyz"
    assert result["text"] == "ok"


# ===== invoke (신규 세션) =====

@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_new_session(mock_env, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            '{"type":"thread.started","thread_id":"tid-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"A"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
        ),
        stderr="")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == "A"
    assert resp.session_id == "tid-1"
    assert resp.tokens.prompt_tokens == 10
    assert resp.tokens.completion_tokens == 5
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "resume" not in cmd


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_resume_session(mock_env, mock_run):
    """session_id 전달 시 `codex exec resume <sid>` 사용."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"B"}}\n',
        stderr="")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="follow")], session_id="tid-existing")
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert "tid-existing" in cmd
    # resume에는 -s sandbox 옵션이 없어야 함
    assert "-s" not in cmd
    # session_id가 응답에 보존 (신규 thread.started 없으면 원 session_id 유지)
    assert resp.session_id == "tid-existing"


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_cwd_and_sandbox(mock_env, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        stderr="")
    p = CodexProvider(sandbox_mode="workspace-write", full_auto=False)
    p.invoke([Message(role="user", content="hi")], cwd="/repo")
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert "--full-auto" not in cmd
    assert "-s" in cmd and "workspace-write" in cmd
    assert "-C" in cmd
    assert cmd[cmd.index("-C") + 1] == "/repo"
    assert kwargs.get("cwd") == "/repo"


@patch("agentcli.providers.codex.subprocess.run",
       side_effect=FileNotFoundError)
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_not_found(mock_env, mock_run):
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_uses_system_and_last_message_only(mock_env, mock_run):
    """세션 provider: system 지시는 주입하되 이전 턴은 재직렬화하지 않는다."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        stderr="")
    p = CodexProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="이전 질문"),
        Message(role="assistant", content="이전 답변"),
        Message(role="user", content="새 질문"),
    ])
    cmd = mock_run.call_args[0][0]
    # 마지막 인자가 prompt
    prompt = cmd[-1]
    assert "Follow GUIDE v2" in prompt
    assert "새 질문" in prompt
    # 이전 히스토리는 들어가면 안 됨
    assert "이전 질문" not in prompt
    assert "이전 답변" not in prompt


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_nonzero_returns_error(mock_env, mock_run):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="HTTP 401 unauthorized")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert "unauthorized" in resp.error
    assert resp.error_type == "auth"


@patch("agentcli.providers.codex.subprocess.run",
       side_effect=subprocess.TimeoutExpired("cmd", 120))
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_timeout(mock_env, mock_run):
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124
