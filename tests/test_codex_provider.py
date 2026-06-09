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


def test_parse_jsonl_events_ignores_codex_initial_greeting():
    stdout = '\n'.join([
        '{"type":"thread.started","thread_id":"tid-greeting"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Ready. What would you like me to work on?"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"label\\": \\"news\\"}"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}',
    ])

    result = _parse_jsonl_events(stdout)

    assert result["text"] == '{"label": "news"}'


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
    assert cmd[0].endswith("codex")
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "resume" not in cmd


@patch("agentcli.providers.codex.shutil.which", return_value=r"C:\Users\u\AppData\Roaming\npm\codex.CMD")
def test_build_cmd_uses_resolved_binary_path(mock_which):
    p = CodexProvider()

    cmd = p._build_cmd("hi", "", None, "")

    assert cmd[0] == r"C:\Users\u\AppData\Roaming\npm\codex.CMD"


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_marks_codex_prompt_usage_as_provider_reported(mock_env, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            '{"type":"thread.started","thread_id":"tid-usage"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"A"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":19000,'
            '"cached_input_tokens":4480,"output_tokens":5}}\n'
        ),
        stderr="")
    p = CodexProvider()

    resp = p.invoke([Message(role="user", content="short prompt")])

    assert resp.tokens.prompt_tokens == 19000
    assert resp.tokens.payload_prompt_tokens > 0
    assert resp.tokens.payload_prompt_tokens < resp.tokens.prompt_tokens
    assert resp.tokens.prompt_tokens_reliable is False
    assert resp.tokens.prompt_tokens_source == "codex_cli_reported"


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_retries_once_when_first_turn_is_only_initial_greeting(mock_env, mock_run):
    mock_run.side_effect = [
        MagicMock(
            returncode=0,
            stdout=(
                '{"type":"thread.started","thread_id":"tid-greeting"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"Ready. What would you like me to work on?"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}\n'
            ),
            stderr=""),
        MagicMock(
            returncode=0,
            stdout=(
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}\n'
            ),
            stderr=""),
    ]
    p = CodexProvider()

    resp = p.invoke([Message(role="user", content="real task")])

    assert resp.content == "done"
    assert resp.session_id == "tid-greeting"
    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    assert second_cmd[1:3] == ["exec", "resume"]
    assert "tid-greeting" in second_cmd


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
    assert cmd[0].endswith("codex")
    assert cmd[1:3] == ["exec", "resume"]
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
    """provider 는 client 가 담은 메시지를 충실히 직렬화한다.

    세션 모드에서 이전 턴 미주입은 client 가 [system?, user] 만 담는 것으로
    보장 (test_session_routing). 명시 주입분은 Context 블록으로 전달된다."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        stderr="")
    p = CodexProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="주입된 노트"),
        Message(role="user", content="새 질문"),
    ])
    cmd = mock_run.call_args[0][0]
    # 마지막 인자가 prompt
    prompt = cmd[-1]
    assert "Follow GUIDE v2" in prompt
    assert "새 질문" in prompt
    assert "Context (injected by host application):" in prompt
    assert "[user] 주입된 노트" in prompt


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


# ===== 인자 주입 방어: `--` 구분자 =====

def test_build_cmd_new_session_has_dashdash_before_prompt():
    """prompt 가 `-`로 시작해도 플래그로 해석되지 않도록 `--` 필수."""
    from unittest.mock import patch
    p = CodexProvider()
    with patch.object(CodexProvider, "_find_binary",
                      return_value="/usr/bin/codex"):
        cmd = p._build_cmd("--dangerously-bypass-approvals-and-sandbox",
                           "", None, "")
    assert cmd is not None
    dd = cmd.index("--")
    assert cmd[dd + 1] == "--dangerously-bypass-approvals-and-sandbox", (
        "악성 prompt 는 -- 뒤 위치 인자로만 전달되어야 한다")
    assert cmd[-1] == "--dangerously-bypass-approvals-and-sandbox"


def test_build_cmd_resume_has_dashdash_before_sid_and_prompt():
    from unittest.mock import patch
    p = CodexProvider()
    with patch.object(CodexProvider, "_find_binary",
                      return_value="/usr/bin/codex"):
        cmd = p._build_cmd("-p", "", None, "-malicious-sid")
    assert cmd is not None
    dd = cmd.index("--")
    assert cmd[dd + 1] == "-malicious-sid"
    assert cmd[dd + 2] == "-p"
