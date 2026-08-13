"""issue #62 — spawn 고속화: 비필수 트래픽 차단 기본화 + 캐시 안정 프리픽스 옵션.

실측(Claude Code 2.1.229, macOS):
- ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` → lean 1회 호출 부팅
  3.0~3.1s → 2.0s (2/2 재현). 자식 spawn 에만 얹으므로 사용자의 대화형
  claude 자동업데이트에는 영향 없다.
- ``--exclude-dynamic-system-prompt-sections`` → 사이에 git 상태 변경을
  넣어도 cache_create 1693 → 519, cache_read 3289 → 4219 — 동적 섹션 때문에
  매 호출 재생성되던 ~1.2k 토큰이 캐시로 이동. 구버전 CLI 는 이 플래그가
  없어 unknown-option 에러가 나므로 **옵트인**이다.
"""

import asyncio
from unittest.mock import patch

from agentcli.providers import warm as warm_mod
from agentcli.providers.claude import (ClaudeProvider,
                                       NONESSENTIAL_TRAFFIC_ENV_VAR)
from agentcli.providers.warm import WarmSession
from agentcli.types import Message

_BIN = patch("agentcli.providers.claude.ClaudeProvider._find_binary",
             return_value="/usr/bin/claude")


def _no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)


# ---- 비필수 트래픽 차단 기본화 ----

def test_spawn_env_sets_traffic_var_by_default(monkeypatch, tmp_path):
    _no_token(monkeypatch, tmp_path)
    monkeypatch.delenv(NONESSENTIAL_TRAFFIC_ENV_VAR, raising=False)
    monkeypatch.setenv("SOME_MARKER_VAR", "marker")
    env = ClaudeProvider()._spawn_env(None)
    assert env is not None
    assert env[NONESSENTIAL_TRAFFIC_ENV_VAR] == "1"
    assert env["SOME_MARKER_VAR"] == "marker"      # 부모 env 는 그대로 상속


def test_parent_definition_wins(monkeypatch, tmp_path):
    """부모가 명시하면 그 값이 이긴다 — 얹을 게 없으니 None(상속 경로)."""
    _no_token(monkeypatch, tmp_path)
    monkeypatch.setenv(NONESSENTIAL_TRAFFIC_ENV_VAR, "0")
    assert ClaudeProvider()._spawn_env(None) is None


def test_token_and_traffic_default_compose(monkeypatch, tmp_path):
    monkeypatch.delenv(NONESSENTIAL_TRAFFIC_ENV_VAR, raising=False)
    env = ClaudeProvider(oauth_token="tok-62")._spawn_env(None)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-62"
    assert env[NONESSENTIAL_TRAFFIC_ENV_VAR] == "1"


@patch("agentcli.providers.claude.run_subprocess_sync")
@_BIN
def test_invoke_env_carries_traffic_var(mock_find, mock_run,
                                        monkeypatch, tmp_path):
    from tests.test_claude_auth import _sync
    _no_token(monkeypatch, tmp_path)
    monkeypatch.delenv(NONESSENTIAL_TRAFFIC_ENV_VAR, raising=False)
    mock_run.return_value = _sync(stdout='{"result":"ok"}')
    ClaudeProvider().invoke([Message(role="user", content="hi")])
    env = mock_run.call_args.kwargs.get("env")
    assert env is not None and env[NONESSENTIAL_TRAFFIC_ENV_VAR] == "1"


def test_warm_spawn_env_carries_traffic_var(monkeypatch):
    monkeypatch.delenv(NONESSENTIAL_TRAFFIC_ENV_VAR, raising=False)
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("spawn 중단 — kwargs 만 확인")

    async def run(sess):
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await sess.send("q")

    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(run(WarmSession(cmd=["claude"])))
    assert captured["env"][NONESSENTIAL_TRAFFIC_ENV_VAR] == "1"

    # 호출자가 env 를 명시해도 그 dict 를 훼손하지 않고 기본만 보충한다
    captured.clear()
    caller_env = {"X_MARKER": "1"}
    with pytest.raises(RuntimeError):
        asyncio.run(run(WarmSession(cmd=["claude"], env=caller_env)))
    assert captured["env"]["X_MARKER"] == "1"
    assert captured["env"][NONESSENTIAL_TRAFFIC_ENV_VAR] == "1"
    assert NONESSENTIAL_TRAFFIC_ENV_VAR not in caller_env   # 원본 불변

    # 호출자가 명시적으로 끈 값은 존중
    captured.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(run(WarmSession(
            cmd=["claude"], env={NONESSENTIAL_TRAFFIC_ENV_VAR: "0"})))
    assert captured["env"][NONESSENTIAL_TRAFFIC_ENV_VAR] == "0"


# ---- --exclude-dynamic-system-prompt-sections 옵트인 ----

@_BIN
def test_exclude_dynamic_flag_is_optin(mock_find):
    flag = "--exclude-dynamic-system-prompt-sections"
    cmd, _ = ClaudeProvider()._build_cmd("hi", "", "", "json")
    assert flag not in cmd                                   # 기본 꺼짐
    cmd, _ = ClaudeProvider(exclude_dynamic_system_prompt=True) \
        ._build_cmd("hi", "", "", "json")
    assert flag in cmd


@_BIN
def test_exclude_dynamic_per_call_override(mock_find):
    flag = "--exclude-dynamic-system-prompt-sections"
    on, _ = ClaudeProvider()._build_cmd(
        "hi", "", "", "json", exclude_dynamic_system_prompt=True)
    assert flag in on
    off, _ = ClaudeProvider(exclude_dynamic_system_prompt=True)._build_cmd(
        "hi", "", "", "json", exclude_dynamic_system_prompt=False)
    assert flag not in off


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_invoke_async_threads_exclude_flag(mock_bc):
    asyncio.run(ClaudeProvider().invoke_async(
        [Message(role="user", content="hi")],
        exclude_dynamic_system_prompt=True))
    assert mock_bc.call_args.kwargs["exclude_dynamic_system_prompt"] is True
