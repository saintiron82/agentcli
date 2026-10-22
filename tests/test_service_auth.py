"""Service auth tests: fake credentials, no account switches or model requests."""
import asyncio
import importlib
import json
import os
import re
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import pytest
from agentcli import AuthProfile, AuthProfileError, LLMClient, MemoryStore, AccountUsage
from agentcli.auth_context import auth_scope, current_auth_profile
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.providers.warm import open_warm
from agentcli.types import StreamChunk


def profile(tmp_path, name):
    return AuthProfile(name, str(tmp_path / name),
                       claude_oauth_token="FAKE_CLAUDE_" + name,
                       codex_api_key="FAKE_CODEX_" + name,
                       github_token="FAKE_GITHUB_" + name).prepare()


def make_client(p, auth, store=None):
    registry = ProviderRegistry()
    registry.register(p)
    return LLMClient(store or MemoryStore(), registry, auth_profile=auth)


def wire(p, monkeypatch):
    monkeypatch.setattr(p, "_find_binary", lambda: ("fake-cli", False)
                        if p.provider_id == "copilot" else "fake-cli")
    module = importlib.import_module(type(p).__module__)
    if p.provider_id != "claude":
        monkeypatch.setattr(module, "build_env", lambda: (_ for _ in ()).throw(
            AssertionError("Service profile must never consult global gh auth/cache")))
    return module


def output(provider):
    events = {
        "claude": [{"type": "result", "subtype": "success", "result": "ok", "session_id": "fake-sid"}],
        "codex": [{"type": "thread.started", "thread_id": "fake-sid"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}],
        "copilot": [{"type": "assistant.message", "data": {"content": "ok"}},
                    {"type": "result", "sessionId": "fake-sid", "exitCode": 0}],
    }
    return "\n".join(json.dumps(e) for e in events[provider]).encode()


@pytest.mark.parametrize("cls", [ClaudeProvider, CodexProvider, CopilotProvider])
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_shared_provider_routes_two_services_independently(tmp_path, monkeypatch, cls, mode):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY", "GITHUB_TOKEN",
                 "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_PROVIDER_API_KEY"):
        monkeypatch.setenv(name, "FAKE_WRONG_GLOBAL")
    before = dict(os.environ)
    p = cls()
    module = wire(p, monkeypatch)
    ca, cb = make_client(p, a), make_client(p, b)
    captured = []
    if mode == "sync":
        barrier = threading.Barrier(2)
        def run(cmd, **kwargs):
            captured.append(kwargs["env"])
            barrier.wait(timeout=3)
            return subprocess.CompletedProcess(cmd, 0, output(p.provider_id).decode(), "")
        if cls is ClaudeProvider:
            monkeypatch.setattr(module, "run_subprocess_sync", lambda cmd, **kw:
                                (run(cmd, **kw).stdout.encode(), b"", 0, False))
        else:
            monkeypatch.setattr(module, "run_owned_command", run)
        results, errors = [], []
        def call(c):
            try:
                results.append(c.chat("hi", provider=p.provider_id, owner="service", alias="chat"))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call, args=(c,)) for c in (ca, cb)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        assert not errors and all(not t.is_alive() for t in threads)
        assert len(results) == 2 and all(r.content == "ok" for r in results)
    else:
        ready = asyncio.Event()
        async def run(cmd, **kwargs):
            captured.append(kwargs["env"])
            if len(captured) == 2: ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            return output(p.provider_id), b"", 0, False
        if mode == "async":
            monkeypatch.setattr(module, "run_subprocess_async", run)
        else:
            async def template(cmd, state, **kwargs):
                await run(cmd, **kwargs)
                yield StreamChunk(type="text", content="ok")
                yield StreamChunk(type="done", content="ok", session_id="fake-sid")
            monkeypatch.setattr(p, "_run_stream_template", template)
        async def call(c):
            opts = dict(provider=p.provider_id, owner="service", alias="chat")
            if mode == "async":
                return (await c.chat_async("hi", **opts)).content
            chunks = [ch async for ch in c.chat_stream("hi", **opts)]
            assert current_auth_profile() is None
            return chunks[-1].content
        async def both():
            return await asyncio.gather(call(ca), call(cb))
        assert asyncio.run(both()) == ["ok", "ok"]
    key = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "CODEX_API_KEY",
           "copilot": "COPILOT_GITHUB_TOKEN"}[p.provider_id]
    expected = {"FAKE_CLAUDE_a", "FAKE_CLAUDE_b"} if cls is ClaudeProvider else (
        {"FAKE_CODEX_a", "FAKE_CODEX_b"} if cls is CodexProvider else {"FAKE_GITHUB_a", "FAKE_GITHUB_b"})
    assert {e[key] for e in captured} == expected
    assert not any("FAKE_WRONG_GLOBAL" in e.values() for e in captured)
    assert len({e["HOME"] for e in captured}) == 2
    environment_unchanged = before == dict(os.environ)
    assert environment_unchanged and current_auth_profile() is None


def test_rotation_during_request_does_not_change_inflight_or_other_service(tmp_path, monkeypatch):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    ca, cb = make_client(p, a), make_client(p, b)
    captured = []
    started, release = asyncio.Event(), asyncio.Event()
    async def run(cmd, **kwargs):
        captured.append(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        if len(captured) == 1:
            started.set()
            await release.wait()
        return output("claude"), b"", 0, False
    monkeypatch.setattr(module, "run_subprocess_async", run)
    async def exercise():
        first = asyncio.create_task(ca.chat_async("first", provider="claude", alias="chat"))
        await started.wait()
        ca.set_auth_profile(replace(a, claude_oauth_token="FAKE_ROTATED_A"))
        assert (await cb.chat_async("B", provider="claude", alias="chat")).content == "ok"
        release.set()
        await first
        assert (await ca.chat_async("second", provider="claude", alias="chat")).content == "ok"
    asyncio.run(exercise())
    assert captured == ["FAKE_CLAUDE_a", "FAKE_CLAUDE_b", "FAKE_ROTATED_A"]


@pytest.mark.parametrize("provider", ["claude", "codex", "copilot", "kiro"])
def test_missing_service_credentials_never_borrow_global(tmp_path, monkeypatch, provider):
    auth = AuthProfile("empty", str(tmp_path / "empty")).prepare()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "FAKE_GLOBAL")
    monkeypatch.setenv("GITHUB_TOKEN", "FAKE_GLOBAL")
    with pytest.raises(AuthProfileError):
        auth.environment(provider)


def test_profile_repr_and_public_metadata_have_no_tokens(tmp_path):
    auth = profile(tmp_path, "a")
    text = repr(auth) + json.dumps(auth.public_dict())
    assert "FAKE_" not in text


def test_root_cannot_be_shared_by_different_profile_ids(tmp_path):
    profile(tmp_path, "a")
    with pytest.raises(AuthProfileError):
        AuthProfile("b", str(tmp_path / "a"), github_token="FAKE").prepare()


def test_profile_scope_blocks_other_client_and_legacy_resume(tmp_path, monkeypatch):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    monkeypatch.setattr(module, "run_subprocess_sync", lambda *args, **kwargs: (output("claude"), b"", 0, False))
    store = MemoryStore()
    ca, cb = make_client(p, a, store), make_client(p, b, store)
    first = ca.chat("hi", provider="claude", owner="same", alias="chat")
    conv = store.get(first.conversation_id)
    assert "FAKE_" not in json.dumps(conv.metadata)
    with pytest.raises(PermissionError, match="profile mismatch"):
        cb.chat("wrong", provider="claude", owner="same", alias="chat")
    with pytest.raises(PermissionError):
        make_client(p, None, store).chat("wrong", provider="claude", owner="same", alias="chat")
    legacy = store.create("same", "claude", alias="legacy")
    store.set_metadata(legacy.id, "session_id:claude", "old")
    with pytest.raises(PermissionError, match="Legacy"):
        ca.chat("wrong", provider="claude", owner="same", alias="legacy")


def test_disabled_service_does_not_affect_other_service(tmp_path, monkeypatch):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        return output("claude"), b"", 0, False
    monkeypatch.setattr(module, "run_subprocess_sync", run)
    ca, cb = make_client(p, a), make_client(p, b)
    ca.set_auth_profile(replace(a, disabled_providers={"claude"}))
    with pytest.raises(AuthProfileError, match="disabled"):
        ca.chat("A", provider="claude")
    assert cb.chat("B", provider="claude").content == "ok"
    assert calls == ["FAKE_CLAUDE_b"]


def test_usage_uses_profile_not_shared_login(tmp_path, monkeypatch):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    seen = []
    def lookup(**kwargs):
        seen.append(kwargs["token"])
        return AccountUsage(provider="claude", status="ok")
    monkeypatch.setattr("agentcli.account_usage.claude_account_usage", lookup)
    ca, cb = make_client(ClaudeProvider(), a), make_client(ClaudeProvider(), b)
    async def run():
        return await asyncio.gather(ca.get_account_usage_async("claude"), cb.get_account_usage_async("claude"))
    assert all(r.status == "ok" for r in asyncio.run(run()))
    assert set(seen) == {"FAKE_CLAUDE_a", "FAKE_CLAUDE_b"}
    with pytest.raises(AuthProfileError):
        ca.get_account_usage("claude", oauth_token="FAKE_OVERRIDE")


@pytest.mark.parametrize("cls", [ClaudeProvider, CodexProvider, CopilotProvider])
def test_health_uses_profile_without_global_login_or_model_probe(tmp_path, monkeypatch, cls):
    a = profile(tmp_path, "a")
    p = cls()
    module = wire(p, monkeypatch)
    monkeypatch.setattr(module.shutil, "which", lambda name: "fake-cli")
    calls = []
    def health(cmd, **kwargs):
        calls.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "fake-version", "")
    monkeypatch.setattr(module, "run_health_command", health)
    result = make_client(p, a).health_check(p.provider_id)
    assert result.status == "configured" and result.auth_ok is None
    assert len(calls) == 1 and calls[0][1]["HOME"] == str(a.directory("home"))


def test_warm_profiles_keep_separate_environment_snapshots(tmp_path):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    async def run():
        wa = await open_warm(binary="fake-cli", auth_profile=a)
        wb = await open_warm(binary="fake-cli", auth_profile=b)
        assert wa._env["CLAUDE_CODE_OAUTH_TOKEN"] == "FAKE_CLAUDE_a"
        assert wb._env["CLAUDE_CODE_OAUTH_TOKEN"] == "FAKE_CLAUDE_b"
        with pytest.raises(AuthProfileError):
            await open_warm(binary="fake-cli", auth_profile=a, env={})
        await wa.close()
        await wb.close()
    asyncio.run(run())


def test_codex_file_auth_and_liveness_use_the_service_root(tmp_path):
    auth = AuthProfile("codex-file", str(tmp_path / "codex-file")).prepare()
    path = auth.directory("codex")
    (path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "FAKE_LOCAL_OAUTH"}}), encoding="utf-8")
    sessions = path / "sessions"
    sessions.mkdir()
    (sessions / "rollout-fake-sid.jsonl").write_text("", encoding="utf-8")
    with auth_scope(auth):
        env = auth.environment("codex")
        assert env["CODEX_HOME"] == str(path)
        assert "CODEX_API_KEY" not in env
        p = CodexProvider()
        assert p.session_alive("fake-sid") is True
        with patch.object(p, "_find_binary", return_value="fake-cli"):
            cmd = p._build_cmd("hi", "", None, "fake-sid")
        assert 'cli_auth_credentials_store="file"' in cmd


def test_claude_liveness_uses_profile_history(tmp_path):
    auth = profile(tmp_path, "a")
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(tmp_path))
    path = auth.directory("claude") / "projects" / encoded
    path.mkdir(parents=True)
    (path / "fake-sid.jsonl").write_text("", encoding="utf-8")
    with auth_scope(auth):
        assert ClaudeProvider().session_alive("fake-sid", cwd=str(tmp_path)) is True


@pytest.mark.parametrize("cls", [ClaudeProvider, CodexProvider, CopilotProvider])
@pytest.mark.parametrize("mode", ["async", "stream"])
def test_actual_child_processes_receive_independent_service_auth(tmp_path, monkeypatch, cls, mode):
    from tests._stream_helpers import write_fake_cli_launcher
    provider_id = cls.provider_id
    script = '''import json, os, sys
provider = PROVIDER
key = {'claude':'CLAUDE_CODE_OAUTH_TOKEN','codex':'CODEX_API_KEY','copilot':'COPILOT_GITHUB_TOKEN'}[provider]
selected = os.environ[key]
if provider == 'claude':
    events = [{'type':'result','subtype':'success','result':selected,'session_id':'fake-sid'}]
    if 'stream-json' in sys.argv:
        events.insert(0, {'type':'assistant','message':{'content':[{'type':'text','text':selected}]}})
elif provider == 'codex':
    events = [{'type':'thread.started','thread_id':'fake-sid'},
              {'type':'item.completed','item':{'type':'agent_message','text':selected}}]
else:
    events = [{'type':'assistant.message','data':{'content':selected}},
              {'type':'result','sessionId':'fake-sid','exitCode':0}]
for event in events:
    print(json.dumps(event), flush=True)
'''.replace('PROVIDER', repr(provider_id))
    binary = write_fake_cli_launcher(tmp_path, script, name="echo-" + provider_id)
    p = cls()
    monkeypatch.setattr(p, "_find_binary", lambda: (binary, False) if cls is CopilotProvider else binary)
    clients = [make_client(p, profile(tmp_path, name)) for name in ("a", "b")]
    async def call(c):
        if mode == "async":
            return (await c.chat_async("fake", provider=provider_id, alias="chat")).content
        chunks = [ch async for ch in c.chat_stream("fake", provider=provider_id, alias="chat")]
        return chunks[-1].content
    async def both():
        return await asyncio.gather(*(call(c) for c in clients))
    prefix = {"claude": "FAKE_CLAUDE_", "codex": "FAKE_CODEX_", "copilot": "FAKE_GITHUB_"}[provider_id]
    assert asyncio.run(both()) == [prefix + "a", prefix + "b"]


def test_auth_failure_is_local_and_does_not_suggest_shared_login(tmp_path, monkeypatch):
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    def run(cmd, **kwargs):
        if kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"].endswith("a"):
            return b"", b"401 authentication failed", 1, False
        return output("claude"), b"", 0, False
    monkeypatch.setattr(module, "run_subprocess_sync", run)
    ra = make_client(p, a).chat("A", provider="claude")
    rb = make_client(p, b).chat("B", provider="claude")
    assert ra.error_type == "auth" and "this service" in ra.suggested_action
    assert rb.content == "ok"


def test_profile_rejects_per_call_and_constructor_credential_override(tmp_path, monkeypatch):
    a = profile(tmp_path, "a")
    p = ClaudeProvider()
    c = make_client(p, a)
    with pytest.raises(AuthProfileError):
        c.chat("bad", provider="claude", provider_options={"oauth_token": "FAKE_OVERRIDE"})
    p = ClaudeProvider(oauth_token="FAKE_CTOR_OVERRIDE")
    with pytest.raises(AuthProfileError):
        make_client(p, a).chat("bad", provider="claude")


def test_rotation_cannot_change_identity_or_auth_method(tmp_path):
    a = profile(tmp_path, "a")
    c = make_client(ClaudeProvider(), a)
    with pytest.raises(AuthProfileError):
        c.set_auth_profile(profile(tmp_path, "b"))
    with pytest.raises(AuthProfileError):
        c.set_auth_profile(replace(a, claude_oauth_token=None, claude_api_key="FAKE_KEY"))


def test_claude_file_credentials_are_read_only_and_local(tmp_path, monkeypatch):
    p = AuthProfile("file", str(tmp_path / "file")).prepare()
    path = p.directory("claude") / ".credentials.json"
    payload = json.dumps({"claudeAiOauth": {"accessToken": "FAKE_LOCAL_ONLY"}})
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "FAKE_GLOBAL")
    assert p.environment("claude")["CLAUDE_CODE_OAUTH_TOKEN"] == "FAKE_LOCAL_ONLY"
    assert path.read_text(encoding="utf-8") == payload


def test_arbitrary_parent_secrets_are_not_inherited_and_extra_env_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_B_PRIVATE_SECRET", "FAKE_FOREIGN_SECRET")
    additions = {"MY_TOOL_SETTING": "a"}
    auth = replace(profile(tmp_path, "a"), extra_env=additions)
    additions["MY_TOOL_SETTING"] = "b"
    env = auth.environment("claude")
    assert "SERVICE_B_PRIVATE_SECRET" not in env
    assert env["MY_TOOL_SETTING"] == "a"
    with pytest.raises(TypeError):
        auth.extra_env["MY_TOOL_SETTING"] = "changed"
    with pytest.raises(AuthProfileError):
        replace(auth, extra_env={"ANTHROPIC_API_KEY": "FAKE_OVERRIDE"})


def test_cancelled_call_does_not_leave_an_unbound_service_handle(tmp_path, monkeypatch):
    a, b = profile(tmp_path, "a"), profile(tmp_path, "b")
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    store = MemoryStore()
    ca, cb = make_client(p, a, store), make_client(p, b, store)
    entered = asyncio.Event()
    async def run(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, "run_subprocess_async", run)
    async def exercise():
        task = asyncio.create_task(ca.chat_async("A", provider="claude", owner="same", alias="chat"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(PermissionError):
            await cb.chat_async("B", provider="claude", owner="same", alias="chat")
        assert current_auth_profile() is None
    asyncio.run(exercise())


def test_codex_usage_reads_only_its_service_auth_file(tmp_path, monkeypatch):
    auth = AuthProfile("file", str(tmp_path / "file")).prepare()
    path = auth.directory("codex") / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "FAKE_LOCAL", "account_id": "local-id"}}), encoding="utf-8")
    seen = []
    def lookup(**kwargs):
        seen.append(kwargs["credentials_path"])
        return AccountUsage(provider="codex")
    monkeypatch.setattr("agentcli.account_usage.codex_account_usage", lookup)
    make_client(CodexProvider(), auth).get_account_usage("codex")
    assert seen == [str(path)]


def test_profile_health_missing_credentials_is_not_shared_auth(tmp_path, monkeypatch):
    auth = AuthProfile("empty", str(tmp_path / "empty")).prepare()
    p = ClaudeProvider()
    monkeypatch.setattr(p, "is_available", lambda: True)
    result = make_client(p, auth).health_check("claude")
    assert not result.ok and result.auth_ok is False
    assert "service" in result.suggested_action


def test_marker_and_credential_errors_do_not_echo_secret_payloads(tmp_path):
    auth = AuthProfile("file", str(tmp_path / "file")).prepare()
    path = auth.directory("codex") / "auth.json"
    path.write_text("FAKE_SECRET_INVALID_JSON", encoding="utf-8")
    with pytest.raises(AuthProfileError) as error:
        auth.environment("codex")
    assert "FAKE_SECRET" not in str(error.value)


def test_api_key_profiles_do_not_query_subscription_usage(tmp_path, monkeypatch):
    auth = AuthProfile("api", str(tmp_path / "api"), claude_api_key="FAKE_KEY", codex_api_key="FAKE_CODEX").prepare()
    def forbidden(**kwargs):
        raise AssertionError("No usage HTTP request for API-key mode")
    monkeypatch.setattr("agentcli.account_usage.claude_account_usage", forbidden)
    monkeypatch.setattr("agentcli.account_usage.codex_account_usage", forbidden)
    assert make_client(ClaudeProvider(), auth).get_account_usage("claude").status == "unsupported_auth"
    assert make_client(CodexProvider(), auth).get_account_usage("codex").status == "unsupported_auth"


def test_auth_profile_directories_reject_cross_profile_junction_or_symlink(tmp_path):
    auth, other = profile(tmp_path, "a"), profile(tmp_path, "b")
    link = auth.directory("codex")
    link.rmdir()  # Known empty synthetic directory under this test's temp root.
    target = other.directory("codex")
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises((PermissionError, AuthProfileError)):
            auth.environment("codex")
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove the test junction only, not its target.
        else:
            link.unlink()


def test_clear_preflights_all_profiles_before_mutation(tmp_path, monkeypatch):
    p = ClaudeProvider()
    module = wire(p, monkeypatch)
    monkeypatch.setattr(module, "run_subprocess_sync", lambda *a, **kw: (output("claude"), b"", 0, False))
    store = MemoryStore()
    ca = make_client(p, profile(tmp_path, "a"), store)
    cb = make_client(p, profile(tmp_path, "b"), store)
    a = ca.chat("A", provider="claude", owner="same", alias="a")
    b = cb.chat("B", provider="claude", owner="same", alias="b")
    with pytest.raises(PermissionError):
        ca.clear_session_metadata(owner="same")
    assert store.get(a.conversation_id).metadata["session_id:claude"] == a.session_id
    assert store.get(b.conversation_id).metadata["session_id:claude"] == b.session_id
    with pytest.raises(PermissionError):
        ca.get_alias_status("same", "b")


def test_stream_rotation_keeps_snapshot_and_does_not_leak_context(tmp_path, monkeypatch):
    a = profile(tmp_path, "a")
    p = ClaudeProvider()
    captured = []
    async def stream(messages, **kwargs):
        captured.append(current_auth_profile().claude_oauth_token)
        yield StreamChunk(type="text", content="one")
        captured.append(current_auth_profile().claude_oauth_token)
        yield StreamChunk(type="text", content="two")
        yield StreamChunk(type="done", session_id="fake")
    monkeypatch.setattr(p, "stream_async", stream)
    c = make_client(p, a)
    async def run():
        iterator = c.chat_stream("hi", provider="claude", alias="chat")
        assert (await anext(iterator)).content == "one"
        assert current_auth_profile() is None
        c.set_auth_profile(replace(a, claude_oauth_token="FAKE_NEW"))
        remaining = [ch async for ch in iterator]
        assert remaining[-1].content == "onetwo"
        assert current_auth_profile() is None
    asyncio.run(run())
    assert captured == ["FAKE_CLAUDE_a", "FAKE_CLAUDE_a"]


def test_filesystem_root_is_not_an_auth_profile_root(tmp_path):
    with pytest.raises(AuthProfileError):
        AuthProfile("bad", str(Path(tmp_path.anchor)))


def test_fallback_cannot_use_unconfigured_global_account(tmp_path, monkeypatch):
    auth = AuthProfile("a", str(tmp_path / "a"), claude_oauth_token="FAKE_A").prepare()
    primary, fallback = ClaudeProvider(), CodexProvider()
    module = wire(primary, monkeypatch)
    monkeypatch.setattr(module, "run_subprocess_sync", lambda *a, **kw: (b"", b"401 authentication failed", 1, False))
    def forbidden(*args, **kwargs):
        raise AssertionError("No global-account fallback invocation")
    monkeypatch.setattr(fallback, "invoke", forbidden)
    registry = ProviderRegistry()
    registry.register(primary)
    registry.register(fallback)
    registry.set_fallback_order(["claude", "codex"])
    result = LLMClient(MemoryStore(), registry, auth_profile=auth).chat(
        "fail", provider="claude", fallback=True)
    assert result.error_type == "auth" and not result.content
    assert "this service" in result.suggested_action
