"""issue #56 — 환경 격리(--safe-mode)가 lean 에 묶여 임베딩에서 놓치기 쉽다.

호스트 앱에 임베드된 agentcli 가 spawn 한 claude 는 호스트의 Claude Code
환경(MCP 서버/skills/CLAUDE.md)을 통째로 상속한다 — 이슈 실측: 같은 요청이
``allowed_tools`` 만으로는 794k 토큰/타임아웃, ``lean=True`` + allowed_tools
는 196k/성공. "호스트 환경을 상속할지"와 "툴이 필요한지"는 독립 관심사라
``isolated`` 로 분리한다.

플래그 의미 실측(Claude Code 2.1.229, macOS):
- ``--safe-mode`` 단독: 커스터마이즈 차단, 빌트인 툴 전부 유지(Bash 동작 확인)
- ``--safe-mode --tools Bash``: 툴 정의 컨텍스트가 1/3 로 줄어든 검증된 조합
- ``--safe-mode --allowedTools Bash``: 컨텍스트 감소 없음 — allowedTools 는
  권한 게이트일 뿐이라 isolated 는 ``--tools`` 쪽으로 배선한다
- ``--safe-mode --disallowedTools Bash``: 차단 동작 확인 — 전달 유지
"""

import asyncio
from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message

_BIN = patch("agentcli.providers.claude.ClaudeProvider._find_binary",
             return_value="/usr/bin/claude")


@_BIN
def test_isolated_adds_safe_mode_and_keeps_builtin_tools(mock_find):
    """isolated=True 는 --safe-mode 만 — 빌트인 툴셋은 건드리지 않는다."""
    cmd, _ = ClaudeProvider(isolated=True)._build_cmd("hi", "", "", "json")
    assert "--safe-mode" in cmd
    assert "--tools" not in cmd
    assert "--allowedTools" not in cmd


@_BIN
def test_isolated_with_allowed_tools_uses_tools_allowlist(mock_find):
    """isolated + allowed_tools 는 --tools 로 간다 — --allowedTools 는 권한
    게이트일 뿐 툴 정의 컨텍스트를 줄이지 못한다(모듈 docstring 실측)."""
    cmd, _ = ClaudeProvider(isolated=True, allowed_tools=["Bash", "Read"]) \
        ._build_cmd("hi", "", "", "json")
    assert "--safe-mode" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read"
    assert "--allowedTools" not in cmd


@_BIN
def test_isolated_keeps_disallowed_gate_but_drops_mcp(mock_find):
    """safe-mode 가 MCP 를 끄므로 mcp_config 는 무의미 — 조용히 버린다.
    --disallowedTools 는 safe-mode 밑에서도 차단이 실측 확인돼 전달 유지."""
    p = ClaudeProvider(isolated=True, disallowed_tools=["Bash"])
    cmd, _ = p._build_cmd("hi", "", "", "json",
                          mcp_config={"pair": {"url": "http://x/mcp"}})
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash"
    assert "--mcp-config" not in cmd


@_BIN
def test_isolated_per_call_override_both_directions(mock_find):
    """다른 오버라이드(lean/permission_mode 등)와 같은 호출 시점 계약."""
    on = ClaudeProvider()._build_cmd("hi", "", "", "json", isolated=True)[0]
    assert "--safe-mode" in on
    off = ClaudeProvider(isolated=True) \
        ._build_cmd("hi", "", "", "json", isolated=False)[0]
    assert "--safe-mode" not in off


@_BIN
def test_lean_semantics_unchanged_and_win_over_isolated(mock_find):
    """lean 은 기존대로(--safe-mode + --tools allowlist) — isolated 와 겹치면
    더 좁은 lean 이 이긴다. --safe-mode 가 중복으로 붙지 않는다."""
    cmd, _ = ClaudeProvider(lean=True, isolated=True) \
        ._build_cmd("hi", "", "", "json")
    assert cmd.count("--safe-mode") == 1
    assert cmd[cmd.index("--tools") + 1] == ""


@_BIN
def test_default_still_inherits_host_env(mock_find):
    """기본값은 기존 동작 그대로 — 상속(비격리). 하위호환 고정."""
    cmd, _ = ClaudeProvider()._build_cmd("hi", "", "", "json")
    assert "--safe-mode" not in cmd


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_invoke_async_threads_isolated(mock_bc):
    asyncio.run(ClaudeProvider().invoke_async(
        [Message(role="user", content="hi")], isolated=True))
    assert mock_bc.call_args.kwargs["isolated"] is True


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_stream_async_threads_isolated(mock_bc):
    async def drain():
        async for _ in ClaudeProvider().stream_async(
                [Message(role="user", content="hi")], isolated=True):
            pass
    asyncio.run(drain())
    assert mock_bc.call_args.kwargs["isolated"] is True
