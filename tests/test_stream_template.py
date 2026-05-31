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
    HangingReadline, jsonl_bytes, make_fake_proc, patch_subprocess_exec)


# ============================================================
# 정상 시퀀스 (sanity check + #11 일부)
# ============================================================


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
