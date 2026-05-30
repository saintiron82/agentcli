"""Regression tests for issue #4.

Issue: claude provider passes ``--resume`` alongside ``-p`` (print /
single-shot) mode. ``-p`` is stateless and has no concept of resuming an
interactive session, so attaching ``--resume <stale_sid>`` to a print-mode
invocation is structurally incompatible. On Windows this manifests as a
5-minute hang followed by a wall-clock timeout (issue #4 reporter).

The deadlock itself is OS-dependent (Windows pipe semantics), but the
*upstream cause* — the library deciding to attach ``--resume`` in ``-p``
mode — is OS-independent and can be locked in with structural assertions
against ``ClaudeProvider._build_cmd``.

Reference: https://github.com/saintiron82/agentcli/issues/4
"""

from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_no_resume_in_print_mode(_mock_find):
    """``--resume`` must not be appended in single-shot ``-p`` mode."""
    p = ClaudeProvider()
    cmd, _ = p._build_cmd(
        prompt="hello",
        model="",
        session_id="stale-uuid-from-prior-call",
        output_format="json",
    )
    assert cmd is not None
    assert "-p" in cmd, "claude provider uses -p (print) mode"
    assert "--resume" not in cmd, (
        "claude -p mode is stateless; passing --resume alongside it is "
        "the upstream cause of the 5-minute hang reported in issue #4"
    )


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_no_resume_in_stream_mode(_mock_find):
    """Same structural guarantee for stream-json output mode (also -p)."""
    p = ClaudeProvider()
    cmd, _ = p._build_cmd(
        prompt="hello",
        model="",
        session_id="stale-uuid-from-prior-call",
        output_format="stream-json",
    )
    assert cmd is not None
    assert "-p" in cmd
    assert "--resume" not in cmd, (
        "stream-json output still uses -p (single-shot); --resume is "
        "incompatible per issue #4"
    )


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_issue_4_session_id_still_generated_for_audit(_mock_find):
    """Even though resume is dropped, the library should still emit a
    fresh ``--session-id`` for usage/audit identification."""
    p = ClaudeProvider()
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="", session_id="", output_format="json")
    assert cmd is not None
    assert "--session-id" in cmd
    assert used_sid, "fresh uuid should be generated when no sid provided"
