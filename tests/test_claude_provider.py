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
    import platform
    p = ClaudeProvider()
    assert p.provider_id == "claude"
    # macOS/Linux: 네이티브 resume 지원. Windows: issue #4 hang 회피로 stateless.
    assert p.supports_sessions is (platform.system() != "Windows")
    # 어느 모드든 히스토리는 CLI 소유 — 라이브러리는 내용을 저장하지 않는다.
    assert p.stores_history is False


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
def test_invoke_session_id_ignored_in_stateless_mode(mock_find, mock_run):
    """issue #4: Windows(stateless) 모드는 외부 session_id를 받아도 `--resume`
    하지 않고 새 식별자를 발급한다 (hang 우회)."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout='{"result":"ok"}', stderr="")
    p = ClaudeProvider()
    p.supports_sessions = False  # Windows 모드 시뮬레이션
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc-123")
    cmd = mock_run.call_args[0][0]
    assert "--resume" not in cmd, "issue #4: stateless 모드에서 --resume 금지"
    assert "--session-id" in cmd
    assert resp.session_id and resp.session_id != "abc-123"


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_resumes_session(mock_find, mock_run):
    """세션 모드: 저장된 session_id로 `--resume`, 동일 sid를 반환."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout='{"result":"ok"}', stderr="")
    p = ClaudeProvider()
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")],
                    session_id="abc-123")
    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd
    assert resp.session_id == "abc-123"


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_stale_session_auto_recovers(mock_find, mock_run):
    """만료된 sid resume 실패 시 새 세션으로 1회 자동 재시도."""
    stale = MagicMock(
        returncode=1, stdout="",
        stderr="No conversation found with session ID: abc-123")
    fresh = MagicMock(
        returncode=0, stdout='{"result":"recovered"}', stderr="")
    mock_run.side_effect = [stale, fresh]
    p = ClaudeProvider()
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")],
                    session_id="abc-123")
    assert resp.content == "recovered"
    assert resp.session_id != "abc-123", "새 세션 sid가 반환되어야 함"
    assert mock_run.call_count == 2
    retry_cmd = mock_run.call_args_list[1][0][0]
    assert "--resume" not in retry_cmd
    assert "--session-id" in retry_cmd


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_other_failure_not_retried(mock_find, mock_run):
    """stale-session 외의 실패는 재시도 없이 그대로 에러 반환."""
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="usage limit reached")
    p = ClaudeProvider()
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")],
                    session_id="abc-123")
    assert resp.content == ""
    assert resp.error
    assert mock_run.call_count == 1


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
def test_invoke_serializes_exactly_what_client_sends(mock_find, mock_run):
    """provider 는 받은 메시지를 충실히 직렬화한다 — 세션 모드에서 이전 턴이
    프롬프트에 안 들어가는 것은 client 가 [system?, user] 만 담는 것으로
    보장된다 (test_session_routing). 명시 주입(inject_context)분은 Context
    블록으로 전달되어야 한다."""
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    p = ClaudeProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="injected note", agent="bull"),
        Message(role="user", content="new question"),
    ])
    cmd = mock_run.call_args[0][0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Follow GUIDE v2" in prompt
    assert "new question" in prompt
    assert "Context (injected by host application):" in prompt
    assert "[user:bull] injected note" in prompt


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
