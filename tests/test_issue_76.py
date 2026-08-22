"""issue #76 — codex-cli 0.149 가 ``--full-auto`` 를 제거 (0.139 은 경고만).

기본값 ``full_auto=True`` 가 두 경로(신규/resume)에서 플래그를 방출해
``provider="codex"`` 가 전면 실패했다 — #74(-a 제거)와 같은 플래그 churn
클래스의 재발. 의미는 원래 잉여였다: sandbox 는 항상 ``-s`` 로 나가고
승인은 ``-c approval_policy=``(#74). ``full_auto`` 는 하위호환 no-op 로
강등되어 어떤 경로에서도 방출되지 않는다.
"""

from unittest.mock import patch

from agentcli.providers.codex import CodexProvider

_BIN = patch("agentcli.providers.codex.CodexProvider._find_binary",
             return_value="/usr/bin/codex")


@_BIN
def test_default_ctor_never_emits_full_auto(mock_find):
    """기본 생성자(= create_default_registry 등록형)가 0.149 에서 살아야 한다."""
    cmd = CodexProvider()._build_cmd("hi", "", None, "")
    assert "--full-auto" not in cmd
    assert "-s" in cmd                      # sandbox 는 여전히 -s 로 전달


@_BIN
def test_resume_path_never_emits_full_auto(mock_find):
    cmd = CodexProvider()._build_cmd("hi", "", "sid-1", "")
    assert "--full-auto" not in cmd


@_BIN
def test_explicit_full_auto_true_is_noop(mock_find):
    """명시적으로 켜도 방출하지 않는다 — 0.149 에서 죽는 플래그이므로
    '사용자 의도 존중'보다 '전 호출 생존'이 우선 (deprecated no-op 계약)."""
    cmd = CodexProvider(full_auto=True)._build_cmd("hi", "", None, "")
    assert "--full-auto" not in cmd
