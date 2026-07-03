from unittest.mock import patch, MagicMock
import subprocess
from agentcli.providers.copilot import CopilotProvider, _parse_copilot_jsonl
from agentcli.types import Message


def test_provider_id():
    p = CopilotProvider()
    assert p.provider_id == "copilot"
    assert p.supports_sessions is True
    assert p.supports_streaming is True


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_health_check_binary_missing(mock_find):
    h = CopilotProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"


def test_list_models():
    p = CopilotProvider()
    models = p.list_models()
    assert any(m["id"] == "gpt-5.5" for m in models)
    assert any(m["id"] == "gpt-5.4" for m in models)
    assert any(m["id"] == "gpt-5.3-codex" for m in models)
    assert any(m["id"] == "claude-sonnet-4.6" for m in models)
    assert any(m["id"] == "claude-opus-4.7" for m in models)
    assert any(m["id"] == "gpt-4.1" for m in models)


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
def test_invoke_debug_writes_invoke_trace(mock_find, mock_env, mock_run, tmp_path):
    """#29: copilot 비스트리밍 invoke 도 debug trace(phase=invoke) 를 남긴다."""
    import json
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_SAMPLE_STDOUT, stderr="cp dbg")
    trace = tmp_path / "copilot_invoke.jsonl"
    resp = CopilotProvider().invoke(
        [Message(role="user", content="hi")],
        debug=True, debug_log_path=str(trace))
    assert resp.content == "Hello world"
    rec = json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "invoke" and rec["provider"] == "copilot"
    assert rec["returncode"] == 0
    assert rec["schema"] == 1 and len(rec["call_id"]) == 12


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
def test_invoke_can_disable_alias_resume(mock_find, mock_env, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider()
    p.invoke([Message(role="user", content="hi")], alias="bull-agent",
             resume_by_alias=False)
    cmd = mock_run.call_args[0][0]
    assert "--name=bull-agent" in cmd
    assert "--resume=bull-agent" not in cmd


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


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_includes_system_prompt_but_not_history(mock_find, mock_env, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider()
    p.invoke([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="injected note"),
        Message(role="user", content="new question"),
    ])
    cmd = mock_run.call_args[0][0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Follow GUIDE v2" in prompt
    assert "new question" in prompt
    assert "Context (injected by host application):" in prompt
    assert "[user] injected note" in prompt


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_invoke_not_found(mock_find):
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error


@patch("agentcli.providers.copilot.subprocess.run",
       side_effect=subprocess.TimeoutExpired("cmd", 120))
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_timeout(mock_find, mock_env, mock_run):
    p = CopilotProvider()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124


# ===== reasoning (effort/thinking) =====

def test_copilot_effort_flag():
    args, res = CopilotProvider()._reasoning_flags("medium", None)
    assert args == ["--effort", "medium"] and res.effort.applied == "medium"


def test_copilot_effort_minimal_renames_to_none_not_clamped():
    args, res = CopilotProvider()._reasoning_flags("minimal", None)
    assert args == ["--effort", "none"] and res.effort.clamped is False


def test_copilot_thinking_concise_enables_summaries():
    args, res = CopilotProvider()._reasoning_flags(None, "concise")
    assert args == ["--enable-reasoning-summaries"]
    assert res.thinking.applied == "on" and res.thinking.clamped is False


def test_copilot_thinking_detailed_clamps_to_boolean():
    args, res = CopilotProvider()._reasoning_flags(None, "detailed")
    assert args == ["--enable-reasoning-summaries"]
    assert res.thinking.clamped is True


def test_copilot_thinking_off_emits_no_flag():
    args, res = CopilotProvider()._reasoning_flags(None, "off")
    assert args == [] and res.thinking.applied == ""


def test_copilot_no_reasoning_means_no_args_and_none():
    args, res = CopilotProvider()._reasoning_flags(None, None)
    assert args == [] and res is None


def test_copilot_percall_effort_overrides_constructor_default():
    p = CopilotProvider(effort="low")
    args, _ = p._reasoning_flags("max", None)   # per-call wins
    assert args == ["--effort", "max"]


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_copilot_build_cmd_includes_reasoning_args(mock_find):
    p = CopilotProvider()
    args, _ = p._reasoning_flags("high", "concise")
    cmd, _use_gh = p._build_cmd("hi", "", "", reasoning_args=args)
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"
    assert "--enable-reasoning-summaries" in cmd


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_constructor_effort_still_emits_flag(mock_find, mock_env, mock_run):
    """생성자 effort 기본값 → _reasoning_flags 경유로도 기존과 동일한 플래그가 붙는다."""
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    p = CopilotProvider(effort="medium")
    resp = p.invoke([Message(role="user", content="hi")])
    cmd = mock_run.call_args[0][0]
    assert "--effort" in cmd and "medium" in cmd
    assert resp.reasoning is not None and resp.reasoning.effort.applied == "medium"


@patch("agentcli.providers.copilot.subprocess.run")
@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_no_reasoning_means_argv_unchanged_and_none(mock_find, mock_env, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_SAMPLE_STDOUT, stderr="")
    resp = CopilotProvider().invoke([Message(role="user", content="hi")])
    cmd = mock_run.call_args[0][0]
    assert "--effort" not in cmd and "--enable-reasoning-summaries" not in cmd
    assert resp.reasoning is None


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_invoke_binary_missing_leaves_reasoning_unset(mock_find):
    resp = CopilotProvider(effort="high").invoke([Message(role="user", content="hi")])
    assert resp.reasoning is None
