"""SEC-005: automatic diagnostics must not persist credential-bearing payloads."""

import asyncio
import copy
import importlib
import json
import logging
import subprocess
from unittest.mock import AsyncMock

import pytest

from agentcli import LLMClient
from agentcli.providers.base import emit_invoke_debug, redact_argv, write_debug_trace
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.types import LLMResponse, Message
from tests._stream_helpers import FakeReadline, jsonl_bytes, make_fake_proc, patch_subprocess_exec


SECRET = "FAKE_UNSTRUCTURED_CREDENTIAL_FOR_TEST_ONLY"


@pytest.mark.parametrize("provider_cls", [ClaudeProvider, CodexProvider])
def test_native_mcp_argv_is_unchanged_but_diagnostics_are_redacted(
        provider_cls, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    provider = provider_cls()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-cli")
    config = {"fake": {"command": "fake-server", "env": {"CUSTOM": SECRET}}}
    if provider_cls is ClaudeProvider:
        cmd, _ = provider._build_cmd("fake", "", "", mcp_config=config)
    else:
        cmd = provider._build_cmd("fake", "", None, "", mcp_config=config)
    original = list(cmd)
    assert SECRET in json.dumps(cmd)
    path = tmp_path / "trace.jsonl"
    emit_invoke_debug(provider.provider_id, cmd, 0, 1, "", path=str(path))
    assert cmd == original
    assert SECRET not in caplog.text
    assert SECRET not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("flag", ["--mcp-config", "-c", "--config"])
def test_redact_configuration_payload_without_parsing(flag):
    payload = '{"headers":{"Authorization":"' + SECRET + '"},"env":{"CUSTOM":"' + SECRET + '"}}'
    cmd = ["fake-cli", flag, payload, "--model", "test-model"]
    original = list(cmd)
    safe = redact_argv(cmd)
    assert SECRET not in json.dumps(safe)
    assert safe[1] == flag
    assert safe[-2:] == ["--model", "test-model"]
    assert cmd == original
    assert redact_argv(safe) == safe


@pytest.mark.parametrize("flag", ["--mcp-config", "--config"])
def test_redact_inline_configuration_and_malformed_payload(flag):
    safe = redact_argv(["fake-cli", flag + "=invalid-json:" + SECRET])
    assert SECRET not in json.dumps(safe)
    assert safe[1].startswith(flag + "=")


def test_trace_writer_is_a_sanitization_boundary(tmp_path):
    record = {"argv": ["fake-cli", "--mcp-config", SECRET], "stderr": SECRET,
              "phase": "invoke", "latency_ms": 7, "returncode": 1}
    original = copy.deepcopy(record)
    path = tmp_path / "trace.jsonl"
    write_debug_trace(str(path), record)
    text = path.read_text(encoding="utf-8")
    saved = json.loads(text)
    assert SECRET not in text
    assert saved["stderr"] == f"<stderr:{len(SECRET)} chars>"
    assert saved["schema"] == 2
    assert saved["latency_ms"] == 7 and saved["returncode"] == 1
    assert record == original


def test_invoke_logging_omits_opaque_stderr_and_configuration(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "trace.jsonl"
    emit_invoke_debug("claude", ["fake-cli", "--mcp-config", SECRET],
                      1, 7, SECRET, path=str(path))
    assert SECRET not in caplog.text
    assert SECRET not in path.read_text(encoding="utf-8")
    assert "latency=7ms" in caplog.text


@pytest.mark.parametrize("provider_cls", [ClaudeProvider, CodexProvider, CopilotProvider])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_failure_logs_are_safe_but_response_is_unchanged(
        provider_cls, asynchronous, monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    provider = provider_cls()
    module = importlib.import_module(provider_cls.__module__)
    binary = ("fake-cli", False) if provider_cls is CopilotProvider else "fake-cli"
    monkeypatch.setattr(provider, "_find_binary", lambda: binary)
    if provider_cls is ClaudeProvider:
        monkeypatch.setattr(provider, "_spawn_env", lambda token: {})
    else:
        monkeypatch.setattr(module, "build_env", lambda: {})
    stderr = "401 authentication failed: " + SECRET
    args = {"debug": True, "debug_log_path": str(tmp_path / "trace.jsonl")}
    if asynchronous:
        runner = AsyncMock(return_value=(b"", stderr.encode(), 1, False))
        monkeypatch.setattr(module, "run_subprocess_async", runner)
        response = asyncio.run(provider.invoke_async([Message("user", "fake")], **args))
        runner.assert_awaited_once()
    else:
        if provider_cls is ClaudeProvider:
            monkeypatch.setattr(module, "run_subprocess_sync", lambda *a, **k: (b"", stderr.encode(), 1, False))
        else:
            monkeypatch.setattr(module, "run_owned_command", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", stderr))
        response = provider.invoke([Message("user", "fake")], **args)
    assert response.raw_stderr == stderr
    assert SECRET in response.error
    assert response.error_type == "auth" and response.exit_code == 1
    assert SECRET not in caplog.text
    assert SECRET not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_client_does_not_log_raw_provider_error(caplog):
    caplog.set_level(logging.DEBUG)
    response = LLMResponse("", "claude", "", error=SECRET, exit_code=1)
    LLMClient._log_provider_failure("claude", response)
    assert SECRET not in caplog.text
    assert response.error == SECRET


def test_streaming_stderr_is_not_logged_or_traced(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    proc = make_fake_proc(stdout_lines=jsonl_bytes([
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "result", "result": "ok", "session_id": "fake-session"},
    ]))
    proc.stderr = FakeReadline([(SECRET + "\n").encode()])
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-cli")
    monkeypatch.setattr(provider, "_spawn_env", lambda token: {})
    path = tmp_path / "stream.jsonl"

    async def consume():
        return [chunk async for chunk in provider.stream_async(
            [Message("user", "fake")], debug=True, debug_log_path=str(path),
            mcp_config={"fake": {"env": {"CUSTOM": SECRET}}})]

    chunks = asyncio.run(consume())
    assert [c.content for c in chunks if c.type == "text"] == ["ok"]
    assert chunks[-1].type == "done" and chunks[-1].content == "ok"
    assert SECRET not in caplog.text
    assert SECRET not in path.read_text(encoding="utf-8")


def test_stream_exception_message_is_not_automatically_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=RuntimeError(SECRET)))
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-cli")
    monkeypatch.setattr(provider, "_spawn_env", lambda token: {})

    async def consume():
        return [chunk async for chunk in provider.stream_async([Message("user", "fake")])]

    chunks = asyncio.run(consume())
    assert chunks[0].type == "error" and chunks[0].content == SECRET
    assert SECRET not in caplog.text


def test_redaction_markers_survive_repeated_trace_writes(tmp_path):
    argv = ["codex", "-c", "mcp_servers.fake={env={CUSTOM='" + SECRET + "'}}",
            "--", "fake-session", "fake prompt"]
    path = tmp_path / "trace.jsonl"
    write_debug_trace(str(path), {"argv": argv, "stderr": SECRET})
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    write_debug_trace(str(path), first)
    second = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    assert first["argv"] == second["argv"] == redact_argv(argv)
    assert first["stderr"] == second["stderr"] == f"<stderr:{len(SECRET)} chars>"
    assert second["argv"][-2] == "fake-session"


def test_trace_failure_does_not_echo_path_or_exception_body(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    write_debug_trace(str(tmp_path / SECRET / "trace.jsonl"), {"a": 1})
    assert SECRET not in caplog.text
    assert "FileNotFoundError" in caplog.text


def test_debug_stream_stale_recovery_still_uses_raw_stderr(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    stale = make_fake_proc(stdout_lines=[], returncode=1)
    stale.stderr = FakeReadline([("no rollout found for thread id old " + SECRET + "\n").encode()])
    fresh = make_fake_proc(stdout_lines=jsonl_bytes([
        {"type": "thread.started", "thread_id": "fake-new-session"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]))
    fresh.stderr = FakeReadline([])
    procs = iter([stale, fresh])

    async def spawn(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    provider = CodexProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake-cli")
    monkeypatch.setattr("agentcli.providers.codex.build_env", lambda: {})
    path = tmp_path / "trace.jsonl"

    async def consume():
        return [chunk async for chunk in provider.stream_async(
            [Message("user", "fake")], session_id="old", debug=True,
            debug_log_path=str(path))]

    chunks = asyncio.run(consume())
    assert not any(c.type == "error" for c in chunks)
    assert chunks[-1].content == "recovered"
    assert chunks[-1].session_id == "fake-new-session"
    assert SECRET not in caplog.text
    assert SECRET not in path.read_text(encoding="utf-8")
