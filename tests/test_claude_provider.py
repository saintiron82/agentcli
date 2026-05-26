from unittest.mock import patch, MagicMock
import subprocess
from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message


def test_is_available_found():
    with patch("shutil.which", return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        assert p.is_available()


def test_is_available_not_found():
    with patch("shutil.which", return_value=None):
        p = ClaudeProvider()
        assert not p.is_available()


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value=None)
def test_health_check_binary_missing(mock_find):
    h = ClaudeProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"
    assert h.suggested_action


@patch("agentcli.providers.claude.run_health_command")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_health_check_auth_required(mock_find, mock_run_health):
    mock_run_health.side_effect = [
        subprocess.CompletedProcess(["claude", "--version"], 0,
                                    stdout="2.1.126", stderr=""),
        subprocess.CompletedProcess(["claude", "auth", "status"], 1,
                                    stdout="", stderr="not authenticated"),
    ]
    h = ClaudeProvider().health_check()
    assert h.ok is False
    assert h.status == "auth_required"
    assert h.error_type == "auth"


def test_list_models():
    p = ClaudeProvider()
    models = p.list_models()
    assert len(models) >= 3
    assert any(m["id"] == "sonnet" for m in models)
    assert any(m["id"] == "claude-opus-4-7" for m in models)
    assert any(m["id"] == "claude-sonnet-4-6" for m in models)
    assert any(m["id"] == "claude-haiku-4-5" for m in models)
    assert p.resolve_model("claude-sonnet-4-6", strict=True) == "claude-sonnet-4-6"


def test_provider_id():
    p = ClaudeProvider()
    assert p.provider_id == "claude"
    assert p.supports_sessions is True


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_success(mock_find, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"result":"응답입니다","usage":{"input_tokens":300,"output_tokens":200}}',
        stderr="")
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hello")])
    assert resp.content == "응답입니다"
    assert resp.tokens.prompt_tokens == 300
    assert resp.tokens.completion_tokens == 200
    assert resp.tokens.total_tokens == 500
    assert resp.provider == "claude"
    assert resp.session_id  # 신규 session_id 발급됨
    cmd = mock_run.call_args[0][0]
    assert "-p" in cmd
    assert "--output-format" in cmd
    # 신규 세션: --session-id가 붙어야 함
    assert "--session-id" in cmd


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_marks_claude_prompt_usage_as_provider_reported(mock_find, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"result":"응답입니다","usage":{"input_tokens":6,"output_tokens":2}}',
        stderr="")
    p = ClaudeProvider()

    resp = p.invoke([Message(role="user", content="longer user prompt than six tokens")])

    assert resp.tokens.prompt_tokens == 6
    assert resp.tokens.payload_prompt_tokens > 0
    assert resp.tokens.prompt_tokens_reliable is False
    assert resp.tokens.prompt_tokens_source == "claude_cli_reported"


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_resume_session(mock_find, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout='{"result":"ok"}', stderr="")
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc-123")
    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "abc-123"
    assert resp.session_id == "abc-123"
    # 재개 시에는 --session-id 가 없어야 함
    assert "--session-id" not in cmd


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_non_json_stdout_fallback(mock_find, mock_run):
    """JSON 파싱 실패 시 stdout 전체를 content로 취급 (하위 호환)."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout="plain text response", stderr="")
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == "plain text response"
    assert resp.tokens.total_tokens == 0


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_cwd_and_tools(mock_find, mock_run):
    """cwd + allowed_tools/disallowed_tools 전달 검증."""
    mock_run.return_value = MagicMock(returncode=0, stdout='{"result":"ok"}', stderr="")
    p = ClaudeProvider(
        permission_mode="default",
        allowed_tools=["Read", "Grep"],
        disallowed_tools=["Bash"])
    p.invoke([Message(role="user", content="hi")], cwd="/repo")
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert "--permission-mode" in cmd
    pidx = cmd.index("--permission-mode")
    assert cmd[pidx + 1] == "default"
    assert "--allowedTools" in cmd
    aidx = cmd.index("--allowedTools")
    assert cmd[aidx + 1] == "Read,Grep"
    assert "--disallowedTools" in cmd
    didx = cmd.index("--disallowedTools")
    assert cmd[didx + 1] == "Bash"
    assert kwargs.get("cwd") == "/repo"


def test_invoke_async_via_base_fallback():
    """base 기본 구현: invoke_async는 to_thread로 동기 invoke를 실행."""
    import asyncio
    from unittest.mock import patch, MagicMock
    with patch("agentcli.providers.claude.subprocess.run") as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"result":"async-ok"}', stderr="")
        # 직접 async 함수를 테스트: Claude는 invoke_async 오버라이드했지만
        # asyncio.create_subprocess_exec 모킹이 까다로우니 여기서는
        # 기본 fallback이 아닌 진짜 async 경로는 integration 단에서 검증.
        # 여기서는 sync invoke가 정상 동작하는지만 재확인.
        p = ClaudeProvider()
        resp = p.invoke([Message(role="user", content="x")])
        assert resp.content == "async-ok"


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_with_model(mock_find, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="hi")], model="sonnet")
    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "sonnet"


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_includes_system_prompt_but_not_history(mock_find, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    p = ClaudeProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="new question"),
    ])
    cmd = mock_run.call_args[0][0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Follow GUIDE v2" in prompt
    assert "new question" in prompt
    assert "old question" not in prompt
    assert "old answer" not in prompt


@patch("agentcli.providers.claude.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120))
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_timeout(mock_find, mock_run):
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value=None)
def test_invoke_not_found(mock_find):
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error
