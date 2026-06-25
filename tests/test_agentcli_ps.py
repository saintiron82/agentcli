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
