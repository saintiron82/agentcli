"""Regression tests for issue #4 (Windows) + claude session resume (POSIX).

Issue #4: on Windows, attaching ``--resume <sid>`` to a ``-p`` (print-mode)
invocation falls back to waiting for interactive input and hangs for 5+
minutes. The fix is platform-scoped: ``ClaudeProvider.supports_sessions`` is
False on Windows, and ``_build_cmd`` never emits ``--resume`` in that mode.

On macOS/Linux, ``claude -p --resume <sid>`` works correctly (verified
against Claude Code 2.1.x: the resumed session keeps the same session ID),
so the provider resumes natively there.

The stateless-mode guarantee is locked in by forcing ``supports_sessions``
to False on a provider instance, which is exactly what the class attribute
evaluates to on Windows.

Reference: https://github.com/saintiron82/agentcli/issues/4
"""

from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider


def _stateless_provider() -> ClaudeProvider:
    """Simulate Windows mode: supports_sessions=False on the instance."""
    p = ClaudeProvider()
    p.supports_sessions = False
    return p


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_no_resume_in_stateless_mode(_mock_find):
    """Windows(stateless) 모드에서는 session_id가 와도 ``--resume`` 금지."""
    p = _stateless_provider()
    cmd, used_sid = p._build_cmd(
        prompt="hello",
        model="",
        session_id="stale-uuid-from-prior-call",
        output_format="json",
    )
    assert cmd is not None
    assert "-p" in cmd, "claude provider uses -p (print) mode"
    assert "--resume" not in cmd, (
        "stateless mode must never attach --resume: it is the upstream "
        "cause of the 5-minute Windows hang reported in issue #4"
    )
    assert "--session-id" in cmd
    assert used_sid != "stale-uuid-from-prior-call"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_no_resume_in_stateless_stream_mode(_mock_find):
    """Same structural guarantee for stream-json output mode (also -p)."""
    p = _stateless_provider()
    cmd, _ = p._build_cmd(
        prompt="hello",
        model="",
        session_id="stale-uuid-from-prior-call",
        output_format="stream-json",
    )
    assert cmd is not None
    assert "-p" in cmd
    assert "--resume" not in cmd


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_session_id_still_generated_for_audit(_mock_find):
    """Stateless 모드에서도 usage/audit 식별용 ``--session-id``는 발급."""
    p = _stateless_provider()
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="", session_id="", output_format="json")
    assert cmd is not None
    assert "--session-id" in cmd
    assert used_sid, "fresh uuid should be generated when no sid provided"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_session_mode_resumes_with_stored_sid(_mock_find):
    """세션 모드(macOS/Linux)에서는 저장된 sid로 ``--resume``."""
    p = ClaudeProvider()
    p.supports_sessions = True  # 플랫폼 무관하게 세션 모드 검증
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="",
        session_id="aaaa1111-2222-3333-4444-555566667777",
        output_format="json")
    assert cmd is not None
    assert "--resume" in cmd
    ridx = cmd.index("--resume")
    assert cmd[ridx + 1] == "aaaa1111-2222-3333-4444-555566667777"
    assert "--session-id" not in cmd
    assert used_sid == "aaaa1111-2222-3333-4444-555566667777", (
        "claude -p --resume keeps the same session id (verified 2.1.x)")


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_session_mode_new_session_mints_session_id(_mock_find):
    """세션 모드에서 sid가 없으면 새 ``--session-id`` 발급."""
    p = ClaudeProvider()
    p.supports_sessions = True
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="", session_id="", output_format="json")
    assert cmd is not None
    assert "--session-id" in cmd
    assert "--resume" not in cmd
    assert used_sid
