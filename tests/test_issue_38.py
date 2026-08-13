"""issue #38 — 정규화 reasoning 컨트롤(#37) 후속 3건의 회귀 고정.

1. **생성자 fail-fast**: effort/thinking 기본값 오타는 선언 지점에서 즉시
   ValueError — 첫 호출까지 잠복하지 않는다.
2. **빈 문자열 정규화**: ``""`` 는 "미설정"(None) 으로 취급한다. 이전에는
   truthiness 우회로 검증 없이 생성자 기본값을 조용히 꺼버리는 함정이었다.
3. **argv e2e 갭**: invoke 가 effort 를 실제 argv 까지 전달하는 검증이
   copilot 에만 있었다 — claude(--effort)/codex(-c config) 를 같은 수준으로
   고정한다.
"""

from unittest.mock import MagicMock, patch

import pytest

from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.types import Message


# ---- 1. 생성자 fail-fast ----

@pytest.mark.parametrize("provider_cls", [ClaudeProvider, CodexProvider,
                                          CopilotProvider])
def test_ctor_rejects_unknown_effort(provider_cls):
    with pytest.raises(ValueError, match="unknown level"):
        provider_cls(effort="hihg")            # 오타는 선언 지점에서 터진다


@pytest.mark.parametrize("provider_cls", [ClaudeProvider, CodexProvider,
                                          CopilotProvider])
def test_ctor_rejects_unknown_thinking(provider_cls):
    with pytest.raises(ValueError, match="unknown level"):
        provider_cls(thinking="loud")


# ---- 2. 빈 문자열 = 미설정 ----

def test_ctor_empty_string_means_unset():
    p = ClaudeProvider(effort="", thinking="")     # 검증 통과, 플래그 없음
    args, res = p._reasoning_flags(None, None)
    assert args == [] and res is None


def test_per_call_empty_string_keeps_ctor_default():
    """호출 시점 "" 가 생성자 기본값을 조용히 끄던 함정 제거 — "" 는 미설정."""
    p = ClaudeProvider(effort="low")
    args, res = p._reasoning_flags("", None)
    assert args == ["--effort", "low"]
    assert res is not None and res.effort.applied == "low"


# ---- 3. claude/codex invoke → argv e2e (copilot 은 기존 테스트가 고정) ----

@patch("agentcli.providers.claude.run_subprocess_sync")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_claude_invoke_ctor_effort_reaches_argv(mock_find, mock_run):
    from tests.test_claude_auth import _sync
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    resp = ClaudeProvider(effort="high").invoke(
        [Message(role="user", content="hi")])
    cmd = mock_run.call_args[0][0]
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert resp.reasoning is not None
    assert resp.reasoning.effort.applied == "high"


@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_codex_invoke_ctor_effort_reaches_argv_clamped(mock_env, mock_run,
                                                       mock_find):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"A"}}\n',
        stderr="")
    resp = CodexProvider(effort="xhigh").invoke(
        [Message(role="user", content="hi")])
    cmd = mock_run.call_args[0][0]
    cfgs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"]
    assert any(c.startswith("model_reasoning_effort=") and "high" in c
               for c in cfgs)                      # codex 상한 high 로 clamp
    assert resp.reasoning.effort.applied == "high"
    assert resp.reasoning.effort.clamped is True
