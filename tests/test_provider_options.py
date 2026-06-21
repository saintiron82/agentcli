"""#154 — call-time provider_options: claude --mcp-config pass-through +
per-call permission/tools override, codex per-call sandbox override, and the
LLMClient.chat(provider_options=...) plumbing."""
import json
from unittest.mock import patch

from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider


# ===== ClaudeProvider._build_cmd: --mcp-config + overrides =====

@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_emits_mcp_config_wrapped(mock_find):
    p = ClaudeProvider()
    cmd, _ = p._build_cmd(
        "hi", "", "", "json",
        mcp_config={"pair": {"url": "https://x/mcp",
                             "headers": {"Authorization": "Bearer t"}}})
    assert "--mcp-config" in cmd
    payload = cmd[cmd.index("--mcp-config") + 1]
    parsed = json.loads(payload)
    # bare server dict gets wrapped under the mcpServers key claude expects
    assert parsed["mcpServers"]["pair"]["url"] == "https://x/mcp"
    assert parsed["mcpServers"]["pair"]["headers"]["Authorization"] == "Bearer t"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_mcp_config_already_wrapped_passes_through(mock_find):
    p = ClaudeProvider()
    cfg = {"mcpServers": {"pair": {"url": "u"}}}
    cmd, _ = p._build_cmd("hi", "", "", "json", mcp_config=cfg)
    payload = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert payload == cfg  # not double-wrapped


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_mcp_config_string_passed_directly(mock_find):
    p = ClaudeProvider()
    cmd, _ = p._build_cmd("hi", "", "", "json", mcp_config="/path/to/servers.json")
    assert cmd[cmd.index("--mcp-config") + 1] == "/path/to/servers.json"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_strict_mcp_config_flag(mock_find):
    p = ClaudeProvider()
    cmd, _ = p._build_cmd("hi", "", "", "json",
                          mcp_config={"pair": {"url": "u"}},
                          strict_mcp_config=True)
    assert "--strict-mcp-config" in cmd


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_no_mcp_flag_when_absent(mock_find):
    p = ClaudeProvider()
    cmd, _ = p._build_cmd("hi", "", "", "json")
    assert "--mcp-config" not in cmd
    assert "--strict-mcp-config" not in cmd


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_permission_mode_override_beats_constructor(mock_find):
    p = ClaudeProvider(permission_mode="plan")
    cmd, _ = p._build_cmd("hi", "", "", "json", permission_mode="acceptEdits")
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_permission_mode_falls_back_to_constructor(mock_find):
    p = ClaudeProvider(permission_mode="plan")
    cmd, _ = p._build_cmd("hi", "", "", "json")
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_build_cmd_allowed_tools_override(mock_find):
    p = ClaudeProvider(allowed_tools=["Read"])
    cmd, _ = p._build_cmd("hi", "", "", "json",
                          allowed_tools=["mcp__pair__create_issue", "Edit"])
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__pair__create_issue,Edit"


# ===== CodexProvider._build_cmd: per-call sandbox override =====

@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
def test_codex_build_cmd_sandbox_override(mock_find):
    p = CodexProvider(sandbox_mode="read-only")
    cmd = p._build_cmd("hi", "", None, "", sandbox_mode="workspace-write")
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "workspace-write"


@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
def test_codex_build_cmd_sandbox_falls_back_to_constructor(mock_find):
    p = CodexProvider(sandbox_mode="read-only")
    cmd = p._build_cmd("hi", "", None, "")
    assert cmd[cmd.index("-s") + 1] == "read-only"


# ===== invoke threads overrides into the subprocess cmd =====

from unittest.mock import MagicMock
from agentcli.types import Message


@patch("agentcli.providers.claude.subprocess.run")
@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_claude_invoke_threads_mcp_config_to_cmd(mock_find, mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"result","subtype":"success","result":"ok",'
               '"session_id":"s","usage":{}}',
        stderr="")
    p = ClaudeProvider()
    p.invoke([Message(role="user", content="hi")],
             mcp_config={"pair": {"url": "https://x/mcp"}},
             permission_mode="acceptEdits",
             allowed_tools=["mcp__pair__add_comment"])
    cmd = mock_run.call_args[0][0]
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__pair__add_comment"


# ===== LLMClient.chat(provider_options=...) plumbing =====

from agentcli.client import LLMClient
from agentcli.providers.registry import ProviderRegistry
from agentcli.providers.base import LLMProvider
from agentcli.store.memory import MemoryStore
from agentcli.types import LLMResponse, TokenUsage


class RecordingProvider(LLMProvider):
    provider_id = "rec"
    supports_sessions = False
    stores_history = True

    def __init__(self):
        self.captured = None

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None, mcp_config=None, permission_mode=None):
        self.captured = {"mcp_config": mcp_config,
                         "permission_mode": permission_mode}
        return LLMResponse(content="ok", provider="rec", model=model,
                           tokens=TokenUsage())

    def list_models(self):
        return [{"id": "", "name": "rec"}]

    def is_available(self):
        return True


def _client_with(provider):
    reg = ProviderRegistry()
    reg.register(provider)
    reg.set_fallback_order([provider.provider_id])
    return LLMClient(MemoryStore(), registry=reg)


def test_chat_provider_options_reach_provider():
    rec = RecordingProvider()
    client = _client_with(rec)
    client.chat("hi", provider="rec", owner="o",
                provider_options={"mcp_config": {"pair": {"url": "u"}},
                                  "permission_mode": "acceptEdits"})
    assert rec.captured["mcp_config"] == {"pair": {"url": "u"}}
    assert rec.captured["permission_mode"] == "acceptEdits"


def test_chat_drops_provider_options_unsupported_by_provider():
    rec = RecordingProvider()
    client = _client_with(rec)
    # 'sandbox_mode' isn't in RecordingProvider.invoke — must be dropped, no crash
    resp = client.chat("hi", provider="rec", owner="o",
                       provider_options={"sandbox_mode": "workspace-write",
                                         "mcp_config": {"x": 1}})
    assert resp.content == "ok"
    assert rec.captured["mcp_config"] == {"x": 1}


# ===== remaining call paths thread overrides into _build_cmd =====
import asyncio


async def _drain(agen):
    return [c async for c in agen]


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_claude_invoke_async_forwards_overrides(mock_bc):
    p = ClaudeProvider()
    asyncio.run(p.invoke_async([Message(role="user", content="hi")],
                               mcp_config={"x": 1}, permission_mode="acceptEdits"))
    kwargs = mock_bc.call_args.kwargs
    assert kwargs["mcp_config"] == {"x": 1}
    assert kwargs["permission_mode"] == "acceptEdits"


@patch.object(ClaudeProvider, "_build_cmd", return_value=(None, ""))
def test_claude_stream_async_forwards_overrides(mock_bc):
    p = ClaudeProvider()
    asyncio.run(_drain(p.stream_async([Message(role="user", content="hi")],
                                      mcp_config={"x": 1},
                                      allowed_tools=["Edit"])))
    kwargs = mock_bc.call_args.kwargs
    assert kwargs["mcp_config"] == {"x": 1}
    assert kwargs["allowed_tools"] == ["Edit"]


@patch.object(CodexProvider, "_build_cmd", return_value=None)
def test_codex_invoke_forwards_sandbox(mock_bc):
    p = CodexProvider()
    p.invoke([Message(role="user", content="hi")], sandbox_mode="workspace-write")
    assert mock_bc.call_args.kwargs["sandbox_mode"] == "workspace-write"


@patch.object(CodexProvider, "_build_cmd", return_value=None)
def test_codex_invoke_async_forwards_sandbox(mock_bc):
    p = CodexProvider()
    asyncio.run(p.invoke_async([Message(role="user", content="hi")],
                               sandbox_mode="workspace-write"))
    assert mock_bc.call_args.kwargs["sandbox_mode"] == "workspace-write"


@patch.object(CodexProvider, "_build_cmd", return_value=None)
def test_codex_stream_async_forwards_sandbox(mock_bc):
    p = CodexProvider()
    asyncio.run(_drain(p.stream_async([Message(role="user", content="hi")],
                                      sandbox_mode="workspace-write")))
    assert mock_bc.call_args.kwargs["sandbox_mode"] == "workspace-write"


# ===== codex MCP pass-through via -c mcp_servers.<name> (#154 follow-up C) =====

@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
def test_codex_build_cmd_emits_mcp_config_http(mock_find):
    p = CodexProvider()
    cmd = p._build_cmd("hi", "", None, "",
                       mcp_config={"pair": {"url": "http://x/mcp"}})
    assert "-c" in cmd
    joined = " ".join(cmd)
    assert "mcp_servers.pair=" in joined
    assert '"http://x/mcp"' in joined   # url serialized as a TOML/JSON string


@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
def test_codex_build_cmd_emits_mcp_config_stdio(mock_find):
    p = CodexProvider()
    cmd = p._build_cmd("hi", "", None, "",
                       mcp_config={"demo": {"command": "python",
                                            "args": ["/tmp/m.py"]}})
    joined = " ".join(cmd)
    assert "mcp_servers.demo=" in joined
    assert '"python"' in joined and '"/tmp/m.py"' in joined


@patch("agentcli.providers.codex.CodexProvider._find_binary",
       return_value="/usr/bin/codex")
def test_codex_build_cmd_no_mcp_flag_when_absent(mock_find):
    p = CodexProvider()
    cmd = p._build_cmd("hi", "", None, "")
    assert "mcp_servers." not in " ".join(cmd)


@patch.object(CodexProvider, "_build_cmd", return_value=None)
def test_codex_invoke_forwards_mcp_config(mock_bc):
    p = CodexProvider()
    p.invoke([Message(role="user", content="hi")],
             mcp_config={"x": {"url": "u"}})
    assert mock_bc.call_args.kwargs["mcp_config"] == {"x": {"url": "u"}}


@patch.object(CodexProvider, "_build_cmd", return_value=None)
def test_codex_stream_async_forwards_mcp_config(mock_bc):
    p = CodexProvider()
    asyncio.run(_drain(p.stream_async([Message(role="user", content="hi")],
                                      mcp_config={"x": {"url": "u"}})))
    assert mock_bc.call_args.kwargs["mcp_config"] == {"x": {"url": "u"}}
