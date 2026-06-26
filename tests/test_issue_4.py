"""Regression tests for issue #4 (resume vs -p hang) + #27 (Windows guard).

Issue #4: attaching ``--resume <sid>`` to a ``-p`` (print-mode) invocation
fell back to waiting for interactive input and hung for 5+ minutes on Windows.
The original fix forced ``supports_sessions=False`` on Windows.

Issue #27: the real trigger was the interactive **stdin wait**; agentcli now
spawns every claude call with ``stdin=DEVNULL`` (base.py), so that wait can't
happen — #27 reproduces the CLI behavior on Windows 11
(``claude -p --resume <sid> < /dev/null`` resumes cleanly). The Windows guard
was removed and ``supports_sessions`` is now ``True`` on every platform
(end-to-end Windows verification of the change is pending).

These tests keep covering the *conditional* ``_build_cmd`` behavior: when
``supports_sessions`` is forced False (legacy/opt-in stateless mode) no
``--resume`` is emitted; when True (now the default everywhere) a stored sid
resumes. On macOS/Linux this resume is verified against Claude Code 2.1.x
(the resumed session keeps the same session ID).

Reference: https://github.com/saintiron82/agentcli/issues/4 , /issues/27
"""

import platform
from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider


def _stateless_provider() -> ClaudeProvider:
    """Force stateless mode (supports_sessions=False) on the instance."""
    p = ClaudeProvider()
    p.supports_sessions = False
    return p


def test_issue_27_supports_sessions_true_on_all_platforms():
    """#27: 플랫폼 가드 제거 — 클래스 기본값이 Windows 포함 항상 True."""
    assert ClaudeProvider.supports_sessions is True
    assert ClaudeProvider().supports_sessions is True, (
        f"on {platform.system()}: stdin=DEVNULL 로 #4 데드락이 없으므로 "
        "Windows 에서도 세션 resume 이 허용되어야 한다 (#27)")


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_no_resume_in_stateless_mode(_mock_find):
    """강제 stateless 모드에서는 session_id가 와도 ``--resume`` 금지."""
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
