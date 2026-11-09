"""ExaoneProvider against a fake `exaone acp` launcher (no network)."""
import asyncio
import json

import pytest

from agentcli import AuthProfile, LLMClient, MemoryStore
from agentcli.providers.exaone import ExaoneProvider
from agentcli.providers.registry import ProviderRegistry, create_default_registry
from agentcli.types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_NETWORK, ERROR_TIMEOUT,
                            ERROR_USAGE_LIMIT, Message)
from tests._stream_helpers import write_fake_cli_launcher

NEW_ID = "11111111-1111-1111-1111-111111111111"
KNOWN_ID = "22222222-2222-2222-2222-222222222222"
STALE_ID = "33333333-3333-3333-3333-333333333333"

_FAKE = '''
import json, os, sys
LOG = __LOG__
MODE = __MODE__
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "key": os.environ.get("EXAONE_API_KEY"),
                         "home": os.environ.get("EXAONE_HOME"),
                         "base": os.environ.get("EXAONE_BASE_URL"),
                         "gh": os.environ.get("GITHUB_TOKEN")}) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("exaone 0.0.0-fake")
    sys.exit(1 if MODE == "version_fail" else 0)

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def update(sid, kind, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}})

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1,
              "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "__NEW__"}})
    elif method == "session/load":
        if params.get("sessionId") != "__KNOWN__":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "session not found"}})
        else:
            update(params["sessionId"], "user_message_chunk", "old q")
            update(params["sessionId"], "agent_message_chunk", "OLD")
            send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "session/prompt":
        sid = params["sessionId"]
        if MODE != "ok":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": MODE,
                  "data": {"kind": "x"}}})
            continue
        update(sid, "agent_thought_chunk", "thinking...")
        update(sid, "agent_message_chunk", "Hello")
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
              "update": {"sessionUpdate": "usage_update", "used": 12,
                         "_meta": {"completion_tokens": 5, "reasoning_tokens": 2}}}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
'''


@pytest.fixture
def fake_exaone(tmp_path):
    def make(mode="ok"):
        log = tmp_path / "calls.jsonl"
        script = (_FAKE.replace("__LOG__", repr(str(log))).replace("__MODE__", repr(mode))
                  .replace("__NEW__", NEW_ID).replace("__KNOWN__", KNOWN_ID))
        return write_fake_cli_launcher(tmp_path, script, name="exaone"), log
    return make


@pytest.fixture(autouse=True)
def host_env(monkeypatch):
    monkeypatch.setenv("EXAONE_API_KEY", "HOST_KEY")
    monkeypatch.delenv("EXAONE_BASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def calls(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def provider(binary, **kw):
    p = ExaoneProvider(**kw)
    p._find_binary = lambda: binary
    return p


def stream(p, **kw):
    async def collect():
        return [c async for c in p.stream_async([Message(role="user", content="hi")], **kw)]
    return asyncio.run(collect())


def test_new_session_streams_thinking_text_usage_and_reports_clamp(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    chunks = stream(provider(binary, effort="medium"), cwd=str(tmp_path))
    assert chunks[0].type == "event"
    assert chunks[0].data["reasoning"]["effort"] == {
        "requested": "medium", "applied": "high", "clamped": True, "supported": True}
    assert len([c for c in chunks if c.type == "event" and "reasoning" in c.data]) == 1
    assert [(c.type, c.content) for c in chunks if c.type in ("thinking", "text")] == [
        ("thinking", "thinking..."), ("text", "Hello")]
    done = chunks[-1]
    assert done.type == "done" and done.content == "Hello" and done.session_id == NEW_ID
    assert (done.usage.prompt_tokens, done.usage.completion_tokens, done.usage.total_tokens) == (12, 5, 17)
    assert done.usage.prompt_tokens_source == "exaone_cli_reported"
    call = calls(log)[0]
    assert call["argv"] == ["acp", "--model=k-exaone_v2.5", "--reasoning-effort=high"]
    assert call["key"] == "HOST_KEY" and call["gh"] is None


def test_resume_drops_replayed_history(fake_exaone, tmp_path):
    binary, _ = fake_exaone()
    chunks = stream(provider(binary), session_id=KNOWN_ID, cwd=str(tmp_path))
    assert [c.content for c in chunks if c.type == "text"] == ["Hello"]
    assert chunks[-1].session_id == KNOWN_ID


def test_stale_session_recovers_with_new_session(fake_exaone, tmp_path):
    binary, _ = fake_exaone()
    chunks = stream(provider(binary), session_id=STALE_ID, cwd=str(tmp_path))
    assert chunks[-1].type == "done" and chunks[-1].session_id == NEW_ID


def test_invoke_folds_and_reports_reasoning(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    r = provider(binary).invoke([Message(role="user", content="hi")], effort="max",
                                thinking="detailed", cwd=str(tmp_path))
    assert (r.content, r.error, r.session_id) == ("Hello", "", NEW_ID)
    assert r.reasoning.effort.applied == "max"
    assert r.reasoning.thinking.supported is False
    assert calls(log)[-1]["argv"][-1] == "--reasoning-effort=max"


def test_no_effort_means_no_flag_and_model_override(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    p = provider(binary)
    p.invoke([Message(role="user", content="hi")], cwd=str(tmp_path))
    p.invoke([Message(role="user", content="hi")], model="other-model", cwd=str(tmp_path))
    assert calls(log)[0]["argv"] == ["acp", "--model=k-exaone_v2.5"]
    assert calls(log)[1]["argv"] == ["acp", "--model=other-model"]


def test_model_alias_resolves_and_dash_model_stays_one_token(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    r = provider(binary).invoke([Message(role="user", content="hi")], model="default",
                                cwd=str(tmp_path))
    assert r.model == "k-exaone_v2.5"
    provider(binary, model="default").invoke([Message(role="user", content="hi")],
                                             cwd=str(tmp_path))
    chunks = stream(provider(binary), model="--help", cwd=str(tmp_path))
    assert chunks[-1].type == "done"
    assert [c["argv"] for c in calls(log)] == [
        ["acp", "--model=k-exaone_v2.5"], ["acp", "--model=k-exaone_v2.5"],
        ["acp", "--model=--help"]]


@pytest.mark.parametrize("message,expected", [
    ("authentication required: EXAONE_API_KEY is not set", ERROR_AUTH),
    ("authentication failed (HTTP 401)", ERROR_AUTH),
    ("rate limit exceeded (HTTP 429)", ERROR_USAGE_LIMIT),
    ("timeout: no data for 300s", ERROR_TIMEOUT),
    ("network error: ConnectionRefusedError", ERROR_NETWORK),
])
def test_exaone_cli_error_prefixes_classify(fake_exaone, tmp_path, message, expected):
    binary, _ = fake_exaone(mode=message)
    r = provider(binary).invoke([Message(role="user", content="hi")], cwd=str(tmp_path))
    assert r.error == message and r.error_type == expected


def test_binary_missing():
    p = ExaoneProvider()
    p._find_binary = lambda: None
    r = p.invoke([Message(role="user", content="hi")])
    assert (r.error, r.error_type) == ("exaone not found", ERROR_BINARY_MISSING)


def test_not_in_default_registry():
    reg = create_default_registry()
    assert reg.get("exaone") is None
    assert "exaone" not in reg.get_fallback_chain()


def test_capabilities_are_honest():
    caps = ExaoneProvider().capabilities()
    assert caps.sessions and caps.streaming
    assert not caps.token_streaming and not caps.session_liveness and not caps.debug
    assert caps.options == frozenset({"effort", "thinking"})
    assert caps.effort_levels == frozenset({"low", "high", "max"})
    assert caps.notes.startswith("unofficial")


def test_defaults_grant_no_tools_or_files():
    p = ExaoneProvider()
    assert p._trust_all is False and p._allow_file_reads is False and p._allow_file_writes is False


def test_service_profile_reaches_child(fake_exaone, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    binary, log = fake_exaone()
    auth = AuthProfile("svc", str(tmp_path / "svc"), exaone_api_key="PROFILE_KEY").prepare()
    reg = ProviderRegistry()
    reg.register(provider(binary))
    ws = tmp_path / "ws"
    ws.mkdir()
    r = LLMClient(MemoryStore(), reg, auth_profile=auth).chat("hi", provider="exaone", cwd=str(ws))
    assert r.error == "" and r.content == "Hello"
    call = calls(log)[-1]
    assert call["key"] == "PROFILE_KEY"
    assert call["home"] == str((tmp_path / "svc").resolve() / "exaone")
    assert call["base"] is None


def test_health_without_key_is_auth_required(fake_exaone, monkeypatch):
    monkeypatch.delenv("EXAONE_API_KEY")
    binary, _ = fake_exaone()
    h = provider(binary).health_check()
    assert (h.ok, h.status, h.auth_ok, h.error_type) == (False, "auth_required", False, ERROR_AUTH)


def test_health_with_key_is_ok_but_unverified(fake_exaone):
    binary, _ = fake_exaone()
    h = provider(binary).health_check()
    assert (h.ok, h.status, h.auth_ok) == (True, "ok", None)
    assert h.version == "exaone 0.0.0-fake"
    assert h.message == "EXAONE_API_KEY set; use probe=True to verify it"


def test_health_version_failure_is_cli_error(fake_exaone):
    binary, _ = fake_exaone(mode="version_fail")
    h = provider(binary).health_check()
    assert (h.ok, h.status, h.auth_ok, h.exit_code) == (False, "cli_error", None, 1)


def profile_client(binary, tmp_path):
    auth = AuthProfile("svc", str(tmp_path / "svc"), exaone_api_key="PROFILE_KEY").prepare()
    reg = ProviderRegistry()
    reg.register(provider(binary))
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return LLMClient(MemoryStore(), reg, auth_profile=auth), str(ws)


def assert_profile_child(call, tmp_path, key="PROFILE_KEY"):
    assert call["key"] == key
    assert call["home"] == str((tmp_path / "svc").resolve() / "exaone")
    assert call["base"] is None


def test_health_under_profile_without_probe_is_configured(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    client, _ = profile_client(binary, tmp_path)
    h = client.health_check("exaone")
    assert (h.ok, h.status, h.auth_ok) == (True, "configured", None)
    assert_profile_child(calls(log)[-1], tmp_path)


def test_probe_inside_running_loop_uses_profile(fake_exaone, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    binary, log = fake_exaone()
    client, ws = profile_client(binary, tmp_path)

    async def main():
        return client.health_check("exaone", probe=True, cwd=ws)

    h = asyncio.run(main())
    assert h.ok is True, h
    assert calls(log)[-1]["argv"][0] == "acp"
    assert_profile_child(calls(log)[-1], tmp_path)


def test_probe_inside_running_loop_without_profile(fake_exaone, tmp_path):
    binary, log = fake_exaone()

    async def main():
        return provider(binary).health_check(probe=True, cwd=str(tmp_path))

    h = asyncio.run(main())
    assert h.ok is True, h
    assert calls(log)[-1]["key"] == "HOST_KEY"


def test_profile_reaches_child_via_chat_async(fake_exaone, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    binary, log = fake_exaone()
    client, ws = profile_client(binary, tmp_path)
    r = asyncio.run(client.chat_async("hi", provider="exaone", cwd=ws))
    assert r.error == "" and r.content == "Hello"
    assert_profile_child(calls(log)[-1], tmp_path)


def test_profile_reaches_child_via_chat_stream(fake_exaone, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    binary, log = fake_exaone()
    client, ws = profile_client(binary, tmp_path)

    async def collect():
        return [c async for c in client.chat_stream("hi", provider="exaone", cwd=ws)]

    chunks = asyncio.run(collect())
    assert [c.content for c in chunks if c.type == "text"] == ["Hello"]
    assert_profile_child(calls(log)[-1], tmp_path)


def test_rotated_profile_key_reaches_child(fake_exaone, tmp_path):
    from dataclasses import replace
    binary, log = fake_exaone()
    client, ws = profile_client(binary, tmp_path)
    client.set_auth_profile(replace(client._auth_profile, exaone_api_key="ROTATED_KEY"))
    r = client.chat("hi", provider="exaone", cwd=ws)
    assert r.error == ""
    assert_profile_child(calls(log)[-1], tmp_path, key="ROTATED_KEY")


def test_health_binary_missing():
    p = ExaoneProvider()
    p._find_binary = lambda: None
    h = p.health_check()
    assert (h.ok, h.status, h.error_type) == (False, "binary_missing", ERROR_BINARY_MISSING)
