"""Atomic profile initialization and request-local credential validation."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from agentcli import AuthProfile, AuthProfileError, LLMClient, MemoryStore
from agentcli.auth_context import SERVICE_AUTH_ACTION
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import make_error_chunk
from tests.test_service_auth import profile, make_client, wire, output


def test_concurrent_prepare_never_reads_partial_marker(tmp_path, monkeypatch):
    auth = AuthProfile("same", str(tmp_path / "same"), claude_oauth_token="FAKE")
    entered, release = threading.Event(), threading.Event()
    original = json.dump
    lock = threading.Lock()
    first = [True]
    def blocked_dump(*args, **kwargs):
        with lock:
            block = first[0]
            first[0] = False
        if block:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(json, "dump", blocked_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_worker = pool.submit(auth.prepare)
        assert entered.wait(2)
        try:
            assert pool.submit(auth.prepare).result(timeout=2) is auth
        finally:
            release.set()
        assert first_worker.result(timeout=2) is auth
    assert all(p.name == ".agentcli-auth-profile.json"
               for p in (tmp_path / "same").glob(".agentcli-auth-*"))
    assert json.loads((tmp_path / "same" / ".agentcli-auth-profile.json").read_text()) == auth.binding


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_one_validation_per_request_and_revalidation_next_request(tmp_path, monkeypatch, mode):
    auth = profile(tmp_path, "a")
    provider = ClaudeProvider()
    module = wire(provider, monkeypatch)
    calls = []
    original = AuthProfile.validate
    def counted(self, name):
        calls.append(name)
        return original(self, name)
    monkeypatch.setattr(AuthProfile, "validate", counted)
    monkeypatch.setattr(module, "run_subprocess_sync", lambda *a, **k: (output("claude"), b"", 0, False))
    async def fake(*a, **k): return output("claude"), b"", 0, False
    monkeypatch.setattr(module, "run_subprocess_async", fake)
    async def template(*a, **k):
        from agentcli.types import StreamChunk
        yield StreamChunk(type="text", content="ok")
        yield StreamChunk(type="done", content="ok", session_id="fake")
    monkeypatch.setattr(provider, "_run_stream_template", template)
    client = make_client(provider, auth)
    async def run():
        for _ in range(2):
            if mode == "sync": client.chat("hi", provider="claude", alias="a")
            elif mode == "async": await client.chat_async("hi", provider="claude", alias="a")
            else: assert [c async for c in client.chat_stream("hi", provider="claude", alias="a")]
    asyncio.run(run())
    assert calls == ["claude", "claude"]
    (tmp_path / "a" / ".agentcli-auth-profile.json").write_text("{}")
    with pytest.raises(AuthProfileError): client.chat("next", provider="claude")


@pytest.mark.parametrize("expired", [False, True])
def test_profile_quota_keeps_expiry_and_browser_scope(tmp_path, monkeypatch, expired):
    auth = AuthProfile("file", str(tmp_path / "file")).prepare()
    path = auth.directory("claude") / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "FAKE",
                    "expiresAt": 1 if expired else 9999999999999, "scopes": ["user:profile"]}}))
    requests = []
    def request(provider, url, headers, **kwargs):
        requests.append(url)
        return 403, {}, b"", None
    monkeypatch.setattr("agentcli.account_usage._request", request)
    result = make_client(ClaudeProvider(), auth).get_account_usage("claude", allow_probe=True)
    assert not result.probe_performed
    if expired:
        assert result.status == "token_expired" and requests == []
    else:
        assert len(requests) == 1 and requests[0].endswith("/api/oauth/usage")


def test_disabled_stream_fallback_never_recommends_shared_login(tmp_path, monkeypatch):
    from dataclasses import replace
    auth = replace(profile(tmp_path, "a"), disabled_providers={"codex"})
    primary = ClaudeProvider()
    async def rejected(*args, **kwargs):
        yield make_error_chunk("fake rejection", provider="claude", error_type="auth")
    monkeypatch.setattr(primary, "stream_async", rejected)
    registry = ProviderRegistry()
    registry.register(primary)
    registry.register(CodexProvider())
    registry.set_fallback_order(["claude", "codex"])
    client = LLMClient(MemoryStore(), registry, auth_profile=auth)
    async def run(): return [c async for c in client.chat_stream("hi", provider="claude", fallback=True)]
    chunks = asyncio.run(run())
    error = next(c for c in chunks if c.type == "error")
    assert error.data["error_type"] == "auth"
    assert error.data["suggested_action"] == SERVICE_AUTH_ACTION


def test_missing_quota_credentials_return_structured_status(tmp_path):
    auth = AuthProfile("empty", str(tmp_path / "empty")).prepare()
    result = make_client(ClaudeProvider(), auth).get_account_usage("claude")
    assert result.status == "auth_required" and not result.ok
    assert result.suggested_action == SERVICE_AUTH_ACTION
