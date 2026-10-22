"""Regressions for structured CLI failures (#81) and prompt boundaries (#82)."""

import asyncio
import json
import subprocess

import pytest

from agentcli import LLMClient, MemoryStore, Message, ProviderRegistry
from agentcli.providers.base import redact_argv
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider


def _invoke(monkeypatch, provider_name, mode, stdout, *, stderr="", rc=1, public=False):
    cls = ClaudeProvider if provider_name == "claude" else CodexProvider
    provider = cls()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-cli")
    if provider_name == "claude":
        monkeypatch.setattr(provider, "_spawn_env", lambda *args: None)
    else:
        monkeypatch.setattr("agentcli.providers.codex.build_env", lambda: {})
    calls = []

    def sync(cmd, **kwargs):
        calls.append(cmd)
        if provider_name == "claude":
            return stdout.encode(), stderr.encode(), rc, False
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    async def asynchronous(cmd, **kwargs):
        calls.append(cmd)
        return stdout.encode(), stderr.encode(), rc, False

    target = "run_subprocess_sync" if provider_name == "claude" else "run_owned_command"
    monkeypatch.setattr(f"agentcli.providers.{provider_name}.{target}", sync)
    monkeypatch.setattr(f"agentcli.providers.{provider_name}.run_subprocess_async", asynchronous)
    if public:
        registry = ProviderRegistry()
        registry.register(provider)
        client = LLMClient(MemoryStore(), registry=registry)
        if mode == "sync":
            response = client.chat("hello", provider=provider_name)
        else:
            response = asyncio.run(client.chat_async("hello", provider=provider_name))
    else:
        messages = [Message("user", "hello")]
        response = (provider.invoke(messages) if mode == "sync" else
                    asyncio.run(provider.invoke_async(messages)))
    assert len(calls) == 1
    return response


def _error_output(provider):
    if provider == "claude":
        return json.dumps({"is_error": True, "result": "Rate limit reached",
                           "api_error_status": 429,
                           "usage": {"input_tokens": 11, "output_tokens": 2}})
    return "\n".join(map(json.dumps, [
        {"type": "thread.started", "thread_id": "failed-thread"},
        {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 2}},
        {"type": "turn.failed", "error": {"message": "Rate limit reached"}},
    ]))


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("public", [False, True])
def test_failed_exit_preserves_stdout_error_and_usage(monkeypatch, provider, mode, public):
    response = _invoke(monkeypatch, provider, mode, _error_output(provider),
                       stderr="diagnostic tag", public=public)
    assert response.content == ""
    assert response.error == "Rate limit reached"
    assert response.error_type == "usage_limit" and response.recoverable
    assert response.exit_code == 1
    assert response.raw_stderr == "diagnostic tag"
    assert response.tokens.total_tokens == 13
    assert response.tokens.payload_prompt_tokens > 0
    assert not response.tokens.prompt_tokens_reliable
    if provider == "codex":
        assert response.session_id == "failed-thread"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("rc", [0, 1])
@pytest.mark.parametrize("payload", [
    {"is_error": True, "result": "Request rejected", "api_error_status": 429},
    {"is_error": True, "api_error_status": 429},
    {"api_error_status": 429},
])
def test_claude_numeric_http_429_is_retryable(monkeypatch, mode, rc, payload):
    response = _invoke(monkeypatch, "claude", mode, json.dumps(payload), rc=rc)
    assert response.content == ""
    assert response.error and response.error_type == "usage_limit"
    assert response.recoverable and response.exit_code == rc


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("stdout", ["", "not JSON", '{"result":"must not become success"}'])
def test_unstructured_failed_exit_uses_stderr(monkeypatch, provider, mode, stdout):
    response = _invoke(monkeypatch, provider, mode, stdout, stderr="not authenticated")
    assert response.content == ""
    assert response.error == "not authenticated"
    assert response.error_type == "auth" and not response.recoverable
    assert response.exit_code == 1


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_failed_exit_without_diagnostics_remains_failure(monkeypatch, provider, mode):
    response = _invoke(monkeypatch, provider, mode, "")
    assert response.content == ""
    assert response.error == "exit=1" and response.error_type == "unknown"


def _failure_with_usage(provider, usage):
    if provider == "claude":
        return json.dumps({"is_error": True, "result": "Rate limit reached", "usage": usage})
    return "\n".join(map(json.dumps, [
        {"type": "turn.completed", "usage": usage},
        {"type": "turn.failed", "error": {"message": "Rate limit reached"}},
    ]))


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("usage", [None, "unavailable", [1], 9, True])
def test_failed_exit_keeps_error_with_malformed_usage(monkeypatch, provider, mode, usage):
    response = _invoke(monkeypatch, provider, mode, _failure_with_usage(provider, usage),
                       stderr="native diagnostic", public=True)
    assert response.error == "Rate limit reached"
    assert response.error_type == "usage_limit" and response.exit_code == 1
    assert response.raw_stderr == "native diagnostic"
    assert response.tokens.total_tokens == 0


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("invalid", [None, "unavailable", [], {}, True, -5, 1.5,
                                     float("nan"), float("inf"), "-1", "1.5"])
def test_invalid_token_count_does_not_erase_valid_fields(monkeypatch, provider, mode, invalid):
    usage = {"input_tokens": invalid, "output_tokens": 2,
             "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3,
             "cached_input_tokens": 5}
    response = _invoke(monkeypatch, provider, mode, _failure_with_usage(provider, usage))
    assert response.error == "Rate limit reached" and response.error_type == "usage_limit"
    assert response.tokens.completion_tokens == 2 and response.tokens.cached_tokens == 5
    assert response.tokens.prompt_tokens == (8 if provider == "claude" else 0)
    assert response.tokens.total_tokens == (10 if provider == "claude" else 2)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_numeric_string_token_counts_remain_supported(monkeypatch, provider, mode):
    output = _failure_with_usage(provider, {"input_tokens": "11", "output_tokens": "2"})
    response = _invoke(monkeypatch, provider, mode, output)
    assert response.tokens.total_tokens == 13


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_codex_failed_turn_accepts_string_error(monkeypatch, mode):
    output = json.dumps({"type": "turn.failed", "error": "Rate limit reached"})
    response = _invoke(monkeypatch, "codex", mode, output)
    assert response.error == "Rate limit reached" and response.error_type == "usage_limit"
    assert response.exit_code == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_codex_invalid_auxiliary_fields_do_not_hide_failure(monkeypatch, mode):
    output = "\n".join(map(json.dumps, [
        {"type": "thread.started", "thread_id": ["invalid"]},
        {"type": "item.completed", "item": "invalid"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": ["invalid"]}},
        {"type": "error", "message": ["invalid"]},
        {"type": "turn.failed", "error": {"message": ["invalid"]}},
    ]))
    response = _invoke(monkeypatch, "codex", mode, output, stderr="not authenticated")
    assert response.error == "not authenticated" and response.error_type == "auth"
    assert response.exit_code == 1


@pytest.mark.parametrize("prompt", ["--version", "--help", "- hello bullet", "--settings={}", "--", "-"])
@pytest.mark.parametrize("session_id", ["", "existing-session"])
@pytest.mark.parametrize("output_format", ["json", "stream-json"])
def test_claude_prompt_is_after_all_options(monkeypatch, prompt, session_id, output_format):
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-claude")
    cmd, sid = provider._build_cmd(prompt, "sonnet", session_id, output_format,
                                  append_system_prompt="SYSTEM SECRET", debug=True)
    assert cmd[-2:] == ["--", prompt]
    sep = len(cmd) - 2
    assert "--model" in cmd[:sep]
    assert "--append-system-prompt" in cmd[:sep]
    assert ("--resume" if session_id else "--session-id") in cmd[:sep]
    assert sid
    if session_id:
        assert sid == session_id
    safe = redact_argv(cmd)
    assert safe[-1] == ("-" if prompt == "-" else f"<prompt:{len(prompt)} chars>")
    assert "SYSTEM SECRET" not in safe


def test_claude_stdin_path_does_not_expose_prompt(monkeypatch):
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-claude")
    cmd, _ = provider._build_cmd("--version", "", "", prompt_via_stdin=True)
    assert "--version" not in cmd
    assert "--" not in cmd
