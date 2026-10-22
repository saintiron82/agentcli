import asyncio
import json
import subprocess
import pytest
from agentcli.providers.codex import CodexProvider
from agentcli.types import Message, StreamChunk


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("sequence,start_sid,expected_calls,expected_answer", [
    (["stale", "greeting", "answer"], "old", 2, False),
    (["greeting", "stale", "answer"], "", 2, False),
    (["greeting", "greeting"], "", 2, False),
    (["stale", "stale"], "old", 2, False),
    (["stale", "answer"], "old", 2, True),
    (["greeting", "answer"], "", 2, True),
    (["tools", "answer"], "", 1, False),
])
def test_mixed_recovery_is_bounded_and_reports_failure(monkeypatch, mode, sequence, start_sid,
                                                      expected_calls, expected_answer):
    provider = CodexProvider()
    attempts = []
    def step():
        kind = sequence[len(attempts)]
        attempts.append(kind)
        if kind == "stale":
            return "", "no rollout found for thread id old", 1
        output = events(kind != "answer", tools=kind == "tools")
        return "\n".join(json.dumps(event) for event in output), "", 0
    monkeypatch.setattr(provider, "_build_cmd", lambda *args, **kwargs: ["fake"])
    def sync(*args, **kwargs):
        stdout, stderr, code = step()
        return subprocess.CompletedProcess([], code, stdout, stderr)
    async def asynchronous(*args, **kwargs):
        stdout, stderr, code = step()
        return stdout.encode(), stderr.encode(), code, False
    async def streaming(cmd, state, **kwargs):
        stdout, stderr, code = step()
        if code:
            yield StreamChunk(type="error", content=stderr)
            return
        for line in stdout.splitlines():
            async for chunk in provider._dispatch_stream_event(json.loads(line), state):
                yield chunk
        yield StreamChunk(type="done", content="".join(state.text_parts),
                          session_id=state.final_session_id, usage=state.final_usage)
    monkeypatch.setattr("agentcli.providers.codex.run_owned_command", sync)
    monkeypatch.setattr("agentcli.providers.codex.run_subprocess_async", asynchronous)
    monkeypatch.setattr(provider, "_run_stream_template", streaming)
    messages = [Message(role="user", content="Do this task")]
    expected_usage = sum(25 if kind == "answer" else 110 if kind in ("greeting", "tools") else 0
                         for kind in sequence[:expected_calls])
    if mode == "sync":
        result = provider.invoke(messages, session_id=start_sid)
    elif mode == "async":
        result = asyncio.run(provider.invoke_async(messages, session_id=start_sid))
    else:
        async def consume():
            return [chunk async for chunk in provider.stream_async(messages, session_id=start_sid)]
        chunks = asyncio.run(consume())
        terminal = [chunk for chunk in chunks if chunk.type in ("error", "done")][-1]
        assert terminal.type == ("done" if expected_answer else "error")
        if "greeting" in attempts or "tools" in attempts:
            assert terminal.usage.total_tokens == expected_usage
        result = None
    assert len(attempts) == expected_calls
    if result is not None:
        assert result.content == ("ANSWER" if expected_answer else "")
        assert bool(result.error) == (not expected_answer)
        assert bool(result.error_type) == (not expected_answer)
        if "greeting" in attempts or "tools" in attempts:
            assert result.tokens.total_tokens == expected_usage


def events(greeting, tools=False):
    output = [{"type": "thread.started", "thread_id": "fake-thread"},
              {"type": "item.completed", "item": {"type": "agent_message",
               "text": "Ready. What would you like me to work on?" if greeting else "ANSWER"}},
              {"type": "turn.completed", "usage": {"input_tokens": 100 if greeting else 20,
                                                    "output_tokens": 10 if greeting else 5}}]
    if tools: output.insert(1, {"type": "item.completed", "item": {"type": "tool_call"}})
    return output


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("greeting", [False, True])
def test_codex_modes_share_retry_and_usage_contract(monkeypatch, mode, greeting):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake")
    monkeypatch.setattr("agentcli.providers.codex.build_env", lambda: {})
    commands = []
    def native(cmd):
        commands.append(cmd)
        return events(greeting and len(commands) == 1)
    def sync(cmd, **options):
        return subprocess.CompletedProcess(cmd, 0, "\n".join(map(json.dumps, native(cmd))), "")
    async def asynchronous(cmd, **options):
        return sync(cmd).stdout.encode(), b"", 0, False
    async def stream(cmd, state, **options):
        for event in native(cmd):
            async for chunk in provider._dispatch_stream_event(event, state): yield chunk
        yield StreamChunk(type="done", content="".join(state.text_parts),
                          usage=state.final_usage, session_id=state.final_session_id)
    monkeypatch.setattr("agentcli.providers.codex.run_owned_command", sync)
    monkeypatch.setattr("agentcli.providers.codex.run_subprocess_async", asynchronous)
    monkeypatch.setattr(provider, "_run_stream_template", stream)
    message = [Message("user", "x" * 100)]
    async def run():
        if mode == "sync": return provider.invoke(message)
        if mode == "async": return await provider.invoke_async(message)
        chunks = [c async for c in provider.stream_async(message)]
        assert sum(c.type == "done" for c in chunks) == 1
        return chunks[-1]
    response = asyncio.run(run())
    usage = response.usage if mode == "stream" else response.tokens
    assert response.content == "ANSWER"
    assert response.session_id == "fake-thread"
    assert len(commands) == (2 if greeting else 1)
    assert usage.total_tokens == (135 if greeting else 25)
    assert usage.payload_prompt_tokens == (50 if greeting else 25)
    assert not usage.prompt_tokens_reliable and usage.prompt_tokens_source == "codex_cli_reported"
    if greeting: assert "resume" in commands[1] and "fake-thread" in commands[1]


def test_greeting_after_tool_execution_is_not_reissued():
    from agentcli.providers.codex import _parse_jsonl_events, _needs_initial_greeting_retry
    parsed = _parse_jsonl_events("\n".join(map(json.dumps, events(True, tools=True))))
    assert not _needs_initial_greeting_retry(parsed, "")


def test_async_greeting_stale_cycle_is_bounded(monkeypatch):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "_find_binary", lambda: "fake")
    monkeypatch.setattr("agentcli.providers.codex.build_env", lambda: {})
    calls = []
    async def native(cmd, **options):
        calls.append(cmd)
        if "resume" in cmd: return b"", b"no rollout found for thread id fake", 1, False
        return "\n".join(map(json.dumps, events(True))).encode(), b"", 0, False
    monkeypatch.setattr("agentcli.providers.codex.run_subprocess_async", native)
    response = asyncio.run(provider.invoke_async([Message("user", "task")]))
    assert len(calls) == 2 and not response.content and response.error
    assert response.tokens.total_tokens == 110
