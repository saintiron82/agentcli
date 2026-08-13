"""issue #59 — env 티어: inherit / explicit / isolated / lean, 기본 explicit.

임베딩 라이브러리의 기본은 "호스트 환경을 통째로 상속"이 아니라 "호출자가
지정한 것만 + 빌트인 기초"여야 한다는 결정(#56 제안 3 → #59). 티어별 argv
계약과 실측 근거는 이슈 #59 의 표 참고. 핵심 실측(2.1.229):

- ``--setting-sources ""`` 는 ambient MCP/스킬을 끊으면서 **명시적
  ``--mcp-config`` 는 살린다** (unreachable 프로브 서버가 init 이벤트에
  ``failed`` 로 시도됨) — safe-mode 는 명시 MCP 까지 죽이므로(``mcp_servers:
  []``) "지정한 것만 붙이기"는 이 조합으로만 가능하다.
- ``--bare`` 는 OAuth/키체인을 절대 읽지 않아 구독 인증에서 사용 불가.
"""

import asyncio

import pytest
from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message

_BIN = patch("agentcli.providers.claude.ClaudeProvider._find_binary",
             return_value="/usr/bin/claude")


# ---- 기본값: explicit ----

@_BIN
def test_default_tier_is_explicit(mock_find):
    """기본이 explicit — 지정한 것만 + 빌트인 툴 (#59 브레이킹 결정)."""
    cmd, _ = ClaudeProvider()._build_cmd("hi", "", "", "json")
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--safe-mode" not in cmd          # 빌트인 툴은 살아 있다
    assert "--tools" not in cmd


@_BIN
def test_explicit_keeps_caller_mcp_config(mock_find):
    """explicit 의 존재 이유 — 호출자가 지정한 MCP 만 붙는다."""
    cmd, _ = ClaudeProvider()._build_cmd(
        "hi", "", "", "json", mcp_config={"pair": {"url": "http://x/mcp"}})
    assert "--mcp-config" in cmd
    assert cmd.count("--strict-mcp-config") == 1


@_BIN
def test_explicit_mcp_tool_names_stay_on_permission_gate(mock_find):
    """--tools 는 빌트인 전용 — mcp__ 이름이 섞이면 --allowedTools 로 가야
    mcp_config 로 붙인 서버의 툴을 좁히는 문서화된 패턴이 계속 동작한다."""
    cmd, _ = ClaudeProvider()._build_cmd(
        "hi", "", "", "json",
        mcp_config={"pair": {"url": "http://x/mcp"}},
        allowed_tools=["mcp__pair__add_comment", "Bash"])
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__pair__add_comment,Bash"
    assert "--tools" not in cmd


@_BIN
def test_explicit_routes_allowed_tools_to_tools(mock_find):
    """#56 과 같은 근거: --allowedTools 는 권한 게이트일 뿐 컨텍스트를 못
    줄인다 — 정의 allowlist 인 --tools 로 보낸다. disallowed 는 유지."""
    cmd, _ = ClaudeProvider(allowed_tools=["Bash"],
                            disallowed_tools=["WebSearch"]) \
        ._build_cmd("hi", "", "", "json")
    assert cmd[cmd.index("--tools") + 1] == "Bash"
    assert "--allowedTools" not in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == "WebSearch"


# ---- inherit: 기존 상속 동작 그대로 ----

@_BIN
def test_inherit_restores_legacy_behavior(mock_find):
    """env="inherit" 는 0.7.x 기본과 동일한 argv — 격리 플래그 없음,
    allowed_tools 는 --allowedTools 로."""
    cmd, _ = ClaudeProvider(env="inherit", allowed_tools=["Edit"]) \
        ._build_cmd("hi", "", "", "json")
    for flag in ("--setting-sources", "--disable-slash-commands",
                 "--strict-mcp-config", "--safe-mode"):
        assert flag not in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "Edit"


# ---- isolated / lean 티어와 부울 별칭 ----

def _sans_session(cmd: list[str]) -> list[str]:
    """자동 발급 --session-id <uuid> 를 뗀 argv — 별칭 동등성 비교용."""
    i = cmd.index("--session-id")
    return cmd[:i] + cmd[i + 2:]


@_BIN
def test_isolated_tier_equals_boolean_alias(mock_find):
    a, _ = ClaudeProvider(env="isolated")._build_cmd("hi", "", "", "json")
    b, _ = ClaudeProvider(isolated=True)._build_cmd("hi", "", "", "json")
    assert _sans_session(a) == _sans_session(b)
    assert "--safe-mode" in a and "--setting-sources" not in a


@_BIN
def test_lean_tier_equals_boolean_alias(mock_find):
    a, _ = ClaudeProvider(env="lean")._build_cmd("hi", "", "", "json")
    b, _ = ClaudeProvider(lean=True)._build_cmd("hi", "", "", "json")
    assert _sans_session(a) == _sans_session(b)
    assert "--safe-mode" in a
    assert a[a.index("--tools") + 1] == ""


# ---- 모호성 거부 ----

def test_env_with_boolean_alias_is_ambiguous():
    with pytest.raises(ValueError, match="env"):
        ClaudeProvider(env="inherit", lean=True)
    with pytest.raises(ValueError, match="env"):
        ClaudeProvider(env="explicit", isolated=True)


@_BIN
def test_per_call_env_with_boolean_is_ambiguous(mock_find):
    p = ClaudeProvider()
    with pytest.raises(ValueError, match="env"):
        p._build_cmd("hi", "", "", "json", env="inherit", lean=True)


def test_unknown_tier_rejected():
    with pytest.raises(ValueError, match="inherit"):
        ClaudeProvider(env="warm")


# ---- 호출 시점 오버라이드 ----

@_BIN
def test_per_call_env_override_both_directions(mock_find):
    inherit_cmd, _ = ClaudeProvider() \
        ._build_cmd("hi", "", "", "json", env="inherit")
    assert "--setting-sources" not in inherit_cmd
    explicit_cmd, _ = ClaudeProvider(env="inherit") \
        ._build_cmd("hi", "", "", "json", env="explicit")
    assert "--setting-sources" in explicit_cmd


@_BIN
def test_per_call_boolean_off_falls_back_to_default_tier(mock_find):
    """생성자 별칭을 호출 시점 False 로 끄면 새 기본(explicit)으로 떨어진다
    — 0.7.x 에서는 inherit 로 떨어졌다(#59 브레이킹 노트)."""
    cmd, _ = ClaudeProvider(lean=True)._build_cmd("hi", "", "", "json",
                                                  lean=False)
    assert "--safe-mode" not in cmd
    assert "--setting-sources" in cmd


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_invoke_async_threads_env(mock_bc):
    asyncio.run(ClaudeProvider().invoke_async(
        [Message(role="user", content="hi")], env="inherit"))
    assert mock_bc.call_args.kwargs["env"] == "inherit"


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_stream_async_threads_env(mock_bc):
    async def drain():
        async for _ in ClaudeProvider().stream_async(
                [Message(role="user", content="hi")], env="inherit"):
            pass
    asyncio.run(drain())
    assert mock_bc.call_args.kwargs["env"] == "inherit"
