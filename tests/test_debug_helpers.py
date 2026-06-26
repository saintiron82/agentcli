"""debug 계측 헬퍼(redact_argv / write_debug_trace) 단위 테스트."""
import json

from agentcli.providers.base import redact_argv, write_debug_trace


def test_redact_argv_hides_prompt_payload():
    cmd = ["/bin/claude", "-p", "비밀 50k 본문",
           "--output-format", "json", "--model", "haiku"]
    out = redact_argv(cmd)
    assert out[1] == "-p"
    assert out[2].startswith("<prompt:") and out[2].endswith("chars>")
    assert "비밀 50k 본문" not in " ".join(out)
    # -p 외의 인자는 그대로 보존
    assert "--output-format" in out and "haiku" in out


def test_redact_argv_without_prompt_unchanged():
    cmd = ["claude", "--version"]
    assert redact_argv(cmd) == cmd


def test_redact_argv_only_redacts_arg_after_dash_p():
    cmd = ["claude", "-p", "PROMPT", "--allowedTools", "Read"]
    out = redact_argv(cmd)
    assert out[2].startswith("<prompt:")
    assert out[4] == "Read"  # -p 다음 한 인자만 redact


def test_write_debug_trace_appends_jsonl(tmp_path):
    p = tmp_path / "trace.jsonl"
    write_debug_trace(str(p), {"a": 1})
    write_debug_trace(str(p), {"b": "둘", "n": 2})
    recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    # 입력 필드 보존 + schema/call_id 자동 stamp
    assert recs[0]["a"] == 1 and recs[1]["b"] == "둘" and recs[1]["n"] == 2
    for r in recs:
        assert r["schema"] == 1
        assert len(r["call_id"]) == 12
    # call_id 는 호출마다 고유
    assert recs[0]["call_id"] != recs[1]["call_id"]


def test_write_debug_trace_thread_safe(tmp_path):
    """여러 스레드가 같은 파일에 써도 라인이 깨지지 않는다(병렬 안전)."""
    import threading
    p = tmp_path / "t.jsonl"

    def w(i):
        write_debug_trace(str(p), {"i": i, "pad": "x" * 5000})  # 4KB 초과

    threads = [threading.Thread(target=w, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    for line in lines:
        json.loads(line)  # 각 라인이 온전한 JSON (interleave 안 됨)


def test_write_debug_trace_bad_path_does_not_raise():
    # 존재하지 않는 디렉토리 → best-effort, 예외 없이 (경고만).
    write_debug_trace("/no/such/dir_xyz123/trace.jsonl", {"a": 1})
