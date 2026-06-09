"""05 — merge-gate + repairman 스킬 기반 간단 리뷰 시스템.

agentcli 임베딩 데모: Claude Code CLI 가 cwd/사용자 레벨에서 네이티브로
로드하는 Agent Skills 를 그대로 활용해 2-에이전트 리뷰 파이프라인을 만든다.

  1. mega-reviewer   — merge-gate(mega) 스킬로 대상 변경을 리뷰 (read-only)
  2. repairman-planner — Critical/Important 발견 시, 리뷰 결과를
     inject_context(호스트 주입 모드)로 받아 repairman 실행 카드 초안 작성

요구사항:
  - claude CLI 설치 + 로그인
  - ~/.claude/skills/merge-gate, ~/.claude/skills/repairman 설치
  - 대상 repo 에 repairman.adapter.yaml 이 있으면 planner 가 사용

사용:
  python examples/05_review_system.py                       # 현재 브랜치 vs trunk
  python examples/05_review_system.py --target "main...HEAD"
  python examples/05_review_system.py --target "uncommitted working-tree changes" --fresh
  python examples/05_review_system.py --model haiku --no-repair

종료 코드: Critical 발견 시 1 (CI/pre-merge 게이트로 사용 가능), 아니면 0.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

from agentcli import LLMClient, MemoryStore, ProviderRegistry
from agentcli.providers.claude import ClaudeProvider
from agentcli.types import Message

OWNER = "review-system"

REVIEW_PROMPT = """\
Use the merge-gate skill (mega) to review: {target}.

Follow the skill's phases and output format. This is a READ-ONLY review —
do not modify any files, do not write tracker records (Phase F is skipped
unless the operator asks). Produce the final report as self-contained
markdown, including the Stage Validation Matrix and severity-classified
findings with file:line evidence."""

REPAIR_PROMPT = """\
Use the repairman skill. The context block above contains a merge-gate
review report for this repository. For the Critical and Important findings
only, draft RePairMan Execution Card(s) following the skill's templates.
Use repairman.adapter.yaml at the repo root if present.

PLAN ONLY: do not edit any files and do not create tracker issues — output
the card drafts as markdown (a "Pending RePairMan Package" set) so the
operator can record them in the tracker."""

# merge-gate 출력에서 심각도를 잡는다: "**Severity**: **Important**" 라벨과
# Stage Validation Matrix 테이블 행("| Important |") 두 형식 모두.
SEVERITY_RE = re.compile(
    r"(?:severity\W{0,8}|\|\s*)(critical|important)\b",
    re.IGNORECASE)


def make_client() -> tuple[LLMClient, MemoryStore]:
    store = MemoryStore()
    registry = ProviderRegistry()
    # 리뷰/계획 전용 — 파일 수정 도구는 차단한다.
    registry.register(ClaudeProvider(
        permission_mode="bypassPermissions",
        disallowed_tools=["Edit", "Write", "NotebookEdit"],
    ))
    registry.set_fallback_order(["claude"])
    return LLMClient(store=store, registry=registry), store


async def run_stage(client: LLMClient, *, alias: str, prompt: str,
                    cwd: str, model: str, fresh: bool = False,
                    inject: list[dict] | None = None) -> str:
    print(f"\n════ {alias} ════", flush=True)
    async for chunk in client.chat_stream(
            prompt, provider="claude", model=model,
            owner=OWNER, alias=alias, cwd=cwd,
            new_session=fresh, inject_context=inject,
            idle_timeout=300, wall_timeout=1800):
        if chunk.type == "text":
            print(chunk.content, end="", flush=True)
        elif chunk.type == "tool_use":
            name = (chunk.data or {}).get("name", "tool")
            print(f"\n  [{alias} → {name}]", flush=True)
        elif chunk.type == "error":
            print(f"\n[{alias} error] {chunk.content}", file=sys.stderr)
        elif chunk.type == "done":
            print()
            return chunk.content
    return ""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="리뷰 대상 repo 경로")
    ap.add_argument("--target", default="the current branch against the "
                    "repository's detected trunk",
                    help="merge-gate 리뷰 대상 (브랜치/범위/PR/설명)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--fresh", action="store_true",
                    help="이전 리뷰 세션을 잇지 않고 새 세션으로 시작")
    ap.add_argument("--no-repair", action="store_true",
                    help="repairman 계획 단계 생략")
    ap.add_argument("--out", default="review-report.md")
    args = ap.parse_args()
    repo = str(Path(args.repo).resolve())

    client, store = make_client()

    health = client.health_check("claude")
    if not health.ok:
        print(health.suggested_action or health.message, file=sys.stderr)
        return 2

    # ── 1단계: merge-gate 리뷰 (CLI 네이티브 세션 모드) ──
    review = await run_stage(
        client, alias="mega-reviewer",
        prompt=REVIEW_PROMPT.format(target=args.target),
        cwd=repo, model=args.model, fresh=args.fresh)
    if not review:
        print("리뷰 단계가 결과를 내지 못했습니다.", file=sys.stderr)
        return 2

    severities = sorted({m.group(1).title()
                         for m in SEVERITY_RE.finditer(review)})
    has_critical = "Critical" in severities
    needs_repair = bool(severities)

    # ── 2단계: repairman 실행 카드 초안 (호스트 주입 모드) ──
    repair = ""
    if needs_repair and not args.no_repair:
        ctx = store.create(OWNER, "claude")
        store.add_message(ctx.id, Message(
            role="user",
            content=f"merge-gate review report:\n\n{review}",
            timestamp=datetime.now(), agent="mega-reviewer"))
        repair = await run_stage(
            client, alias="repairman-planner", prompt=REPAIR_PROMPT,
            cwd=repo, model=args.model,
            inject=[{"conversation_id": ctx.id, "limit": 5}])

    # ── 리포트 저장 + 요약 ──
    out = Path(args.out)
    report = [f"# Review report — {datetime.now():%Y-%m-%d %H:%M}",
              f"\nTarget: {args.target}\n",
              "\n## merge-gate\n", review]
    if repair:
        report += ["\n## repairman execution cards (draft)\n", repair]
    out.write_text("\n".join(report), encoding="utf-8")

    stats = client.get_token_stats(owner=OWNER)
    print(f"\n════ summary ════")
    print(f"severities found : {', '.join(severities) or 'none'}")
    print(f"report           : {out}")
    print(f"tokens           : {stats['total_tokens']} "
          f"({stats['total_calls']} calls)")
    if has_critical:
        print("verdict          : ❌ Critical findings — merge blocked")
        return 1
    print("verdict          : ✅ no Critical findings")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
