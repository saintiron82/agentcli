"""issue #42 — debug trace argv redaction 의 provider 형태별 커버리지.

``redact_argv`` 는 원래 claude 의 ``-p <prompt>`` 형태만 알았다. 실측 결과
copilot 도 ``-p`` 를 쓰므로 이미 커버였고(이 파일이 회귀로 고정한다), 실제
유출은 **codex** — 프롬프트가 ``--`` 종결자 뒤 마지막 위치 인자로 실려
(resume: ``-- <sid> <prompt>`` / 신규: ``-- <prompt>``) 그대로 trace 에
기록됐다. stdin 모드의 placeholder(``"-"``) 는 내용이 없으므로 남겨서
trace 에 stdin 경유임이 보이게 한다.
"""

from agentcli.providers.base import redact_argv

SECRET = "민감한 기사 본문 SECRET-9917"


def test_claude_p_form_redacted():
    r = redact_argv(["claude", "-p", SECRET, "--output-format", "json"])
    assert SECRET not in r
    assert r[2].startswith("<prompt:")


def test_copilot_p_form_redacted():
    """copilot 도 ``-p`` 형태라 기존 로직이 커버한다 — 회귀로 고정."""
    r = redact_argv(["copilot", "-p", SECRET, "--no-color"])
    assert SECRET not in r
    assert r[2].startswith("<prompt:")


def test_codex_resume_form_redacts_prompt_keeps_sid():
    r = redact_argv(["codex", "exec", "--json", "--", "sid-1", SECRET])
    assert SECRET not in r
    assert "sid-1" in r                       # 세션 id 는 진단 가치가 있어 유지
    assert r[-1].startswith("<prompt:")


def test_codex_new_session_form_redacted():
    r = redact_argv(["codex", "exec", "--json", "--", SECRET])
    assert SECRET not in r
    assert r[-1].startswith("<prompt:")


def test_codex_stdin_placeholder_preserved():
    """stdin 모드 placeholder 는 내용이 없다 — 가리지 않아야 stdin 경유가 보인다."""
    r = redact_argv(["codex", "exec", "--json", "--", "sid-1", "-"])
    assert r[-1] == "-"
