#!/usr/bin/env python3
"""agentcli 가 띄운 CLI(claude/codex/copilot)와 그 자식(MCP 서버·node 헬퍼)의
실행 상태를 추적하는 진단 스크립트.

agentcli 는 각 CLI 를 새 세션(프로세스 그룹)으로 띄우므로(0.6.2+), **PGID 단위로
묶으면 "한 번의 agent 실행 = 한 그룹"** 으로 잔여/행/좀비를 한눈에 본다.

의존성 없음 — 표준 라이브러리 + 시스템 ``ps`` 만 사용 (POSIX: macOS/Linux).

사용 예:
    python scripts/agentcli_ps.py                 # 실행 중인 agent 그룹 트리
    python scripts/agentcli_ps.py --older-than 300  # 5분 넘게 산 그룹만
    python scripts/agentcli_ps.py --json           # 기계 판독용
    python scripts/agentcli_ps.py --kill <PGID>    # 그룹 전체 SIGKILL (좀비 정리)

플래그(자동 표시): [defunct]=좀비(Z), [residual]=부모 없음(PPID 1), [long]=오래됨.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys

# 위양성 제외: 이 스크립트 자신·grep·ps 자체.
_EXCLUDE_RE = re.compile(r"agentcli_ps\.py|grep |ps -ao", re.IGNORECASE)


def _is_agent(args: str) -> bool:
    """agentcli 가 띄운 **비대화형** CLI 호출인지 — 데스크탑 GUI 앱이나
    인터랙티브 세션이 아니라 ``-p``/``exec`` 호출만 매칭한다.

    시그니처(agentcli 가 실제로 빌드하는 커맨드):
      - claude:  ``claude -p <prompt> --output-format json|stream-json``
      - codex:   ``codex exec ... --json``
      - copilot: ``copilot -p <prompt> ...`` 또는 ``gh copilot``
    """
    low = args.lower()
    if _EXCLUDE_RE.search(low):
        return False
    # 데스크탑 GUI 앱 제외 (Claude.app / Codex.app / VS Code 확장 등).
    if "/applications/" in low or ".app/contents/" in low:
        return False
    if "claude" in low and " -p" in low and "--output-format" in low:
        return True
    if "codex" in low and " exec" in low and "--json" in low:
        return True
    if ("copilot" in low and " -p" in low) or "gh copilot" in low:
        return True
    return False


def _parse_etime(etime: str) -> int:
    """ps etime([[dd-]hh:]mm:ss) → 초."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + s


def _snapshot() -> list[dict]:
    """전체 프로세스 목록 (pid/ppid/pgid/elapsed/stat/args)."""
    out = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,pgid=,etime=,stat=,args="],
        capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, pgid, etime, stat, args = parts
        try:
            procs.append({
                "pid": int(pid), "ppid": int(ppid), "pgid": int(pgid),
                "elapsed": _parse_etime(etime), "stat": stat, "args": args,
            })
        except ValueError:
            continue
    return procs


def collect_groups(procs: list[dict]) -> dict[int, list[dict]]:
    """agent 프로세스를 하나라도 포함하는 PGID 그룹만 반환."""
    by_pgid: dict[int, list[dict]] = {}
    for p in procs:
        by_pgid.setdefault(p["pgid"], []).append(p)
    agent_pgids = {p["pgid"] for p in procs if _is_agent(p["args"])}
    return {pg: sorted(by_pgid[pg], key=lambda x: x["pid"])
            for pg in sorted(agent_pgids)}


def _flags(p: dict, long_threshold: int) -> list[str]:
    f = []
    if "Z" in p["stat"]:
        f.append("defunct")
    # agent 그룹 안에서 ppid==1 은 띄운 부모(파이썬)가 사라졌다는 뜻 → 잔여.
    if p["ppid"] == 1:
        f.append("residual")
    if p["elapsed"] >= long_threshold:
        f.append("long")
    return f


def render(groups: dict[int, list[dict]], long_threshold: int) -> str:
    if not groups:
        return "agent CLI 프로세스 그룹 없음 (실행 중인 claude/codex/copilot 없음)."
    lines = []
    for pgid, members in groups.items():
        leader = next((m for m in members if m["pid"] == pgid), members[0])
        gflags = sorted({fl for m in members for fl in _flags(m, long_threshold)})
        tag = f"  [{', '.join(gflags)}]" if gflags else ""
        lines.append(f"\nPGID {pgid}  ({len(members)} proc, "
                     f"leader {leader['args'].split()[0].rsplit('/', 1)[-1]}, "
                     f"{leader['elapsed']}s){tag}")
        lines.append(f"  {'PID':>7} {'PPID':>7} {'ELAPSED':>8} {'STAT':<5} COMMAND")
        for m in members:
            mf = _flags(m, long_threshold)
            mark = f"  «{','.join(mf)}»" if mf else ""
            cmd = m["args"] if len(m["args"]) <= 90 else m["args"][:87] + "..."
            lines.append(f"  {m['pid']:>7} {m['ppid']:>7} {m['elapsed']:>7}s "
                         f"{m['stat']:<5} {cmd}{mark}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--older-than", type=int, default=0, metavar="SEC",
                    help="이 초 이상 산 그룹만 표시")
    ap.add_argument("--long", type=int, default=600, metavar="SEC",
                    help="[long] 플래그 임계값 (기본 600s)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--kill", type=int, metavar="PGID",
                    help="해당 PGID 그룹 전체를 SIGKILL (좀비/행 정리)")
    args = ap.parse_args(argv)

    if os.name != "posix":
        print("이 스크립트는 POSIX(macOS/Linux) 전용입니다.", file=sys.stderr)
        return 2

    if args.kill is not None:
        try:
            os.killpg(args.kill, signal.SIGKILL)
            print(f"PGID {args.kill} 그룹 SIGKILL 전송.")
            return 0
        except ProcessLookupError:
            print(f"PGID {args.kill} 그룹 없음 (이미 종료).", file=sys.stderr)
            return 1
        except PermissionError:
            print(f"PGID {args.kill} 권한 없음.", file=sys.stderr)
            return 1

    groups = collect_groups(_snapshot())
    if args.older_than:
        groups = {pg: ms for pg, ms in groups.items()
                  if max(m["elapsed"] for m in ms) >= args.older_than}

    if args.json:
        print(json.dumps({str(pg): ms for pg, ms in groups.items()},
                         ensure_ascii=False, indent=2))
    else:
        print(render(groups, args.long))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
