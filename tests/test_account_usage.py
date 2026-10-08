"""Quota tests use fake credentials and HTTP responses; no real account calls."""

import base64
import io
import json
import threading
import time
import urllib.error
from email.message import Message as Headers
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentcli import AccountUsage, LLMClient, MemoryStore, UsageWindow
from agentcli import account_usage as usage
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.status, self.body = status, body
        self.headers = Headers()
        for name, value in (headers or {}).items():
            self.headers[name] = str(value)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        return self.body[:size]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("AGENTCLI_CLAUDE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CODEX_HOME", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.fixture
def http(monkeypatch):
    class FakeOpener:
        replies = []
        seen = []

        def open(self, request, timeout):
            self.seen.append((request, timeout))
            assert self.replies, "Unexpected HTTP request"
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    opener = FakeOpener()
    monkeypatch.setattr(usage.urllib.request, "build_opener", lambda *args: opener)
    return opener


def credentials(tmp_path, provider, **changes):
    if provider == "claude":
        path = tmp_path / ".claude" / ".credentials.json"
        data = {"claudeAiOauth": {"accessToken": "TEST_CLAUDE_ACCESS",
                "refreshToken": "TEST_REFRESH", "scopes": ["user:profile", "user:inference"],
                "expiresAt": (time.time() + 3600) * 1000, **changes}}
    else:
        path = tmp_path / ".codex" / "auth.json"
        data = {"auth_mode": "chatgpt", "tokens": {"access_token": "TEST_CODEX_ACCESS",
                "refresh_token": "TEST_REFRESH", "account_id": "TEST_ACCOUNT", **changes}}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Test fixtures only, not provider-managed credentials.
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def body_response(data):
    return Response(json.dumps(data).encode())


def claude_response(used=42):
    return body_response({"five_hour": {"utilization": used, "resets_at": "2026-10-08T12:00:00Z"},
                          "seven_day": {"utilization": 17, "resets_at": None}})


def probe_response(status=200):
    return Response(status=status, headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-5h-reset": "1791460800",
        "anthropic-ratelimit-unified-7d-utilization": "0.17"})


def test_claude_browser_read_only(isolated, http):
    path = credentials(isolated, "claude")
    before = path.read_bytes()
    response = claude_response()
    http.replies.append(response)
    result = LLMClient(MemoryStore()).get_account_usage("claude")
    assert isinstance(result, AccountUsage)
    assert result.ok and result.status == "ok" and result.source == "oauth"
    assert not result.probe_performed
    assert result.windows["five_hour"].used_percentage == 42
    assert result.windows["five_hour"].remaining_percentage == 58
    assert result.windows["five_hour"].resets_at == 1791460800
    request, timeout = http.seen[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.full_url == usage.CLAUDE_USAGE_URL and timeout == 15
    assert request.get_header("Authorization") == "Bearer TEST_CLAUDE_ACCESS"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert path.read_bytes() == before and response.closed
    assert "TEST_CLAUDE_ACCESS" not in repr(result.public_dict()) + repr(result)


def test_usage_full_does_not_mean_failed_lookup(isolated, http):
    credentials(isolated, "claude")
    http.replies.append(claude_response(100))
    result = ClaudeProvider().get_account_usage()
    assert result.ok and result.windows["five_hour"].remaining_percentage == 0


@pytest.mark.parametrize("status", [200, 429])
def test_setup_probe_opt_in_and_headers(isolated, http, status):
    credentials(isolated, "claude", scopes=["user:inference"])
    provider = ClaudeProvider()
    result = provider.get_account_usage()
    assert result.status == "probe_required" and not result.probe_performed
    assert not http.seen
    http.replies.append(probe_response(status))
    result = provider.get_account_usage(allow_probe=True)
    assert result.ok and result.probe_performed and result.http_status == status
    assert result.source == "probe" and result.windows["five_hour"].used_percentage == 42
    assert result.windows["seven_day"].used_percentage == 17
    request, _ = http.seen[0]
    body = json.loads(request.data)
    assert request.get_method() == "POST" and request.full_url == usage.CLAUDE_PROBE_URL
    assert body["max_tokens"] == 1 and body["model"] == usage.CLAUDE_PROBE_MODEL


def test_unknown_scope_403_does_not_implicitly_probe(isolated, http):
    http.replies.append(Response(status=403))
    result = ClaudeProvider(oauth_token="TEST_SETUP").get_account_usage()
    assert result.status == "probe_required" and result.http_status == 403
    assert not result.probe_performed and len(http.seen) == 1


def test_unknown_scope_explicit_probe_fallback(isolated, http):
    http.replies.extend([Response(status=403), probe_response()])
    result = ClaudeProvider(oauth_token="TEST_SETUP").get_account_usage(allow_probe=True, timeout=10)
    assert result.ok and result.probe_performed
    assert len(http.seen) == 2 and 0 < http.seen[1][1] <= 10
    assert all(req.get_header("Authorization") == "Bearer TEST_SETUP" for req, _ in http.seen)


@pytest.mark.parametrize("code", [401, 403])
def test_browser_auth_failure_never_probes(isolated, http, code):
    credentials(isolated, "claude")
    http.replies.append(Response(status=code))
    result = ClaudeProvider().get_account_usage(allow_probe=True)
    assert result.status == "auth_required" and not result.probe_performed
    assert len(http.seen) == 1


@pytest.mark.parametrize("source", ["constructor", "call", "env", "native_env", "token_file"])
def test_claude_existing_token_precedence(isolated, http, monkeypatch, source):
    credentials(isolated, "claude")
    provider, kwargs = ClaudeProvider(), {}
    if source == "constructor":
        provider = ClaudeProvider(oauth_token="TEST_OVERRIDE")
    elif source == "call":
        provider = ClaudeProvider(oauth_token="TEST_DEFAULT")
        kwargs["oauth_token"] = "TEST_OVERRIDE"
    elif source in ("env", "native_env"):
        key = "AGENTCLI_CLAUDE_OAUTH_TOKEN" if source == "env" else "CLAUDE_CODE_OAUTH_TOKEN"
        monkeypatch.setenv(key, "TEST_OVERRIDE")
    else:
        path = isolated / ".agentcli" / "claude_oauth_token"
        path.parent.mkdir()
        path.write_text("TEST_OVERRIDE", encoding="utf-8")
    http.replies.append(claude_response())
    assert provider.get_account_usage(**kwargs).ok
    assert http.seen[0][0].get_header("Authorization") == "Bearer TEST_OVERRIDE"


def test_explicit_credentials_do_not_switch_to_configured_token(isolated, http, monkeypatch):
    path = credentials(isolated, "claude")
    monkeypatch.setenv("AGENTCLI_CLAUDE_OAUTH_TOKEN", "DIFFERENT_ACCOUNT")
    http.replies.append(claude_response())
    result = ClaudeProvider(oauth_token="OTHER_ACCOUNT").get_account_usage(credentials_path=str(path))
    assert result.ok and http.seen[0][0].get_header("Authorization") == "Bearer TEST_CLAUDE_ACCESS"
    with pytest.raises(ValueError):
        ClaudeProvider().get_account_usage(oauth_token="TOKEN", credentials_path=str(path))


def test_expired_claude_no_request(isolated, http):
    path = credentials(isolated, "claude", expiresAt=1)
    before = path.read_bytes()
    result = ClaudeProvider().get_account_usage(allow_probe=True)
    assert result.status == "token_expired" and not http.seen
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_missing_or_bad_credentials(isolated, http, provider):
    instance = ClaudeProvider() if provider == "claude" else CodexProvider()
    assert instance.get_account_usage().status == "auth_required"
    path = credentials(isolated, provider)
    for raw in (b"bad-json", b"[]", b"\xff", b"{}", b"x" * (usage._MAX_BYTES + 1)):
        path.write_bytes(raw)
        assert instance.get_account_usage().status == "auth_required"
    assert not http.seen


def test_codex_read_only_with_account_header(isolated, http):
    path = credentials(isolated, "codex")
    before = path.read_bytes()
    http.replies.append(body_response({"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 25, "reset_at": 1791460800, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 10, "reset_after_seconds": 100, "limit_window_seconds": 604800}}}))
    result = CodexProvider().get_account_usage(allow_probe=True)
    assert result.ok and result.plan == "plus" and not result.probe_performed
    assert result.windows["five_hour"].used_percentage == 25
    assert result.windows["seven_day"].limit_window_seconds == 604800
    assert result.windows["seven_day"].resets_at > time.time()
    request, _ = http.seen[0]
    assert request.full_url == usage.CODEX_USAGE_URL and request.data is None
    assert request.get_header("Chatgpt-account-id") == "TEST_ACCOUNT"
    assert path.read_bytes() == before


@pytest.mark.parametrize("lengths", [(604800, 18000), (18000, 3600), (None, None)])
def test_codex_window_mapping(isolated, http, lengths):
    credentials(isolated, "codex")
    http.replies.append(body_response({"rate_limit": {
        "primary_window": {"used_percent": 20, "limit_window_seconds": lengths[0]},
        "secondary_window": {"used_percent": 40, "limit_window_seconds": lengths[1]}}}))
    windows = CodexProvider().get_account_usage().windows
    assert set(windows) == {"five_hour", "seven_day"}
    assert windows["five_hour"].used_percentage == (20 if lengths == (None, None) else 40)


def test_codex_expired_token_no_request(isolated, http):
    payload = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
    credentials(isolated, "codex", access_token=f"header.{payload}.signature")
    assert CodexProvider().get_account_usage().status == "token_expired"
    assert not http.seen


@pytest.mark.parametrize("provider, env", [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")])
def test_custom_cli_home(isolated, http, monkeypatch, provider, env):
    path = credentials(isolated / "custom", provider)
    monkeypatch.setenv(env, str(path.parent))
    http.replies.append(claude_response() if provider == "claude" else body_response({
        "rate_limit": {"primary_window": {"used_percent": 5}}}))
    assert LLMClient(MemoryStore()).get_account_usage(provider).ok


def test_api_key_auth_is_not_subscription_usage(isolated, http, monkeypatch):
    credentials(isolated, "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "TEST_API_KEY")
    assert ClaudeProvider().get_account_usage().status == "unsupported_auth"
    path = credentials(isolated, "codex")
    path.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"TEST_API_KEY"}', encoding="utf-8")
    assert CodexProvider().get_account_usage().status == "unsupported_auth"
    assert not http.seen


@pytest.mark.parametrize("code,status", [(401, "auth_required"), (403, "auth_required"),
                                         (429, "rate_limited"), (500, "http_error"), (302, "http_error")])
def test_http_errors_do_not_leak_raw_body(isolated, http, code, status):
    credentials(isolated, "codex")
    headers = Headers()
    http.replies.append(urllib.error.HTTPError(usage.CODEX_USAGE_URL, code,
        "TEST_CODEX_ACCESS", headers, io.BytesIO(b"secret TEST_CODEX_ACCESS")))
    result = CodexProvider().get_account_usage()
    assert result.status == status and result.http_status == code and not result.ok
    assert "TEST_CODEX_ACCESS" not in repr(result) + repr(result.public_dict())


@pytest.mark.parametrize("exc,status", [(TimeoutError("SECRET"), "timeout"),
    (urllib.error.URLError(TimeoutError("SECRET")), "timeout"),
    (urllib.error.URLError("SECRET"), "network_error"), (OSError("SECRET"), "network_error")])
def test_probe_transport_failure_remembers_attempt(isolated, http, exc, status):
    credentials(isolated, "claude", scopes=["user:inference"])
    http.replies.append(exc)
    result = ClaudeProvider().get_account_usage(allow_probe=True)
    assert result.status == status and result.probe_performed
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"bad-json", b"\xff", b"x" * (usage._MAX_BYTES + 1)],
                         ids=["empty", "array", "invalid-json", "invalid-utf8", "oversized"])
def test_invalid_response(isolated, http, raw):
    credentials(isolated, "claude")
    http.replies.append(Response(raw))
    assert ClaudeProvider().get_account_usage().status == "invalid_response"


@pytest.mark.parametrize("invalid", [True, None, "42", float("nan"), float("inf")])
def test_invalid_utilization_is_not_zero(isolated, http, invalid):
    credentials(isolated, "claude")
    http.replies.append(body_response({"five_hour": {"utilization": invalid}}))
    result = ClaudeProvider().get_account_usage()
    assert not result.ok and not result.windows


@pytest.mark.parametrize("timeout", [0, -1, True, "10", float("inf"), float("nan")])
def test_invalid_timeout(isolated, http, timeout):
    with pytest.raises(ValueError):
        LLMClient(MemoryStore()).get_account_usage("claude", timeout=timeout)
    assert not http.seen


def test_redirects_are_blocked():
    handler = usage._NoRedirect()
    assert handler.redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_public_types_and_capabilities(isolated, http):
    client = LLMClient(MemoryStore())
    assert UsageWindow(42).to_dict()["remaining_percentage"] == 58
    for provider in ("claude", "codex"):
        assert client.supports(provider, "account_usage")
    for provider in ("copilot", "kiro"):
        assert not client.supports(provider, "account_usage")
        assert client.get_account_usage(provider).status == "unsupported"
    assert client.get_account_usage("missing").status == "unknown_provider"
    assert not http.seen


@pytest.mark.asyncio
async def test_async_client_does_not_write_token_stats(isolated, http):
    credentials(isolated, "claude")
    http.replies.append(claude_response())
    client = LLMClient(MemoryStore())
    before = client.get_token_stats()
    result = await client.get_account_usage_async("claude")
    assert result.ok and client.get_token_stats() == before


def test_codex_rejects_ambiguous_bare_token(isolated, http):
    with pytest.raises(ValueError):
        CodexProvider().get_account_usage(oauth_token="TEST")
    assert not http.seen


def test_extreme_numeric_fields_are_contained():
    assert not usage._finite(10 ** 1000)
    assert usage._epoch(10 ** 1000) is None
    assert usage._window(float("nan")) is None


def test_real_local_transport_and_redirect_blocking(isolated, monkeypatch):
    """Exercise urllib (including HTTPError headers), not just the fake opener."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/sink")
                self.end_headers()
                return
            payload = json.dumps({"five_hour": {"utilization": 12}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429)
            self.send_header("anthropic-ratelimit-unified-5h-utilization", "1.0")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        monkeypatch.setattr(usage, "CLAUDE_USAGE_URL", base + "/usage")
        monkeypatch.setattr(usage, "CLAUDE_PROBE_URL", base + "/probe")
        credentials(isolated, "claude")
        assert ClaudeProvider().get_account_usage().windows["five_hour"].used_percentage == 12
        credentials(isolated, "claude", scopes=["user:inference"])
        quota = ClaudeProvider().get_account_usage(allow_probe=True)
        assert quota.ok and quota.http_status == 429 and quota.probe_performed
        assert quota.windows["five_hour"].remaining_percentage == 0
        credentials(isolated, "claude")
        monkeypatch.setattr(usage, "CLAUDE_USAGE_URL", base + "/redirect")
        assert ClaudeProvider().get_account_usage().http_status == 302
        assert seen == ["/usage", "/probe", "/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
