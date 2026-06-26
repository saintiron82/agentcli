"""``_run_stream_template`` runtime 회로 + ``run_subprocess_async`` 회귀 테스트.

triad-review 합의 (PR #8) follow-up:
- issue #9: ``wall_timeout=0`` silent override 수정
- issue #10: ``except Exception`` 이 ``GeneratorExit`` 못 잡아 좀비 잔존
- issue #11: runtime 회로 mock 단위 (정상 시퀀스, idle/wall timeout)
"""
from __future__ import annotations

import asyncio

import pytest

from agentcli.providers.base import StreamState
from agentcli.providers.claude import ClaudeProvider
from tests._stream_helpers import (
    FakeReadline, HangingReadline, jsonl_bytes, make_fake_proc,
    patch_subprocess_exec)


# ============================================================
# 정상 시퀀스 (sanity check + #11 일부)
# ============================================================


def test_stream_partial_messages_incremental_text(monkeypatch):
    """partial_messages: text_delta 로 증분 방출, 뒤따르는 전체 assistant 블록은
    중복 집계하지 않는다."""
    from unittest.mock import patch
    from agentcli.types import Message
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "index": 0, "delta": {"type": "text_delta", "text": "lo"}}},
        # 델타와 별개로 전체 assistant 블록도 뒤따라온다 — 중복되면 안 된다.
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Hello"}]}},
        {"type": "result", "subtype": "success", "result": "Hello",
         "usage": {"input_tokens": 3, "output_tokens": 2}, "session_id": "s1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()

    async def run():
        with patch.object(provider, "_find_binary", return_value="/usr/bin/claude"):
            return [c async for c in provider.stream_async(
                [Message(role="user", content="hi")], partial_messages=True)]

    chunks = asyncio.run(run())
    texts = [c.content for c in chunks if c.type == "text"]
    # 증분 델타만 text 청크로, 전체 블록 중복 없음
    assert texts == ["Hel", "lo"], f"got {texts}"
    done = [c for c in chunks if c.type == "done"][-1]
    assert done.content == "Hello", "done content == 델타 합"


def test_stream_partial_no_delta_falls_back_to_full_block(monkeypatch):
    """partial=True 인데 델타 없이 전체 assistant 블록만 오면 텍스트를 유실하지
    않고 fallback 으로 방출한다 (merge-gate Important 회귀)."""
    from unittest.mock import patch
    from agentcli.types import Message
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        # text_delta 가 한 번도 안 옴 — 전체 블록만 도착
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "WHOLE answer"}]}},
        {"type": "result", "subtype": "success", "result": "WHOLE answer",
         "session_id": "s1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()

    async def run():
        with patch.object(provider, "_find_binary", return_value="/usr/bin/claude"):
            return [c async for c in provider.stream_async(
                [Message(role="user", content="hi")], partial_messages=True)]

    chunks = asyncio.run(run())
    texts = [c.content for c in chunks if c.type == "text"]
    assert texts == ["WHOLE answer"], f"델타 없으면 전체 블록 fallback 필요, got {texts}"
    done = [c for c in chunks if c.type == "done"][-1]
    assert done.content == "WHOLE answer", "텍스트 유실 금지"


def test_stream_partial_thinking_and_message_start_reset(monkeypatch):
    """partial: thinking_delta 는 thinking 청크로(텍스트 합엔 미포함), 그리고
    message_start 가 delta-seen 플래그를 리셋한다."""
    from unittest.mock import patch
    from agentcli.types import Message
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "hmm"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "ans"}}},
        # 전체 블록 — thinking/text 모두 델타로 왔으니 중복 방출 금지
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "ans"}]}},
        {"type": "result", "subtype": "success", "result": "ans",
         "session_id": "s1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()

    async def run():
        with patch.object(provider, "_find_binary", return_value="/usr/bin/claude"):
            return [c async for c in provider.stream_async(
                [Message(role="user", content="hi")], partial_messages=True)]

    chunks = asyncio.run(run())
    assert [c.content for c in chunks if c.type == "thinking"] == ["hmm"]
    assert [c.content for c in chunks if c.type == "text"] == ["ans"]
    done = [c for c in chunks if c.type == "done"][-1]
    assert done.content == "ans", "thinking 은 최종 content 에 포함되지 않는다"


def test_codex_stream_stale_session_recovers(monkeypatch):
    """codex 스트리밍: 죽은 thread(첫 청크가 'no rollout found' 에러) → 새 세션
    으로 1회 재시도해 복구."""
    from agentcli.providers.codex import CodexProvider
    from agentcli.types import Message
    stale = make_fake_proc(
        stdout_lines=[], returncode=1,
        stderr_bytes=b"Error: thread/resume failed: no rollout found for thread id abc")
    fresh = make_fake_proc(stdout_lines=jsonl_bytes([
        {"type": "thread.started", "thread_id": "tid-new"},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "recovered"}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]), returncode=0)
    procs = iter([stale, fresh])

    async def fake_create(*a, **k):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(CodexProvider, "_find_binary", lambda self: "/usr/bin/codex")
    monkeypatch.setattr("agentcli.providers.codex.build_env",
                        lambda: {"PATH": "/usr/bin"})
    prov = CodexProvider()

    async def run():
        return [c async for c in prov.stream_async(
            [Message(role="user", content="hi")], session_id="abc")]

    chunks = asyncio.run(run())
    # stale 에러는 caller 에 새지 않고, 복구된 text/done 만 보인다.
    assert not any(c.type == "error" for c in chunks), "stale 에러 누출 금지"
    done = [c for c in chunks if c.type == "done"][-1]
    assert done.content == "recovered"
    assert done.session_id == "tid-new"


def test_codex_stream_stale_recovery_is_bounded(monkeypatch):
    """스트리밍 재시도(새 세션)도 stale 면 무한 루프 없이 에러를 caller 에 전달."""
    from agentcli.providers.codex import CodexProvider
    from agentcli.types import Message
    stale = make_fake_proc(
        stdout_lines=[], returncode=1,
        stderr_bytes=b"no rollout found for thread id abc")
    # 두 번 모두 stale
    procs = iter([stale, make_fake_proc(
        stdout_lines=[], returncode=1,
        stderr_bytes=b"no rollout found for thread id abc")])

    async def fake_create(*a, **k):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(CodexProvider, "_find_binary", lambda self: "/usr/bin/codex")
    monkeypatch.setattr("agentcli.providers.codex.build_env",
                        lambda: {"PATH": "/usr/bin"})
    prov = CodexProvider()

    async def run():
        return [c async for c in prov.stream_async(
            [Message(role="user", content="hi")], session_id="abc")]

    chunks = asyncio.run(run())
    # 2번째(새 세션)도 실패 → 에러 청크가 caller 에 전달됨 (무한 루프 아님)
    assert any(c.type == "error" for c in chunks)


def test_codex_stream_debug_writes_trace(monkeypatch, tmp_path):
    """codex 스트리밍도 debug 계측(청크 타임라인 + trace) — 전 provider 확대."""
    import json
    from agentcli.providers.codex import CodexProvider
    from agentcli.types import Message
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "hi"}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 3, "output_tokens": 2}},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    proc.stderr = FakeReadline([b"codex debug stderr\n"])
    patch_subprocess_exec(monkeypatch, proc)
    monkeypatch.setattr(CodexProvider, "_find_binary", lambda self: "/usr/bin/codex")
    monkeypatch.setattr("agentcli.providers.codex.build_env",
                        lambda: {"PATH": "/usr/bin"})
    prov = CodexProvider()
    trace = tmp_path / "codex.jsonl"

    async def run():
        return [c async for c in prov.stream_async(
            [Message(role="user", content="hi")],
            debug=True, debug_log_path=str(trace))]

    chunks = asyncio.run(run())
    assert any(c.type == "text" for c in chunks)
    rec = json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "stream"
    assert rec["schema"] == 1 and len(rec["call_id"]) == 12
    assert any(c["type"] == "text" for c in rec["chunks"])


def test_stream_partial_with_tool_use_interleave(monkeypatch):
    """partial: 텍스트는 델타로, tool_use 는 전체 assistant 블록에서, tool_result
    는 user 블록에서 — 텍스트 중복 없이 올바른 순서/최종 content."""
    from unittest.mock import patch
    from agentcli.types import Message
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "Use tool"}}},
        # 전체 블록: text(델타로 이미 옴 → skip) + tool_use(전체 블록에서 방출)
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Use tool"},
            {"type": "tool_use", "name": "Read", "id": "t1", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "result": "Use tool",
         "session_id": "s1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()

    async def run():
        with patch.object(provider, "_find_binary", return_value="/usr/bin/claude"):
            return [c async for c in provider.stream_async(
                [Message(role="user", content="hi")], partial_messages=True)]

    chunks = asyncio.run(run())
    assert [c.content for c in chunks if c.type == "text"] == ["Use tool"]
    assert sum(1 for c in chunks if c.type == "tool_use") == 1
    assert sum(1 for c in chunks if c.type == "tool_result") == 1
    # 청크 순서: text → tool_use → tool_result
    seq = [c.type for c in chunks if c.type in ("text", "tool_use", "tool_result")]
    assert seq == ["text", "tool_use", "tool_result"]
    done = [c for c in chunks if c.type == "done"][-1]
    assert done.content == "Use tool"


def test_run_stream_template_debug_writes_trace(monkeypatch, tmp_path):
    """debug=True: 청크 타임라인 + redact argv + stderr 를 trace 파일로 기록."""
    import json
    events = [
        {"type": "system", "session_id": "sys-1"},
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "usage": {"input_tokens": 3, "output_tokens": 2},
         "session_id": "sys-1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    # stderr 동시 드레인 검증을 위해 readline 가능한 fake 로 교체
    proc.stderr = FakeReadline([b"debug: mcp connected\n",
                                b"debug: tool_use Bash\n"])
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="seed")
    trace = tmp_path / "trace.jsonl"
    cmd = ["claude", "-p", "SECRET-PROMPT", "--debug",
           "--output-format", "stream-json"]

    async def run():
        return [c async for c in provider._run_stream_template(
            cmd, state, model="m", debug=True, debug_log_path=str(trace))]

    chunks = asyncio.run(run())
    assert [c.type for c in chunks][-1] == "done"

    rec = json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["phase"] == "stream"
    assert rec["chunk_count"] >= 2
    assert any(c["type"] == "text" for c in rec["chunks"])
    # 정상 done 도달 → 부분 데이터 아님
    assert rec["truncated"] is False
    # 프롬프트 본문 redact
    assert "SECRET-PROMPT" not in json.dumps(rec["argv"])
    # stderr 동시 드레인되어 trace 에 담김
    assert "mcp connected" in rec["stderr"]


def test_run_stream_template_debug_trace_truncated_on_timeout(monkeypatch, tmp_path):
    """idle timeout 으로 중단되면 trace 의 truncated=True (Fix B — 부분 데이터 표시)."""
    import json
    proc = make_fake_proc(stdout_lines=[], returncode=None)
    proc.stdout = HangingReadline()       # readline 영원히 → idle timeout
    proc.stderr = FakeReadline([b"partial\n"])
    patch_subprocess_exec(monkeypatch, proc)
    provider = ClaudeProvider()
    state = StreamState()
    trace = tmp_path / "t.jsonl"

    async def run():
        return [c async for c in provider._run_stream_template(
            ["claude", "-p", "X"], state, timeout=0.2,
            debug=True, debug_log_path=str(trace))]

    chunks = asyncio.run(run())
    assert any(c.type == "error" for c in chunks)
    rec = json.loads(trace.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["truncated"] is True


def test_run_stream_template_normal_sequence(monkeypatch):
    """JSONL stdout → dispatch → text + done chunk 정상 흐름."""
    events = [
        {"type": "system", "session_id": "sys-1"},
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "usage": {"input_tokens": 3, "output_tokens": 2},
         "session_id": "sys-1"},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="seed")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="claude-test")]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]

    assert "text" in types, f"expected text chunk, got {types}"
    assert types[-1] == "done", f"expected last=done, got {types}"
    done = chunks[-1]
    assert done.content == "hi"
    assert done.session_id == "sys-1"
    assert done.data["provider"] == "claude"
    assert done.data["model"] == "claude-test"


# ============================================================
# issue #9: wall_timeout=0 silent override 회귀
# ============================================================


def test_wall_timeout_zero_is_not_silently_ignored(monkeypatch):
    """``wall_timeout=0`` 은 silent 로 None 처리되면 안 된다.

    pre-fix: ``if wall_timeout else None`` 패턴이 0 을 falsy 처리 → wall_deadline=None
             → wall 검사 비활성 → 사용자가 의도적으로 0 을 넘겨도 stream 그대로 진행.
    post-fix: ``is not None`` 체크 → wall_deadline = start + 0 = start
              → 첫 iteration 에서 wall deadline 이미 지남 → wall timeout error.

    이 테스트는 fix 전엔 실패하고 fix 후 통과해야 한다.
    """
    # 정상 chunk 가 도착하기 전에 wall 검사가 작동해야 함
    proc = make_fake_proc(
        stdout_lines=jsonl_bytes([{
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "should-not-arrive"}]},
        }]),
        returncode=0,
    )
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake-cmd"], state,
            model="m", cwd=None,
            timeout=100,
            wall_timeout=0,  # ← issue #9: silent override 시나리오
        )]

    chunks = asyncio.run(run())

    # fix 후: 첫 chunk 가 wall timeout error 여야 함
    error_chunks = [c for c in chunks if c.type == "error"]
    assert error_chunks, (
        "wall_timeout=0 should produce a wall timeout error chunk, "
        f"got chunks={[c.type for c in chunks]}")
    assert error_chunks[0].data.get("timeout_kind") == "wall", (
        f"expected timeout_kind='wall', got data={error_chunks[0].data}")
    # 정상 text chunk 가 도착하면 안 됨 (wall 이 그 전에 끊어야)
    text_chunks = [c for c in chunks if c.type == "text"]
    assert not text_chunks, (
        "wall_timeout=0 should preempt the normal text yield, "
        f"got text chunks: {[c.content for c in text_chunks]}")


def test_wall_timeout_none_still_means_no_wall_check(monkeypatch):
    """``wall_timeout=None`` (기본값) 은 여전히 wall 검사를 비활성한다 (regression guard).

    fix 가 ``is not None`` 으로 가도, ``None`` 인 경우는 wall 검사 안 함.
    """
    events = [
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "ok"}]}},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=0)
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m",
            timeout=100, wall_timeout=None)]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]
    # 정상 text + done, error 없음
    assert "text" in types
    assert types[-1] == "done"
    assert not [c for c in chunks if c.type == "error"]


# ============================================================
# issue #10: GeneratorExit cleanup 회귀
# ============================================================


def test_early_break_triggers_proc_cleanup(monkeypatch):
    """caller 가 ``async for ... break`` 로 일찍 종료해도 proc 가 정리되어야 한다.

    pre-fix: ``except Exception`` 이 ``GeneratorExit`` 을 못 잡아 cleanup
             (``proc.kill``) 실행 안 됨 → 좀비 프로세스 + idle task 잔존.
    post-fix: ``try/finally`` 로 cleanup 보장 → ``proc.kill`` 호출됨.

    이 테스트는 fix 전엔 실패하고 fix 후 통과해야 한다.
    """
    events = [
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "second"}]}},
        {"type": "result", "usage": {}, "session_id": "sid"},
    ]
    # returncode=None: proc 아직 살아있음을 시뮬레이션 (caller break 시점)
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=None)
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run_early_break():
        agen = provider._run_stream_template(
            ["fake"], state, model="m", timeout=100)
        async for c in agen:
            if c.type == "text":
                # 첫 text 받자마자 caller 가 break
                break
        # async generator 명시적 정리 (caller 가 일찍 종료한 패턴)
        await agen.aclose()

    asyncio.run(run_early_break())

    # fix 후: proc.kill 이 호출되어야 함
    assert proc.kill.called, (
        "Early break + aclose() should trigger proc.kill via finally cleanup. "
        "Pre-fix: except Exception missed GeneratorExit → no cleanup.")


# ============================================================
# issue #11-C: runtime 회로 추가 mock 케이스
# ============================================================


def test_idle_timeout_yields_idle_error_chunk(monkeypatch):
    """readline 이 hang 하면 idle timeout error chunk 가 즉시 발행되어야 한다."""
    proc = make_fake_proc(returncode=None)
    proc.stdout = HangingReadline()
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m",
            timeout=120,
            idle_timeout=0.01,  # 10ms — 즉시 idle timeout
        )]

    chunks = asyncio.run(run())
    error_chunks = [c for c in chunks if c.type == "error"]
    assert error_chunks, "idle hang should yield an idle timeout error chunk"
    assert error_chunks[0].data.get("timeout_kind") == "idle"
    # proc cleanup 검증 (finally 경로)
    assert proc.kill.called


def test_partial_success_yields_done_with_text(monkeypatch):
    """text chunk 발행 후 rc != 0 이어도 text_parts 가 있으면 done 발행 (현재 invariant).

    issue #5 (done.data 에 returncode 노출) 미래 fix 시 이 테스트가 done.data
    구조 변경을 잡는다.
    """
    events = [
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "partial"}]}},
    ]
    proc = make_fake_proc(stdout_lines=jsonl_bytes(events), returncode=1)
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m")]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]
    assert "text" in types
    assert types[-1] == "done", (
        f"text+rc≠0 should still yield done (current invariant), got {types}")
    done = chunks[-1]
    assert done.content == "partial"


def test_failed_run_with_no_text_yields_error_with_stderr(monkeypatch):
    """rc != 0 + 빈 text 는 stderr 를 담은 error chunk 로 surface."""
    proc = make_fake_proc(
        stdout_lines=[],
        returncode=1,
        stderr_bytes=b"cli failed: rate limit",
    )
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m")]

    chunks = asyncio.run(run())
    error_chunks = [c for c in chunks if c.type == "error"]
    assert error_chunks
    assert "cli failed: rate limit" in error_chunks[0].content
    assert error_chunks[0].data.get("returncode") == 1
    # done chunk 는 발행되면 안 됨
    assert not [c for c in chunks if c.type == "done"]


def test_json_decode_error_yields_event_chunk_and_continues(monkeypatch):
    """잘못된 JSON 라인은 raw event chunk 로 통과시키고 stream 은 계속."""
    bad_line = b"this-is-not-json\n"
    good_event = {"type": "assistant", "message": {
        "content": [{"type": "text", "text": "after-bad-line"}]}}
    proc = make_fake_proc(
        stdout_lines=[bad_line] + jsonl_bytes([good_event]),
        returncode=0,
    )
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="sid")

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m")]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]
    # event chunk (raw 잘못된 라인) + text chunk + done
    raw_events = [c for c in chunks if c.type == "event"
                  and c.data.get("raw") == "this-is-not-json"]
    assert raw_events, (
        f"bad JSON line should surface as raw event chunk, got types={types}")
    text_chunks = [c for c in chunks if c.type == "text"]
    assert text_chunks and text_chunks[0].content == "after-bad-line", (
        f"stream should continue after JSON decode error, got types={types}")
    assert types[-1] == "done"


# ============================================================
# 비-dict JSON 라인 방어: dispatcher 의 evt.get 이 스트림을 죽이지 않는다
# ============================================================


def test_non_dict_json_line_becomes_raw_event(monkeypatch):
    """유효한 JSON이지만 객체가 아닌 라인은 raw event 로 흘려보내고 계속 진행."""
    lines = [
        b'"Reading input from stdin..."\n',
        b'123\n',
        b'{"type": "assistant", "message": {"content": '
        b'[{"type": "text", "text": "ok"}]}}\n',
    ]
    proc = make_fake_proc(stdout_lines=lines, returncode=0)
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState()

    async def run():
        return [c async for c in provider._run_stream_template(
            ["fake"], state, model="m")]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]
    raw_events = [c for c in chunks
                  if c.type == "event" and "raw" in (c.data or {})]
    assert len(raw_events) == 2, f"non-dict lines must surface as raw events: {types}"
    assert "text" in types
    assert types[-1] == "done"


# ============================================================
# claude stream stale-session 자동 복구
# ============================================================


def test_claude_stream_stale_session_retries_with_new_session(monkeypatch):
    """첫 청크가 stale-session 에러면 새 세션으로 1회 재시도."""
    from agentcli.types import Message

    stale_proc = make_fake_proc(
        stdout_lines=[], returncode=1,
        stderr_bytes=b"No conversation found with session ID: old-sid")
    ok_events = [
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "recovered"}]}},
        {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1},
         "session_id": "new-sid"},
    ]
    ok_proc = make_fake_proc(stdout_lines=jsonl_bytes(ok_events), returncode=0)

    procs = iter([stale_proc, ok_proc])
    spawned_cmds: list[list[str]] = []

    async def fake_create(*args, **kwargs):
        spawned_cmds.append(list(args))
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        ClaudeProvider, "_find_binary", lambda self: "/usr/bin/claude")

    provider = ClaudeProvider()
    provider.supports_sessions = True

    async def run():
        return [c async for c in provider.stream_async(
            [Message(role="user", content="hi")], session_id="old-sid")]

    chunks = asyncio.run(run())
    types = [c.type for c in chunks]
    assert "error" not in types, f"stale 에러는 재시도로 흡수되어야 함: {types}"
    assert "text" in types
    assert chunks[-1].type == "done"
    assert chunks[-1].content == "recovered"
    assert chunks[-1].session_id == "new-sid"
    assert len(spawned_cmds) == 2
    assert "--resume" in spawned_cmds[0]
    assert "--resume" not in spawned_cmds[1]
    assert "--session-id" in spawned_cmds[1]


def test_claude_stream_non_stale_error_not_retried(monkeypatch):
    """stale 외의 스트림 실패는 재시도 없이 error 청크 그대로 전달."""
    from agentcli.types import Message

    fail_proc = make_fake_proc(
        stdout_lines=[], returncode=1,
        stderr_bytes=b"usage limit reached")
    spawn_count = 0

    async def fake_create(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return fail_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        ClaudeProvider, "_find_binary", lambda self: "/usr/bin/claude")

    provider = ClaudeProvider()
    provider.supports_sessions = True

    async def run():
        return [c async for c in provider.stream_async(
            [Message(role="user", content="hi")], session_id="old-sid")]

    chunks = asyncio.run(run())
    assert [c.type for c in chunks] == ["error"]
    assert "usage limit" in chunks[0].content
    assert spawn_count == 1


# ============================================================
# run_subprocess_async: 호출 task 취소 시 proc 정리 (invoke 경로 좀비 방지)
# ============================================================


def test_run_subprocess_async_cancellation_kills_proc(monkeypatch):
    """CancelledError 전파 경로에서도 subprocess 가 kill 되어야 한다."""
    from unittest.mock import AsyncMock, MagicMock
    from agentcli.providers.base import run_subprocess_async

    proc = make_fake_proc()

    async def hang():
        await asyncio.Event().wait()

    proc.communicate = hang
    proc.returncode = None
    proc.kill = MagicMock(
        side_effect=lambda: setattr(proc, "returncode", -9))
    proc.wait = AsyncMock(return_value=-9)
    patch_subprocess_exec(monkeypatch, proc)

    async def run():
        task = asyncio.create_task(run_subprocess_async(["x"], timeout=60))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    proc.kill.assert_called_once()


def test_run_subprocess_async_timeout_still_kills_proc(monkeypatch):
    """기존 timeout 경로 회귀: finally 로 이동한 kill 이 그대로 동작."""
    from unittest.mock import AsyncMock, MagicMock
    from agentcli.providers.base import run_subprocess_async

    proc = make_fake_proc()

    async def hang():
        await asyncio.Event().wait()

    proc.communicate = hang
    proc.returncode = None
    proc.kill = MagicMock(
        side_effect=lambda: setattr(proc, "returncode", -9))
    proc.wait = AsyncMock(return_value=-9)
    patch_subprocess_exec(monkeypatch, proc)

    async def run():
        return await run_subprocess_async(["x"], timeout=0)

    stdout_b, stderr_b, rc, timed_out = asyncio.run(run())
    assert timed_out is True
    assert rc == 124
    # Fix A: 정상완료 포함 모든 경로에서 그룹 reap → except+finally 가 멱등적으로
    # 두 번 kill 할 수 있다(해롭지 않음). 한 번 이상 호출되면 충분.
    assert proc.kill.called
