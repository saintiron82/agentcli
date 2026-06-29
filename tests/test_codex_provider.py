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

@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_debug_writes_invoke_trace(mock_env, mock_run, mock_find, tmp_path):
    """#29: codex 비스트리밍 invoke 도 debug trace(phase=invoke) 를 남긴다."""
    import json
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"A"}}\n',
        stderr="codex debug stderr")
    trace = tmp_path / "codex_invoke.jsonl"
    resp = CodexProvider().invoke(
        [Message(role="user", content="hi")],
        debug=True, debug_log_path=str(trace))
    assert resp.content == "A"
    rec = json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "invoke" and rec["provider"] == "codex"
    assert rec["returncode"] == 0
    assert rec["schema"] == 1 and len(rec["call_id"]) == 12
    assert "codex debug stderr" in rec["stderr"]


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_new_session(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_stale_session_auto_recovers(mock_env, mock_run, mock_find):
    """죽은 thread(resume 실패: 'no rollout found') → 새 세션으로 1회 자동 재시도."""
    stale = MagicMock(
        returncode=1, stdout="",
        stderr="Error: thread/resume failed: no rollout found for thread id abc (code -32600)")
    fresh = MagicMock(
        returncode=0,
        stdout=('{"type":"thread.started","thread_id":"tid-new"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"recovered"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'),
        stderr="")
    mock_run.side_effect = [stale, fresh]
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc")
    assert resp.content == "recovered"
    assert resp.session_id == "tid-new", "새 세션 thread_id 반환"
    assert mock_run.call_count == 2
    # 1차는 resume, 2차(재시도)는 새 세션(resume 아님)
    assert "resume" in mock_run.call_args_list[0][0][0]
    assert "resume" not in mock_run.call_args_list[1][0][0]


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_other_resume_failure_not_retried(mock_env, mock_run, mock_find):
    """stale 외의 resume 실패는 재시도 없이 에러 반환."""
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="some other error")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc")
    assert resp.error
    assert mock_run.call_count == 1


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_stale_recovery_is_bounded(mock_env, mock_run, mock_find):
    """재시도(새 세션)도 stale 면 무한 루프 없이 1회로 끝낸다."""
    stale = MagicMock(returncode=1, stdout="",
                      stderr="no rollout found for thread id abc")
    mock_run.side_effect = [stale, stale]
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc")
    assert mock_run.call_count == 2, "정확히 1회 재시도 (무한 루프 금지)"
    assert resp.error  # 2번째(새 세션)도 실패 → 에러 반환, 3번째 호출 없음


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_stale_recovery_preserves_options(mock_env, mock_run, mock_find):
    """stale 재시도(새 세션)에 sandbox/approval 이 적용된다 (resume 는 무시하던 것)."""
    stale = MagicMock(returncode=1, stdout="",
                      stderr="no rollout found for thread id abc")
    fresh = MagicMock(
        returncode=0,
        stdout='{"type":"thread.started","thread_id":"tid-new"}\n'
               '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        stderr="")
    mock_run.side_effect = [stale, fresh]
    p = CodexProvider()
    p.invoke([Message(role="user", content="hi")], session_id="abc",
             sandbox_mode="workspace-write", approval_policy="never")
    cmd2 = mock_run.call_args_list[1][0][0]
    assert "resume" not in cmd2
    assert "-s" in cmd2 and "workspace-write" in cmd2
    assert "-a" in cmd2 and "never" in cmd2


def test_is_codex_stale_case_insensitive():
    """버전별 표기 흔들림(대소문자)에 견디는 stale 마커 매칭."""
    from agentcli.providers.codex import _is_codex_stale
    assert _is_codex_stale(
        "Error: thread/resume failed: no rollout found for thread id abc (code -32600)")
    assert _is_codex_stale("No Rollout Found for thread id X")  # 대문자 변형
    assert _is_codex_stale("no rollout found")
    assert not _is_codex_stale("some unrelated error")
    assert not _is_codex_stale(None)
    assert not _is_codex_stale("")


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_async_stale_session_auto_recovers(mock_env, mock_find):
    """async 경로도 죽은 thread → 새 세션 1회 자동 재시도 (sync 와 parity)."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    stale = (b"", b"thread/resume failed: no rollout found for thread id abc", 1, False)
    fresh = (b'{"type":"thread.started","thread_id":"tid-new"}\n'
             b'{"type":"item.completed","item":{"type":"agent_message","text":"recovered"}}\n',
             b"", 0, False)
    with patch("agentcli.providers.codex.run_subprocess_async",
               new=AsyncMock(side_effect=[stale, fresh])) as m:
        p = CodexProvider()
        resp = asyncio.run(p.invoke_async(
            [Message(role="user", content="hi")], session_id="abc"))
    assert resp.content == "recovered"
    assert resp.session_id == "tid-new"
    assert m.call_count == 2


@patch("agentcli.providers.codex.shutil.which", return_value=r"C:\Users\u\AppData\Roaming\npm\codex.CMD")
def test_build_cmd_uses_resolved_binary_path(mock_which):
    p = CodexProvider()

    cmd = p._build_cmd("hi", "", None, "")

    assert cmd[0] == r"C:\Users\u\AppData\Roaming\npm\codex.CMD"


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_marks_codex_prompt_usage_as_provider_reported(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_retries_once_when_first_turn_is_only_initial_greeting(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_resume_session(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_cwd_and_sandbox(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value=None)
def test_invoke_binary_missing(mock_find, mock_run):
    """바이너리 미해석 시 exec 전에 binary_missing 으로 단락한다."""
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error == "codex CLI not found"
    assert resp.error_type == "binary_missing"
    assert resp.exit_code == 127
    # exec 자체가 호출되지 않아야 한다 (단락 검증).
    mock_run.assert_not_called()


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run",
       side_effect=FileNotFoundError)
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_not_found(mock_env, mock_run, mock_find):
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_uses_system_and_last_message_only(mock_env, mock_run, mock_find):
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


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_nonzero_returns_error(mock_env, mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="HTTP 401 unauthorized")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert "unauthorized" in resp.error
    assert resp.error_type == "auth"


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run",
       side_effect=subprocess.TimeoutExpired("cmd", 120))
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_timeout(mock_env, mock_run, mock_find):
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
