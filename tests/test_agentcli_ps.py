"""scripts/agentcli_ps.py 진단 스크립트의 순수 매칭/파싱 로직 테스트.

핵심은 ``_is_agent`` 가 agentcli 의 **비대화형 호출만** 잡고 데스크탑 앱·
인터랙티브 세션은 제외하는 것(과매칭 회귀 방지). 라이브 ``ps`` 의존부는
테스트하지 않는다.
"""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "agentcli_ps.py"
_spec = importlib.util.spec_from_file_location("agentcli_ps", _SCRIPT)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def test_is_agent_matches_agentcli_invocations():
    assert ps._is_agent("/usr/bin/claude -p hi --output-format stream-json --verbose")
    assert ps._is_agent("/Users/x/.local/bin/claude -p prompt --output-format json")
    assert ps._is_agent("codex exec --json -- sid prompt")
    assert ps._is_agent("copilot -p hello --no-color")
    assert ps._is_agent("gh copilot suggest something")


def test_is_agent_excludes_desktop_and_interactive():
    # 데스크탑 GUI 앱
    assert not ps._is_agent("/Applications/Claude.app/Contents/MacOS/Claude")
    assert not ps._is_agent(
        "/Applications/Codex.app/Contents/Resources/codex app-server")
    # 인터랙티브 세션 (-p/--output-format 없음)
    assert not ps._is_agent("claude")
    assert not ps._is_agent("claude -c")
    # 스크립트 자신
    assert not ps._is_agent("python scripts/agentcli_ps.py")


def test_parse_etime():
    assert ps._parse_etime("05") == 5
    assert ps._parse_etime("02:03") == 123          # mm:ss
    assert ps._parse_etime("01:02:03") == 3723      # hh:mm:ss
    assert ps._parse_etime("1-00:00:10") == 86410   # dd-hh:mm:ss


def test_collect_groups_includes_agent_group_with_children_only():
    procs = [
        # agent leader (claude -p) + 그 자식(MCP) 같은 PGID
        {"pid": 100, "ppid": 50, "pgid": 100, "elapsed": 3, "stat": "Ss",
         "args": "/u/claude -p hi --output-format stream-json"},
        {"pid": 101, "ppid": 100, "pgid": 100, "elapsed": 3, "stat": "S",
         "args": "node mcp-server.js"},
        # 데스크탑 앱 — 별도 그룹, agent 아님 → 제외
        {"pid": 200, "ppid": 1, "pgid": 200, "elapsed": 5, "stat": "S",
         "args": "/Applications/Claude.app/Contents/MacOS/Claude"},
        # 무관 프로세스
        {"pid": 300, "ppid": 1, "pgid": 300, "elapsed": 9, "stat": "S",
         "args": "/usr/sbin/sshd"},
    ]
    groups = ps.collect_groups(procs)
    assert set(groups) == {100}, "agent 리더가 있는 그룹만"
    assert {p["pid"] for p in groups[100]} == {100, 101}, "리더 + 자식 모두 포함"


def test_snapshot_parses_ps_output(monkeypatch):
    fake = ("  100    50   100 01:02:03 Ss   /u/claude -p hi --output-format json\n"
            "bad short line\n"
            "  101   100   100 00:05 S     node mcp-server.js\n")
    monkeypatch.setattr(ps.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": fake})())
    procs = ps._snapshot()
    pids = {p["pid"]: p for p in procs}
    assert pids[100]["elapsed"] == 3723         # 01:02:03
    assert pids[101]["elapsed"] == 5            # 00:05
    assert pids[100]["ppid"] == 50 and pids[100]["pgid"] == 100
    # 짧은/깨진 라인은 건너뛴다
    assert all("bad short" not in p["args"] for p in procs)


def test_render_groups_with_flags():
    groups = {100: [
        {"pid": 100, "ppid": 50, "pgid": 100, "elapsed": 700, "stat": "Ss",
         "args": "/u/claude -p hi --output-format json"},
        {"pid": 101, "ppid": 1, "pgid": 100, "elapsed": 5, "stat": "Z",
         "args": "<defunct>"},
    ]}
    out = ps.render(groups, 600)
    assert "PGID 100" in out
    assert "claude" in out
    assert "long" in out and "defunct" in out and "residual" in out


def test_render_empty_message():
    assert "없음" in ps.render({}, 600)


def _one_agent_snapshot():
    return [{"pid": 100, "ppid": 50, "pgid": 100, "elapsed": 3, "stat": "S",
             "args": "/u/claude -p x --output-format json"}]


def test_main_json_output(monkeypatch, capsys):
    monkeypatch.setattr(ps, "_snapshot", _one_agent_snapshot)
    rc = ps.main(["--json"])
    assert rc == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert "100" in data and data["100"][0]["pid"] == 100


def test_main_older_than_filters_out_young(monkeypatch, capsys):
    monkeypatch.setattr(ps, "_snapshot", _one_agent_snapshot)
    rc = ps.main(["--older-than", "600"])   # 3s < 600 → 걸러짐
    assert rc == 0
    assert "PGID 100" not in capsys.readouterr().out


def test_main_kill_sends_sigkill(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ps.os, "killpg", lambda pg, sig: calls.append((pg, sig)))
    rc = ps.main(["--kill", "123"])
    assert rc == 0
    assert calls and calls[0][0] == 123


def test_main_kill_missing_group_returns_1(monkeypatch, capsys):
    def boom(pg, sig):
        raise ProcessLookupError
    monkeypatch.setattr(ps.os, "killpg", boom)
    rc = ps.main(["--kill", "999"])
    assert rc == 1


def test_flags_defunct_residual_long():
    thr = 600
    assert "defunct" in ps._flags(
        {"pid": 1, "ppid": 2, "pgid": 3, "elapsed": 1, "stat": "Z"}, thr)
    # 부모(파이썬) 사라진 잔여 — ppid 1
    assert "residual" in ps._flags(
        {"pid": 5, "ppid": 1, "pgid": 5, "elapsed": 1, "stat": "S"}, thr)
    assert "long" in ps._flags(
        {"pid": 1, "ppid": 2, "pgid": 3, "elapsed": 601, "stat": "S"}, thr)
    # 정상: 플래그 없음
    assert ps._flags(
        {"pid": 1, "ppid": 2, "pgid": 3, "elapsed": 5, "stat": "S"}, thr) == []
