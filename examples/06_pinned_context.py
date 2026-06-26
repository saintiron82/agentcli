"""Pinned context: 큰 입력 1회 주입 → 다양한 질의 (refine / fork).

큰 전사록을 한 번 넣고 그 위에서 여러 형태의 회의록·수정 요구를 던지는 패턴.
"잠시 있다가 다시 요청"(세션 만료/프로세스 재시작)도 자동 대응한다.

실행: python examples/06_pinned_context.py   (claude CLI 로그인 필요)
"""
from agentcli import LLMClient, MemoryStore

TRANSCRIPT = """\
[회의 전사록]
A: 다음 분기 로드맵을 정합시다. 결제 모듈 리팩토링이 최우선입니다.
B: 동의합니다. 다만 인증 마이그레이션이 블로커예요. 2주는 잡아야 합니다.
A: 그럼 결제는 그 다음으로. QA 리소스는 C가 조율하기로 하죠.
C: 네, QA는 3월 둘째 주부터 투입 가능합니다.
"""  # ... 실제로는 5만 자급 전사록


def main() -> None:
    client = LLMClient(MemoryStore())

    # 큰 전사록을 1회 pin. 빠른 모델 + (선택) lean 으로 단일 completion 최적화.
    ctx = client.pin_context(
        TRANSCRIPT, provider="claude", owner="demo", alias="meeting-2026Q1",
        model="claude-haiku-4-5",
        provider_options={"lean": True})

    # ── refine: 이어가기 (전사록은 첫 호출에만 전송, 이후엔 지시만) ──
    r1 = ctx.refine("위 전사록으로 격식 회의록(안건/결정/액션아이템)을 작성해줘.")
    print("=== 격식 회의록 ===\n", r1.content, "\n")

    r2 = ctx.refine("방금 회의록을 3줄로 더 짧게 줄여줘.")   # 앞 답을 봄
    print("=== 짧은 버전 ===\n", r2.content, "\n")

    # ── fork: 독립 변형 (서로 안 섞임, 매번 전사록 재시드) ──
    f1 = ctx.fork("액션아이템만 담당자·기한과 함께 표로 뽑아줘.", label="actions")
    print("=== 액션아이템 (독립) ===\n", f1.content, "\n")

    f2 = ctx.fork("이해관계자에게 보낼 캐주얼한 요약 한 문단.", label="casual")
    print("=== 캐주얼 요약 (독립) ===\n", f2.content, "\n")

    # ── 잠시 있다가 다시 요청 (또는 다른 프로세스) ──
    # 같은 alias + 전사록으로 재구성하면, 세션이 살아있으면 resume,
    # 죽었으면 전사록을 자동 재시드한다.
    later = client.pin_context(
        TRANSCRIPT, provider="claude", owner="demo", alias="meeting-2026Q1",
        model="claude-haiku-4-5", provider_options={"lean": True})
    print("세션 살아있나?:", later.is_alive())
    r3 = later.refine("결정사항 중 블로커가 된 항목만 다시 정리해줘.")
    print("=== 나중에 이어서 ===\n", r3.content)


if __name__ == "__main__":
    main()
