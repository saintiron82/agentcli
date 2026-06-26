"""CopilotProvider 추가 행동 커버리지.

기존 ``tests/test_copilot_provider.py`` 와 ``tests/test_dispatch_stream_event.py``
가 다루지 않는 잔여 경로를 채운다:

  - ``_find_binary`` gh 폴백 (copilot 없고 gh 만 있을 때)
  - ``health_check`` gh 경로: version → auth status → auth_required / ok / probe,
    그리고 copilot 바이너리 probe 경로
  - ``_build_cmd`` 의 use_gh / text 포맷 / --model / --available-tools /
    --allow-all-paths 분기
  - 동기 ``invoke`` 의 rc!=0 에러 / FileNotFoundError 경로
  - 비동기 ``invoke_async`` 성공·에러·타임아웃·binary-missing·FileNotFoundError
  - ``stream_async`` cmd None 에러 + 실제 dispatch 위임
  - ``_parse_copilot_jsonl`` 의 빈 라인 skip / result.error 필드

mock 규약은 기존 파일과 동일: subprocess / run_subprocess_async / build_env /
_find_binary 를 patch.
"""
from unittest.mock import patch, MagicMock

import asyncio

from agentcli.providers.copilot import CopilotProvider, _parse_copilot_jsonl
from agentcli.types import (Message, ERROR_BINARY_MISSING, ERROR_AUTH,
                            StreamChunk)


_OK_STDOUT = '\n'.join([
    '{"type":"assistant.message","data":{"content":"hi","outputTokens":2}}',
    '{"type":"result","sessionId":"SID-1","exitCode":0}',
])


def _completed(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ============================================================
# _find_binary — gh 폴백
# ============================================================


def test_find_binary_falls_back_to_gh_when_copilot_absent():
    """copilot 미설치, gh 설치 → (gh_path, use_gh=True)."""
    def fake_which(name):
        if name == "copilot":
            return None
        if name in ("gh", "gh.exe"):
            return "/usr/local/bin/gh"
        return None

    with patch("agentcli.providers.copilot.shutil.which", side_effect=fake_which):
        path, use_gh = CopilotProvider()._find_binary()
    assert path == "/usr/local/bin/gh"
    assert use_gh is True


def test_find_binary_none_when_neither_present():
    with patch("agentcli.providers.copilot.shutil.which", return_value=None):
        path, use_gh = CopilotProvider()._find_binary()
    assert path is None
    assert use_gh is False


def test_is_available_true_when_binary_found():
    with patch("agentcli.providers.copilot.CopilotProvider._find_binary",
               return_value=("/usr/bin/copilot", False)):
        assert CopilotProvider().is_available() is True


# ============================================================
# health_check — gh 경로
# ============================================================


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/local/bin/gh", True))
def test_health_check_gh_auth_required(mock_find, mock_env):
    """gh auth status rc!=0 → auth_required."""
    def fake_health(cmd, **kwargs):
        if "version" in cmd:
            return _completed(0, stdout="gh version 2.40")
        # auth status
        return _completed(1, stdout="", stderr="You are not logged in")

    with patch("agentcli.providers.copilot.run_health_command",
               side_effect=fake_health):
        h = CopilotProvider().health_check()
    assert h.ok is False
    assert h.status == "auth_required"
    assert h.error_type == ERROR_AUTH
    assert h.auth_ok is False
    assert h.binary == "/usr/local/bin/gh"
    assert "not logged in" in h.message


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/local/bin/gh", True))
def test_health_check_gh_ok_without_probe(mock_find, mock_env):
    """gh auth status ok, probe=False → status ok, auth_ok True."""
    def fake_health(cmd, **kwargs):
        if "version" in cmd:
            return _completed(0, stdout="gh version 2.40")
        return _completed(0, stdout="Logged in to github.com")

    with patch("agentcli.providers.copilot.run_health_command",
               side_effect=fake_health):
        h = CopilotProvider().health_check()
    assert h.ok is True
    assert h.status == "ok"
    assert h.auth_ok is True
    assert h.version == "gh version 2.40"
    assert "Logged in" in h.message


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/local/bin/gh", True))
def test_health_check_gh_probe_invokes_provider(mock_find, mock_env):
    """gh auth ok + probe=True → invoke 호출 후 health_from_response."""
    def fake_health(cmd, **kwargs):
        if "version" in cmd:
            return _completed(0, stdout="gh version 2.40")
        return _completed(0, stdout="Logged in")

    with patch("agentcli.providers.copilot.run_health_command",
               side_effect=fake_health), \
         patch("agentcli.providers.copilot.subprocess.run",
               return_value=_completed(0, stdout=_OK_STDOUT, stderr="")):
        h = CopilotProvider().health_check(probe=True)
    assert h.ok is True
    assert h.status == "ok"
    assert h.binary == "/usr/local/bin/gh"


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_health_check_copilot_binary_no_probe(mock_find, mock_env):
    """copilot 바이너리, probe=False → ok 이지만 auth_ok 미확인(None)."""
    with patch("agentcli.providers.copilot.run_health_command",
               return_value=_completed(0, stdout="copilot 1.2.3")):
        h = CopilotProvider().health_check()
    assert h.ok is True
    assert h.status == "ok"
    assert h.auth_ok is None
    assert h.version == "copilot 1.2.3"


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_health_check_copilot_probe_path(mock_find, mock_env):
    """copilot 바이너리 + probe=True → invoke 경유 health_from_response."""
    with patch("agentcli.providers.copilot.run_health_command",
               return_value=_completed(0, stdout="copilot 1.2.3")), \
         patch("agentcli.providers.copilot.subprocess.run",
               return_value=_completed(0, stdout=_OK_STDOUT, stderr="")):
        h = CopilotProvider().health_check(probe=True)
    assert h.ok is True
    assert h.status == "ok"
    assert h.version == "copilot 1.2.3"


# ============================================================
# _build_cmd — use_gh / text 포맷 / --model / available-tools / allow-all-paths
# ============================================================


def test_build_cmd_gh_prefixes_copilot_subcommand():
    p = CopilotProvider(allow_all_tools=False)
    with patch.object(p, "_find_binary",
                      return_value=("/usr/local/bin/gh", True)):
        cmd, use_gh = p._build_cmd("hello", model="gpt-5.5",
                                   session_id="", output_format="json")
    assert use_gh is True
    assert cmd[0] == "/usr/local/bin/gh"
    assert cmd[1] == "copilot"
    # use_gh 면 --model 은 무시된다 (model and not use_gh)
    assert "--model" not in cmd


def test_build_cmd_text_format_uses_silent_flag():
    p = CopilotProvider(allow_all_tools=False)
    with patch.object(p, "_find_binary",
                      return_value=("/usr/bin/copilot", False)):
        cmd, _ = p._build_cmd("hi", model="", session_id="",
                              output_format="text")
    assert "-s" in cmd
    assert "--output-format" not in cmd


def test_build_cmd_model_added_when_not_gh():
    p = CopilotProvider(allow_all_tools=False)
    with patch.object(p, "_find_binary",
                      return_value=("/usr/bin/copilot", False)):
        cmd, _ = p._build_cmd("hi", model="gpt-5.5", session_id="",
                              output_format="json")
    assert "--model" in cmd
    assert "gpt-5.5" in cmd


def test_build_cmd_available_tools_and_allow_all_paths():
    p = CopilotProvider(allow_all_tools=False,
                        available_tools=["Read", "Edit"],
                        allow_all_paths=True)
    with patch.object(p, "_find_binary",
                      return_value=("/usr/bin/copilot", False)):
        cmd, _ = p._build_cmd("hi", model="", session_id="",
                              output_format="json")
    assert "--available-tools" in cmd
    assert "Read,Edit" in cmd
    assert "--allow-all-paths" in cmd


def test_build_cmd_returns_none_when_binary_missing():
    p = CopilotProvider()
    with patch.object(p, "_find_binary", return_value=(None, False)):
        cmd, use_gh = p._build_cmd("hi", model="", session_id="",
                                   output_format="json")
    assert cmd is None
    assert use_gh is False


# ============================================================
# invoke — rc!=0 에러 / FileNotFoundError
# ============================================================


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_nonzero_return_code_is_error(mock_find, mock_env):
    with patch("agentcli.providers.copilot.subprocess.run",
               return_value=_completed(2, stdout="",
                                       stderr="boom failure detail")):
        resp = CopilotProvider().invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.exit_code == 2
    assert "boom failure detail" in resp.error
    assert resp.error_type  # classified, non-empty


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_nonzero_with_empty_stderr_uses_exit_marker(mock_find, mock_env):
    with patch("agentcli.providers.copilot.subprocess.run",
               return_value=_completed(3, stdout="", stderr="")):
        resp = CopilotProvider().invoke([Message(role="user", content="hi")])
    assert resp.exit_code == 3
    assert resp.error == "exit=3"


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.subprocess.run",
       side_effect=FileNotFoundError())
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_file_not_found_normalizes_binary_missing(mock_find, mock_run,
                                                         mock_env):
    """_build_cmd 는 통과했지만 실제 exec 시 FileNotFoundError → binary_missing."""
    resp = CopilotProvider().invoke([Message(role="user", content="hi")])
    assert resp.content == ""
    assert resp.error_type == ERROR_BINARY_MISSING
    assert resp.exit_code == 127


# ============================================================
# invoke_async — 성공 / rc!=0 / 타임아웃 / binary-missing / FileNotFoundError
# ============================================================


def _run(coro):
    return asyncio.run(coro)


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_async_success_parses_result(mock_find, mock_env):
    async def fake_async(cmd, **kwargs):
        return (_OK_STDOUT.encode(), b"", 0, False)

    with patch("agentcli.providers.copilot.run_subprocess_async",
               side_effect=fake_async):
        resp = _run(CopilotProvider().invoke_async(
            [Message(role="user", content="hi")]))
    assert resp.content == "hi"
    assert resp.session_id == "SID-1"
    assert resp.tokens.completion_tokens == 2
    assert resp.exit_code == 0


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_async_nonzero_return_code_is_error(mock_find, mock_env):
    async def fake_async(cmd, **kwargs):
        return (b"", b"async boom", 5, False)

    with patch("agentcli.providers.copilot.run_subprocess_async",
               side_effect=fake_async):
        resp = _run(CopilotProvider().invoke_async(
            [Message(role="user", content="hi")], session_id="keep-me"))
    assert resp.content == ""
    assert resp.exit_code == 5
    assert "async boom" in resp.error
    # rc!=0 경로에서도 들고온 session_id 보존
    assert resp.session_id == "keep-me"


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_async_timeout(mock_find, mock_env):
    async def fake_async(cmd, **kwargs):
        return (b"", b"timeout", 124, True)

    with patch("agentcli.providers.copilot.run_subprocess_async",
               side_effect=fake_async):
        resp = _run(CopilotProvider().invoke_async(
            [Message(role="user", content="hi")], timeout=7))
    assert resp.error_type == "timeout"
    assert resp.exit_code == 124
    assert "7" in resp.error


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_invoke_async_binary_missing(mock_find):
    resp = _run(CopilotProvider().invoke_async(
        [Message(role="user", content="hi")]))
    assert resp.content == ""
    assert resp.error_type == ERROR_BINARY_MISSING
    assert resp.exit_code == 127


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_invoke_async_file_not_found(mock_find, mock_env):
    async def boom(cmd, **kwargs):
        raise FileNotFoundError()

    with patch("agentcli.providers.copilot.run_subprocess_async",
               side_effect=boom):
        resp = _run(CopilotProvider().invoke_async(
            [Message(role="user", content="hi")]))
    assert resp.error_type == ERROR_BINARY_MISSING
    assert resp.exit_code == 127


# ============================================================
# stream_async — cmd None 에러 + 실제 dispatch 위임
# ============================================================


def _collect_stream(provider, **kwargs):
    async def run():
        return [c async for c in provider.stream_async(
            [Message(role="user", content="hi")], **kwargs)]
    return asyncio.run(run())


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=(None, False))
def test_stream_async_binary_missing_yields_error(mock_find):
    chunks = _collect_stream(CopilotProvider())
    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert "not found" in chunks[0].content


@patch("agentcli.providers.copilot.build_env", return_value={"PATH": "/usr/bin"})
@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_stream_async_delegates_to_template_and_dispatch(mock_find, mock_env):
    """_run_stream_template 를 결정적 fake 로 대체해 dispatch 위임만 확인.

    실제 subprocess 없이, 미리 만든 JSON event 들을 _dispatch_stream_event
    로 흘려보내 정규화된 청크가 그대로 yield 되는지 검증.
    """
    events = [
        {"type": "assistant.message_delta", "data": {"deltaContent": "Hel"}},
        {"type": "assistant.message_delta", "data": {"deltaContent": "lo"}},
        {"type": "assistant.message",
         "data": {"content": "Hello", "outputTokens": 2}},
        {"type": "result", "sessionId": "SID-STREAM", "exitCode": 0},
    ]

    async def fake_template(self, cmd, state, **kwargs):
        async for evt in _aiter(events):
            async for chunk in self._dispatch_stream_event(evt, state):
                yield chunk
        yield StreamChunk(type="done", session_id=state.final_session_id,
                          usage=state.final_usage)

    async def _aiter(items):
        for it in items:
            yield it

    with patch("agentcli.providers.base.LLMProvider._run_stream_template",
               fake_template):
        chunks = _collect_stream(CopilotProvider())

    text = "".join(c.content for c in chunks if c.type == "text")
    assert text == "Hello"
    done = [c for c in chunks if c.type == "done"]
    assert len(done) == 1
    assert done[0].session_id == "SID-STREAM"


# ============================================================
# _parse_copilot_jsonl — 빈 라인 skip / result.error 필드
# ============================================================


def test_parse_skips_blank_lines():
    stdout = '\n'.join([
        '',
        '   ',
        '{"type":"assistant.message","data":{"content":"ok","outputTokens":1}}',
        '',
        '{"type":"result","sessionId":"S-blank","exitCode":0}',
    ])
    r = _parse_copilot_jsonl(stdout)
    assert r["text"] == "ok"
    assert r["session_id"] == "S-blank"
    assert r["error"] == ""


def test_parse_result_error_field_string():
    stdout = '\n'.join([
        '{"type":"result","sessionId":"S-err","exitCode":2,"error":"quota exceeded"}',
    ])
    r = _parse_copilot_jsonl(stdout)
    assert r["session_id"] == "S-err"
    assert r["error"] == "quota exceeded"


def test_parse_result_error_field_non_string_is_stringified():
    stdout = '{"type":"result","sessionId":"S","exitCode":1,"error":{"code":42}}'
    r = _parse_copilot_jsonl(stdout)
    # dict error → str() 화
    assert "42" in r["error"]


def test_parse_nonzero_exit_without_message_uses_marker():
    stdout = '{"type":"result","sessionId":"S","exitCode":9}'
    r = _parse_copilot_jsonl(stdout)
    assert r["error"] == "copilot exit=9"
