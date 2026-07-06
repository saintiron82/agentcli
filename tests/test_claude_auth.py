"""ClaudeProvider OAuth 토큰 주입 테스트 (issue #36).

agentcli-managed 소스(per-call kwarg > 생성자 > env var > 파일)에서 claude
OAuth 토큰을 resolve 해 subprocess env 의 ``CLAUDE_CODE_OAUTH_TOKEN`` 으로
주입한다 — headless/container(``~/.claude`` read-only mount) 배포에서 매일
401 을 겪는 문제의 근본 대응. 소스가 하나도 없으면 ``env=None`` 을 그대로
넘겨 기존 동작(부모 env 상속)과 byte-identical 해야 한다(하위호환 계약).

토큰 값은 어떤 로그/트레이스/argv 에도 노출되면 안 된다 — env 로만 전달.
"""
import asyncio
import logging
import stat
from unittest.mock import AsyncMock, patch

import pytest

from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message


def _sync(stdout="", stderr="", rc=0, timed_out=False):
    """run_subprocess_sync 반환 계약 ``(stdout, stderr, rc, timed_out)`` 생성."""
    return (stdout.encode("utf-8"), stderr.encode("utf-8"), rc, timed_out)


# ---------------------------------------------------------------------------
# _resolve_oauth_token 단위 테스트 — 소스 우선순위
# ---------------------------------------------------------------------------

def test_resolve_percall_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    (tmp_path / ".agentcli" / "claude_oauth_token").write_text("file-token")
    p = ClaudeProvider(oauth_token="ctor-token")
    assert p._resolve_oauth_token("percall-token") == "percall-token"


def test_resolve_constructor_wins_over_env_and_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    (tmp_path / ".agentcli" / "claude_oauth_token").write_text("file-token")
    p = ClaudeProvider(oauth_token="ctor-token")
    assert p._resolve_oauth_token(None) == "ctor-token"


def test_resolve_env_wins_over_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    (tmp_path / ".agentcli" / "claude_oauth_token").write_text("file-token")
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) == "env-token"


def test_resolve_file_used_when_no_other_source(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    token_path = tmp_path / ".agentcli" / "claude_oauth_token"
    token_path.write_text("file-token")
    token_path.chmod(0o600)
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) == "file-token"


def test_resolve_no_source_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) is None


def test_resolve_empty_env_var_falls_through_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "   ")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    token_path = tmp_path / ".agentcli" / "claude_oauth_token"
    token_path.write_text("file-token")
    token_path.chmod(0o600)
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) == "file-token"


def test_resolve_empty_file_value_means_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    token_path = tmp_path / ".agentcli" / "claude_oauth_token"
    token_path.write_text("   \n")
    token_path.chmod(0o600)
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) is None


def test_resolve_percall_empty_string_skips_constructor_falls_to_env(
        monkeypatch, tmp_path):
    """per-call kwarg 가 명시적으로 주어지면(빈 문자열이라도) 생성자 기본값을
    건너뛰고 그 다음 소스(env var)로 폴백한다 — effort/thinking 과 동일한
    ``self._x if per_call is None else per_call`` idiom."""
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = ClaudeProvider(oauth_token="ctor-token")
    assert p._resolve_oauth_token("") == "env-token"


def test_resolve_whitespace_only_constructor_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-token")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = ClaudeProvider(oauth_token="   ")
    assert p._resolve_oauth_token(None) == "env-token"


def test_resolve_strips_whitespace_from_resolved_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = ClaudeProvider()
    assert p._resolve_oauth_token("  spaced-token  ") == "spaced-token"


def test_resolve_missing_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # ~/.agentcli 디렉터리 자체가 없는 경우
    p = ClaudeProvider()
    assert p._resolve_oauth_token(None) is None


# ---------------------------------------------------------------------------
# 파일 권한 경고 (POSIX 전용) — 경고는 경로만 언급, 토큰 값은 절대 포함 금지
# ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("os").name != "posix",
                    reason="POSIX 권한 모델 전용")
def test_world_readable_file_warns_but_still_used(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    token_path = tmp_path / ".agentcli" / "claude_oauth_token"
    token_path.write_text("super-secret-token")
    token_path.chmod(0o644)  # world-readable
    p = ClaudeProvider()
    with caplog.at_level(logging.WARNING):
        token = p._resolve_oauth_token(None)
    assert token == "super-secret-token"  # 여전히 사용됨
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(token_path) in msg for msg in warnings)
    assert not any("super-secret-token" in msg for msg in warnings)


@pytest.mark.skipif(__import__("os").name != "posix",
                    reason="POSIX 권한 모델 전용")
def test_owner_only_file_does_not_warn(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".agentcli").mkdir()
    token_path = tmp_path / ".agentcli" / "claude_oauth_token"
    token_path.write_text("super-secret-token")
    token_path.chmod(0o600)
    p = ClaudeProvider()
    with caplog.at_level(logging.WARNING):
        p._resolve_oauth_token(None)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# _auth_env — env=None 하위호환 계약 / env dict 조립
# ---------------------------------------------------------------------------

def test_auth_env_returns_none_when_no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = ClaudeProvider()
    assert p._auth_env(None) is None


def test_auth_env_injects_token_and_inherits_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("SOME_MARKER_VAR", "marker-value")
    p = ClaudeProvider(oauth_token="tok-xyz")
    env = p._auth_env(None)
    assert env is not None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-xyz"
    assert env["SOME_MARKER_VAR"] == "marker-value"


# ---------------------------------------------------------------------------
# invoke() — env kwarg 전달 (backward compat + injection)
# ---------------------------------------------------------------------------

@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_no_token_passes_env_none(mock_find, mock_run, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="hi")])
    assert mock_run.call_args.kwargs.get("env") is None


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_constructor_token_reaches_subprocess_env(mock_find, mock_run):
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider(oauth_token="ctor-tok")
    p.invoke([Message(role="user", content="hi")])
    env = mock_run.call_args.kwargs.get("env")
    assert env is not None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ctor-tok"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_percall_token_overrides_constructor(mock_find, mock_run):
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider(oauth_token="ctor-tok")
    p.invoke([Message(role="user", content="hi")], oauth_token="percall-tok")
    env = mock_run.call_args.kwargs.get("env")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "percall-tok"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_env_var_token_reaches_subprocess_env(mock_find, mock_run, monkeypatch):
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "env-tok")
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="hi")])
    env = mock_run.call_args.kwargs.get("env")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "env-tok"


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_invoke_stale_retry_preserves_oauth_token(mock_find, mock_run):
    """stale-session 자동 복구 재시도에도 oauth_token 이 유지되어야 한다."""
    mock_run.side_effect = [
        _sync(stdout="", stderr="No conversation found with session ID abc", rc=1),
        _sync(stdout='{"result":"recovered"}', rc=0),
    ]
    p = ClaudeProvider(oauth_token="ctor-tok")
    resp = p.invoke([Message(role="user", content="hi")], session_id="abc")
    assert resp.content == "recovered"
    for call in mock_run.call_args_list:
        env = call.kwargs.get("env")
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ctor-tok"


# ---------------------------------------------------------------------------
# invoke_async() — 동일 계약, run_subprocess_async 경유
# ---------------------------------------------------------------------------

def test_invoke_async_no_token_passes_env_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b'{"result":"ok"}', b"", 0, False))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        asyncio.run(p.invoke_async([Message(role="user", content="hi")]))
    assert mock_run.call_args.kwargs.get("env") is None


def test_invoke_async_token_reaches_subprocess_env():
    with patch("agentcli.providers.claude.run_subprocess_async",
               new=AsyncMock(
                   return_value=(b'{"result":"ok"}', b"", 0, False))) as mock_run, \
         patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"):
        p = ClaudeProvider()
        asyncio.run(p.invoke_async(
            [Message(role="user", content="hi")], oauth_token="async-tok"))
    env = mock_run.call_args.kwargs.get("env")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "async-tok"


# ---------------------------------------------------------------------------
# stream_async() — env 를 _run_stream_template 을 통해 실제 subprocess spawn 에 전달
# ---------------------------------------------------------------------------

def test_stream_async_no_token_omits_env_kwarg(monkeypatch, tmp_path):
    from tests._stream_helpers import make_fake_proc, jsonl_bytes

    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    proc = make_fake_proc(stdout_lines=jsonl_bytes([
        {"type": "result", "subtype": "success", "result": "ok",
         "session_id": "sid-1", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]), returncode=0)
    captured = {}

    async def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(ClaudeProvider, "_find_binary",
                        lambda self: "/usr/bin/claude")
    prov = ClaudeProvider()

    async def run():
        return [c async for c in
                prov.stream_async([Message(role="user", content="hi")])]

    asyncio.run(run())
    assert "env" not in captured


def test_stream_async_token_reaches_subprocess_env(monkeypatch):
    from tests._stream_helpers import make_fake_proc, jsonl_bytes

    proc = make_fake_proc(stdout_lines=jsonl_bytes([
        {"type": "result", "subtype": "success", "result": "ok",
         "session_id": "sid-1", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]), returncode=0)
    captured = {}

    async def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(ClaudeProvider, "_find_binary",
                        lambda self: "/usr/bin/claude")
    prov = ClaudeProvider(oauth_token="stream-tok")

    async def run():
        return [c async for c in
                prov.stream_async([Message(role="user", content="hi")])]

    asyncio.run(run())
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "stream-tok"


# ---------------------------------------------------------------------------
# 토큰 절대 미노출 — argv / debug trace
# ---------------------------------------------------------------------------

@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_oauth_token_never_appears_in_argv(mock_find, mock_run):
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    p = ClaudeProvider(oauth_token="super-secret-tok")
    p.invoke([Message(role="user", content="hi")])
    cmd = mock_run.call_args.args[0]
    assert all("super-secret-tok" not in str(arg) for arg in cmd)


@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary", return_value="/usr/bin/claude")
def test_oauth_token_not_in_debug_trace(mock_find, mock_run, tmp_path):
    mock_run.return_value = _sync(stdout='{"result":"ok"}', stderr="mcp connect...\n")
    trace = tmp_path / "trace.jsonl"
    p = ClaudeProvider(oauth_token="super-secret-tok",
                       debug=True, debug_log_path=str(trace))
    p.invoke([Message(role="user", content="hi")])
    assert trace.exists()
    assert "super-secret-tok" not in trace.read_text(encoding="utf-8")
