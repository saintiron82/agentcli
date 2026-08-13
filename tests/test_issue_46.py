"""issue #46 — debug 스트림 중도 취소 시 stderr_task 정리가 CancelledError 를 놓친다.

``_run_stream_template`` 의 stderr_task 정리 2곳이 ``except Exception:`` 만
잡는데, ``asyncio.CancelledError`` 는 3.8+ 에서 BaseException 상속이라 그물을
빠져나간다. finally 는 **우리가 방금 cancel() 을 요청한** task 를 await 하므로
CancelledError 는 예외 상황이 아니라 정상 결과다 — 그것이 새면:

- 템플릿을 직접 소비하는 쪽에서는 ``aclose()`` 가 CancelledError 로 터지고,
- provider ``stream_async`` 처럼 ``async for`` 로 감싼 경로에서는 이벤트 루프의
  asyncgen 파이널라이저 태스크에서 경고로 표출되며,
- 어느 쪽이든 **finally 의 후속 정리(debug trace 마감 기록)가 중단**된다.

#44 가 stdin_task 정리에 이미 쓴 패턴(``except (Exception,
asyncio.CancelledError)``)을 stderr_task 2곳에 미러하면 된다. 재현은 감싸는
generator 를 거치지 않고 템플릿을 직접 구동해야 결정적이다.
"""

import asyncio

from tests._stream_helpers import patch_subprocess_exec, jsonl_bytes, make_fake_proc

from agentcli.providers.base import StreamState
from agentcli.providers.claude import ClaudeProvider


class _HangAfter:
    """준비된 라인을 소진한 뒤에는 취소될 때까지 잠드는 readline mock.

    EOF(b"") 를 돌려주는 FakeReadline 과 달리, 소비자가 스트림을 중도
    포기(aclose)하는 시점에 stdout 읽기와 stderr 드레인 task 가 모두 '진행
    중'인 상태를 만들기 위한 것이다.
    """

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(3600)
        return b""


def test_aclose_mid_stream_with_debug_completes_cleanup(monkeypatch, tmp_path):
    """debug 스트림을 첫 청크 후 aclose — CancelledError 가 새지 않고
    finally 가 끝까지 돌아 trace 마감까지 기록돼야 한다."""
    events = [{"type": "assistant",
               "message": {"content": [{"type": "text", "text": "hi"}]}}]
    proc = make_fake_proc(returncode=None)
    proc.stdout = _HangAfter(jsonl_bytes(events))    # 청크 1개 후 진행 중
    proc.stderr = _HangAfter([])                      # 드레인 task 도 진행 중
    patch_subprocess_exec(monkeypatch, proc)

    provider = ClaudeProvider()
    state = StreamState(final_session_id="seed")
    trace = tmp_path / "trace.jsonl"

    async def run():
        agen = provider._run_stream_template(
            ["claude", "-p", "x", "--output-format", "stream-json"],
            state, model="m", debug=True, debug_log_path=str(trace))
        first = await agen.__anext__()
        # 수정 전: finally 의 stderr_task cancel+await 에서 CancelledError 가
        # 새어 aclose() 가 예외로 끝난다. 수정 후: 조용히 완료.
        await agen.aclose()
        return first

    first = asyncio.run(run())
    assert first.type == "text" and first.content == "hi"
    # finally 후속 정리의 완주 증거 — CancelledError 가 샜다면 이 기록 전에
    # finally 가 중단되어 trace 파일이 비거나 없다.
    assert trace.exists()
    assert '"phase": "stream"' in trace.read_text(encoding="utf-8")
