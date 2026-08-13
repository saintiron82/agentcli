from unittest.mock import patch, MagicMock
import subprocess
from agentcli.providers.base import PROMPT_STDIN_THRESHOLD
from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message


def _sync(stdout="", stderr="", rc=0, timed_out=False):
    """run_subprocess_sync 반환 계약 ``(stdout, stderr, rc, timed_out)`` 생성."""
    return (stdout.encode("utf-8"), stderr.encode("utf-8"), rc, timed_out)


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
    # 전 플랫폼 네이티브 resume 지원 — Windows 전용 stateless 가드는 제거됐다
    # (issue #27: #4 의 인터랙티브 stdin 대기 전제가 해소되어 stale 이었음).
    assert p.supports_sessions is True
    # 어느 모드든 히스토리는 CLI 소유 — 라이브러리는 내용을 저장하지 않는다.
    assert p.stores_history is False


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_success(mock_find, mock_run):
    mock_run.return_value = _sync(
        stdout='{"result":"응답입니다","usage":{"input_tokens":300,"output_tokens":200}}')
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


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_marks_claude_prompt_usage_as_provider_reported(mock_find, mock_run):
    mock_run.return_value = _sync(
        stdout='{"result":"응답입니다","usage":{"input_tokens":6,"output_tokens":2}}')
    p = ClaudeProvider()

    resp = p.invoke([Message(role="user", content="longer user prompt than six tokens")])

    assert resp.tokens.prompt_tokens == 6
    assert resp.tokens.payload_prompt_tokens > 0
    assert resp.tokens.prompt_tokens_reliable is False
    assert resp.tokens.prompt_tokens_source == "claude_cli_reported"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_session_id_ignored_in_stateless_mode(mock_find, mock_run):
    """issue #4: Windows(stateless) 모드는 외부 session_id를 받아도 `--resume`
    하지 않고 새 식별자를 발급한다 (hang 우회)."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()
    p.supports_sessions = False  # Windows 모드 시뮬레이션
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc-123")
    cmd = mock_run.call_args[0][0]
    assert "--resume" not in cmd, "issue #4: stateless 모드에서 --resume 금지"
    assert "--session-id" in cmd
    assert resp.session_id and resp.session_id != "abc-123"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_resumes_session(mock_find, mock_run):
    """세션 모드: 저장된 session_id로 `--resume`, 동일 sid를 반환."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")],
                    session_id="abc-123")
    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd
    assert resp.session_id == "abc-123"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_stale_session_auto_recovers(mock_find, mock_run):
    """만료된 sid resume 실패 시 새 세션으로 1회 자동 재시도."""
    stale = _sync(stderr="No conversation found with session ID: abc-123", rc=1)
    fresh = _sync(stdout='{"result":"recovered"}')
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


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_stale_retry_preserves_lean_and_debug(mock_find, mock_run, tmp_path):
    """stale-session 자동 재시도가 lean/debug 를 재시도 커맨드에도 유지한다."""
    mock_run.side_effect = [
        _sync(stderr="No conversation found with session ID: abc-123", rc=1),
        _sync(stdout='{"result":"ok"}'),
    ]
    p = ClaudeProvider(lean=True, debug=True,
                       debug_log_path=str(tmp_path / "t.jsonl"))
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc-123")
    assert resp.content == "ok"
    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        assert "--safe-mode" in cmd, "재시도에도 lean 유지"
        assert "--debug" in cmd, "재시도에도 debug 유지"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_other_failure_not_retried(mock_find, mock_run):
    """stale-session 외의 실패는 재시도 없이 그대로 에러 반환."""
    mock_run.return_value = _sync(stderr="usage limit reached", rc=1)
    p = ClaudeProvider()
    p.supports_sessions = True
    resp = p.invoke([Message(role="user", content="hi")],
                    session_id="abc-123")
    assert resp.content == ""
    assert resp.error
    assert mock_run.call_count == 1


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_non_json_stdout_fallback(mock_find, mock_run):
    """JSON 파싱 실패 시 stdout 전체를 content로 취급 (하위 호환)."""
    mock_run.return_value = _sync(stdout="plain text response")
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == "plain text response"
    assert resp.tokens.total_tokens == 0


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_cwd_and_tools(mock_find, mock_run):
    """cwd + allowed_tools/disallowed_tools 전달 검증."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
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
    # 기본 explicit 티어(#59)에서 빌트인 allowed_tools 는 --tools(정의
    # allowlist)로 간다 — --allowedTools 는 inherit 티어의 배선이다.
    assert "--tools" in cmd
    aidx = cmd.index("--tools")
    assert cmd[aidx + 1] == "Read,Grep"
    assert "--disallowedTools" in cmd
    didx = cmd.index("--disallowedTools")
    assert cmd[didx + 1] == "Bash"
    assert kwargs.get("cwd") == "/repo"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_lean_strips_harness(mock_find, mock_run):
    """lean=True 는 단일 completion 용으로 커스터마이즈+빌트인 툴을 모두 끊는다."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider(lean=True)
    p.invoke([Message(role="user", content="요약해줘")])
    cmd = mock_run.call_args[0][0]
    # 모든 커스터마이즈(CLAUDE.md/skills/plugins/hooks/MCP/custom agents) 비활성화
    assert "--safe-mode" in cmd
    # 빌트인 툴 전부 비활성화: --tools "" (allowed_tools 미지정 시)
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    # MCP/툴 허용·거부 플래그는 lean 에서 붙지 않는다
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd
    assert "--disallowedTools" not in cmd


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_lean_keeps_explicit_tools(mock_find, mock_run):
    """lean + allowed_tools 명시 → 그 빌트인 툴만 --tools 로 남긴다."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider(lean=True, allowed_tools=["Read", "Grep"])
    p.invoke([Message(role="user", content="hi")])
    cmd = mock_run.call_args[0][0]
    assert "--safe-mode" in cmd
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_lean_ignores_mcp_and_disallowed_when_present(mock_find):
    """lean 은 mcp_config/disallowed_tools 가 명시되어 있어도 무시한다(문서 계약).
    --safe-mode 가 MCP 를 끄고 --tools 가 allowlist 이므로."""
    p = ClaudeProvider(lean=True, disallowed_tools=["Bash"])
    cmd, _ = p._build_cmd("hi", "", "", "stream-json",
                          mcp_config={"pair": {"url": "https://x/mcp"}},
                          strict_mcp_config=True)
    assert "--safe-mode" in cmd
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "--mcp-config" not in cmd
    assert "--disallowedTools" not in cmd
    assert "--strict-mcp-config" not in cmd


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_lean_and_debug_combined(mock_find):
    """lean + debug 동시 → --safe-mode 와 --debug 가 함께 붙는다."""
    cmd, _ = ClaudeProvider(lean=True, debug=True)._build_cmd(
        "hi", "", "", "stream-json")
    assert "--safe-mode" in cmd
    assert "--debug" in cmd
    assert "--tools" in cmd


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_lean_per_call_override(mock_find, mock_run):
    """생성자 기본이 lean=False 여도 호출 시 lean=True 로 켤 수 있다."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()  # 기본 lean=False
    p.invoke([Message(role="user", content="hi")])
    assert "--safe-mode" not in mock_run.call_args[0][0]
    p.invoke([Message(role="user", content="hi")], lean=True)
    assert "--safe-mode" in mock_run.call_args[0][0]


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_debug_adds_flag_and_writes_trace(mock_find, mock_run, tmp_path):
    """debug=True 는 --debug 를 붙이고, debug_log_path 에 redact 된 trace 를 남긴다."""
    import json as _json
    mock_run.return_value = _sync(stdout='{"result":"ok"}', stderr="mcp connect...\n")
    trace = tmp_path / "trace.jsonl"
    p = ClaudeProvider(debug=True, debug_log_path=str(trace))
    p.invoke([Message(role="user", content="비밀프롬프트요약")])
    cmd = mock_run.call_args[0][0]
    assert "--debug" in cmd
    assert trace.exists()
    rec = _json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "invoke"
    assert rec["returncode"] == 0
    # argv 의 프롬프트 본문은 redact 되어 원문이 노출되지 않아야 한다
    assert any("<prompt:" in str(a) for a in rec["argv"])
    assert "비밀프롬프트요약" not in _json.dumps(rec["argv"], ensure_ascii=False)


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_debug_off_no_flag(mock_find, mock_run):
    """기본(debug=False)은 --debug 를 붙이지 않는다 — 기존 동작 불변."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    ClaudeProvider().invoke([Message(role="user", content="hi")])
    assert "--debug" not in mock_run.call_args[0][0]


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_build_cmd_partial_messages_flag(mock_find):
    """partial_messages 는 stream-json 에만 --include-partial-messages 를 붙인다."""
    p = ClaudeProvider()
    cmd, _ = p._build_cmd("hi", "", "", "stream-json", partial_messages=True)
    assert "--include-partial-messages" in cmd
    cmd2, _ = p._build_cmd("hi", "", "", "stream-json", partial_messages=False)
    assert "--include-partial-messages" not in cmd2
    # 비스트리밍 json 에는 절대 붙지 않는다
    cmd3, _ = p._build_cmd("hi", "", "", "json", partial_messages=True)
    assert "--include-partial-messages" not in cmd3
    # 생성자 기본 상속: 호출 시 partial_messages 미지정(None) → self 값 사용
    cmd4, _ = ClaudeProvider(partial_messages=True)._build_cmd(
        "hi", "", "", "stream-json")
    assert "--include-partial-messages" in cmd4


def test_invoke_async_via_base_fallback():
    """base 기본 구현: invoke_async는 to_thread로 동기 invoke를 실행."""
    import asyncio
    from unittest.mock import patch, MagicMock
    with patch("agentcli.providers.claude.run_subprocess_sync") as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        mock_run.return_value = _sync(stdout='{"result":"async-ok"}')
        # 직접 async 함수를 테스트: Claude는 invoke_async 오버라이드했지만
        # asyncio.create_subprocess_exec 모킹이 까다로우니 여기서는
        # 기본 fallback이 아닌 진짜 async 경로는 integration 단에서 검증.
        # 여기서는 sync invoke가 정상 동작하는지만 재확인.
        p = ClaudeProvider()
        resp = p.invoke([Message(role="user", content="x")])
        assert resp.content == "async-ok"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_with_model(mock_find, mock_run):
    mock_run.return_value = _sync(stdout="ok")
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="hi")], model="sonnet")
    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "sonnet"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_serializes_exactly_what_client_sends(mock_find, mock_run):
    """provider 는 받은 메시지를 충실히 직렬화한다 — 세션 모드에서 이전 턴이
    프롬프트에 안 들어가는 것은 client 가 [system?, user] 만 담는 것으로
    보장된다 (test_session_routing). 명시 주입(inject_context)분은 Context
    블록으로 전달되어야 한다. system 은 #51 부터 ``-p`` 평탄화 대신 실제
    ``--append-system-prompt`` 플래그로 격리되어 전달된다 — 내용은 여전히
    유실 없이 전부 CLI 에 도달한다."""
    mock_run.return_value = _sync(stdout="ok")
    p = ClaudeProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="injected note", agent="bull"),
        Message(role="user", content="new question"),
    ])
    cmd = mock_run.call_args[0][0]
    prompt = cmd[cmd.index("-p") + 1]
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Follow GUIDE v2"
    assert "Follow GUIDE v2" not in prompt, (
        "#51: system 블록이 -p 에도 남으면 격리가 무의미하다(이중 전송)")
    assert "new question" in prompt
    assert "Context (injected by host application):" in prompt
    assert "[user:bull] injected note" in prompt


@patch("agentcli.providers.claude.run_subprocess_sync",
       return_value=(b"", b"timeout after 120s", 124, True))
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_timeout(mock_find, mock_run):
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124


def test_invoke_async_closes_stdin():
    """claude ``invoke_async`` 도 sync 경로(run_subprocess_sync 는 stdin=DEVNULL
    하드코딩)와 codex/copilot 의 async 경로처럼 stdin 을 DEVNULL 로 닫아야 한다
    — 안 닫으면 부모 stdin 을 상속해 대화형 입력 대기로 행할 수 있다 (issue #34).
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b'{"result":"ok"}', b"", 0, False))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        resp = asyncio.run(
            p.invoke_async([Message(role="user", content="hi")]))
    assert resp.content == "ok"
    assert mock_run.call_args.kwargs.get("use_stdin_devnull") is True


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value=None)
def test_invoke_not_found(mock_find):
    p = ClaudeProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error


def test_claude_effort_flag_and_report():
    p = ClaudeProvider()
    args, res = p._reasoning_flags("high", None)
    assert args == ["--effort", "high"]
    assert res.effort.applied == "high" and res.effort.clamped is False


def test_claude_effort_minimal_clamps_up_to_low():
    args, res = ClaudeProvider()._reasoning_flags("minimal", None)
    assert args == ["--effort", "low"]
    assert res.effort.clamped is True


def test_claude_thinking_is_unsupported_noop():
    args, res = ClaudeProvider()._reasoning_flags(None, "detailed")
    assert args == []                      # no flag emitted
    assert res.thinking.supported is False and res.thinking.applied == ""


def test_claude_percall_effort_overrides_constructor_default():
    p = ClaudeProvider(effort="low")
    args, _ = p._reasoning_flags("max", None)   # per-call wins
    assert args == ["--effort", "max"]


def test_claude_no_reasoning_means_no_args_and_none():
    args, res = ClaudeProvider()._reasoning_flags(None, None)
    assert args == [] and res is None


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_claude_build_cmd_includes_effort_via_reasoning_args(mock_find):
    p = ClaudeProvider()
    args, _ = p._reasoning_flags("xhigh", None)
    cmd, _sid = p._build_cmd("hi", "", "", reasoning_args=args)
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "xhigh"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_timeout_debug_carries_partial_stderr(mock_find, mock_run, tmp_path):
    """invoke() timeout 브랜치는 debug=True 일 때 합성 문자열 대신 부분 stderr를
    debug trace 에 남겨야 한다 (commit 9305b69 회귀). 사용자 계약(error="timeout after 120s")
    은 변하지 않는다."""
    import json as _json
    # timed_out=True 이고 real partial stderr 를 반환
    mock_run.return_value = (b"", b"DEBUG-MARKER partial stderr from claude", 124, True)
    trace = tmp_path / "trace.jsonl"
    p = ClaudeProvider(debug=True, debug_log_path=str(trace))
    resp = p.invoke([Message(role="user", content="hi")], timeout=120)

    # 사용자 계약: 타임아웃 에러는 여전히 "timeout after 120s"
    assert resp.content == ""
    assert resp.error == "timeout after 120s"
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124

    # debug trace 검증: partial stderr 가 그대로 들어가야 함 (합성 string 아님)
    assert trace.exists()
    rec = _json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "invoke"
    assert rec["returncode"] == 124
    # 부분 stderr 가 그대로 들어가 있어야 함
    assert "DEBUG-MARKER partial stderr from claude" in rec["stderr"]
    # "timeout after" 문자열은 debug trace 에 들어가면 안 됨
    assert "timeout after" not in rec["stderr"]


def test_invoke_async_timeout_debug_carries_partial_stderr(tmp_path):
    """invoke_async() timeout 브랜치도 debug=True 일 때 부분 stderr를
    debug trace 에 남겨야 한다 (invoke 와 동일 계약). 사용자 계약은 변하지 않는다."""
    import asyncio
    import json as _json
    from unittest.mock import AsyncMock, patch

    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b"", b"DEBUG-MARKER async partial stderr", 124, True))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        trace = tmp_path / "trace.jsonl"
        p = ClaudeProvider(debug=True, debug_log_path=str(trace))
        resp = asyncio.run(
            p.invoke_async([Message(role="user", content="hi")], timeout=120))

        # 사용자 계약: 타임아웃 에러는 여전히 "timeout after 120s"
        assert resp.content == ""
        assert resp.error == "timeout after 120s"
        assert resp.error_type == "timeout"
        assert resp.exit_code == 124

        # debug trace 검증: partial stderr 가 그대로 들어가야 함
        assert trace.exists()
        rec = _json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert rec["phase"] == "invoke_async"
        assert rec["returncode"] == 124
        # 부분 stderr 가 그대로 들어가 있어야 함
        assert "DEBUG-MARKER async partial stderr" in rec["stderr"]
        # "timeout after" 문자열은 debug trace 에 들어가면 안 됨
        assert "timeout after" not in rec["stderr"]


# ===== issue #30: 큰 프롬프트 stdin 전달 (Windows 32,767자 argv 한계 우회) =====

def _big_prompt() -> str:
    """PROMPT_STDIN_THRESHOLD 를 넘는 ascii 프롬프트 (1 byte/char)."""
    return "x" * (PROMPT_STDIN_THRESHOLD + 1)


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_prompt_exactly_at_threshold_stays_argv_mode(mock_find, mock_run):
    """경계값: 정확히 PROMPT_STDIN_THRESHOLD 바이트인 프롬프트는 (엄격한
    ``>`` 비교이므로) 여전히 ARGV 모드 — stdin 으로 넘어가지 않는다."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    exact = "x" * PROMPT_STDIN_THRESHOLD
    p = ClaudeProvider()
    p.invoke([Message(role="user", content=exact)])
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert cmd[cmd.index("-p") + 1] == exact
    assert kwargs.get("input_bytes") is None


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_build_cmd_prompt_via_stdin_omits_positional_prompt(mock_find):
    p = ClaudeProvider()
    cmd, _sid = p._build_cmd("giant prompt body", "", "", "json",
                             prompt_via_stdin=True)
    assert "-p" in cmd
    assert "giant prompt body" not in cmd
    # -p 바로 다음은 프롬프트가 아니라 다음 플래그여야 한다.
    assert cmd[cmd.index("-p") + 1] == "--output-format"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_build_cmd_default_still_inlines_prompt(mock_find):
    """prompt_via_stdin 미지정(기본 False) 은 기존과 byte-identical."""
    p = ClaudeProvider()
    cmd, _sid = p._build_cmd("small prompt", "", "", "json")
    assert cmd[cmd.index("-p") + 1] == "small prompt"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_large_prompt_routes_via_stdin(mock_find, mock_run):
    """8000바이트 초과 프롬프트는 argv 가 아니라 input_bytes 로 전달돼야 한다."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    big = _big_prompt()
    p = ClaudeProvider()
    p.invoke([Message(role="user", content=big)])
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert big not in cmd, "큰 프롬프트가 argv 에 그대로 남으면 안 된다"
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "--output-format"
    assert kwargs.get("input_bytes") == big.encode("utf-8")


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_small_prompt_stdin_untouched(mock_find, mock_run):
    """임계치 이하 프롬프트는 기존과 동일하게 argv 로, input_bytes 는 None."""
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="small prompt")])
    cmd = mock_run.call_args[0][0]
    kwargs = mock_run.call_args[1]
    assert cmd[cmd.index("-p") + 1] == "small prompt"
    assert kwargs.get("input_bytes") is None


def test_invoke_async_large_prompt_routes_via_input_bytes():
    """invoke_async 도 큰 프롬프트는 use_stdin_devnull 대신 input_bytes 를
    써야 한다 (run_subprocess_async 의 상호 배타 가드와 정합)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    big = _big_prompt()
    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b'{"result":"ok"}', b"", 0, False))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        resp = asyncio.run(
            p.invoke_async([Message(role="user", content=big)]))
    assert resp.content == "ok"
    cmd = mock_run.call_args.args[0]
    assert big not in cmd
    assert mock_run.call_args.kwargs.get("use_stdin_devnull") is False
    assert mock_run.call_args.kwargs.get("input_bytes") == big.encode("utf-8")


def test_invoke_async_small_prompt_still_closes_stdin_devnull():
    """작은 프롬프트는 여전히 use_stdin_devnull=True, input_bytes 없음(불변)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b'{"result":"ok"}', b"", 0, False))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        asyncio.run(p.invoke_async([Message(role="user", content="hi")]))
    assert mock_run.call_args.kwargs.get("use_stdin_devnull") is True
    assert mock_run.call_args.kwargs.get("input_bytes") is None
