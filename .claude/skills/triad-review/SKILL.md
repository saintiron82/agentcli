---
name: triad-review
description: Use when `/triad-review <PR번호>`로 agentcli PR에 세 시각(까칠 senior · 실용주의 베테랑 · 테스트 중심)의 합의 리뷰가 필요할 때. 인자로 PR 번호 필수. Triggers include '리뷰 시스템', 'triad review', '3계층 리뷰', 'PR 합의', '리뷰어 호출', 'reviewer panel'.
disable-model-invocation: true
---

# 3계층 리뷰 + 상호 평가 + 통합 조율 시스템

인자: `$ARGUMENTS` = GitHub PR 번호 (필수). 예: `/triad-review 5` → `saintiron82/agentcli#5`.

- Phase별 명령·spawn·출력 형식: [references/phases.md](references/phases.md)
- 3 reviewer 페르소나·검사 항목·PR 첨부 방식: [references/reviewers.md](references/reviewers.md)

## Phase 인덱스

| Phase | 내용 | 실행 | 산출물 |
|---|---|---|---|
| 0 | 입력 확인 (PR 메타·diff·CLAUDE.md 캐시, TaskCreate 4건) | main agent | 캐시된 컨텍스트 |
| 1 | 3 reviewer 1차 분석 | **병렬 spawn (한 메시지 3개 동시)** | 각자 PR에 코멘트 + `VERDICT:` |
| 2 | 상호 평가 (cross-review) | **병렬 spawn (한 메시지 3개 동시)** | 각자 `REVISED_VERDICT:` |
| 3 | 통합 조율 + PR 합의 코멘트 1회 | main agent | 합의 verdict + 통합 표 |
| 4 | 사용자 보고 + 결정 위임 | main agent | 다음 단계 결정 |

## 철칙

1. **언어** — PR 코멘트, `gh pr review` body, 사용자 보고 모두 **한국어**.
   코드·CLI·외부 식별자는 영어 유지.
2. **병렬 spawn** — Phase 1, Phase 2는 반드시 한 메시지에 3개 Agent 호출을
   동시 발행. 직렬 spawn 금지.
3. **PR 코멘트는 한 번만** — Phase 1의 3 reviewer 코멘트 + Phase 3의 통합
   합의 코멘트만 PR에 첨부. Phase 2는 PR에 추가하지 않음.
4. **결정을 대신 하지 않음** — Phase 4에서 사용자에게 위임. 합의 verdict가
   `request-changes`인데도 머지/Stage 5 진행 여부는 호출자 스킬(예: `fix-issue`)이
   사용자 확인 후 판단.

## 합의 verdict 규칙 (REVISED_VERDICT 3개 → 합의)

우선순위:

1. REVISED_VERDICT 중 **`request-changes`가 1개 이상** → 합의 `request-changes`.
   단, 다른 둘이 같은 발견을 follow-up OK로 분류했으면 본문에 그 사실 명시.
2. 셋 다 `approve` → 합의 `approve`.
3. 그 외 (`comment` 섞임) → 합의 `comment`.

## 절대 하지 말 것

- 3 reviewer를 직렬로 spawn (반드시 한 메시지에 3개 동시 발행 — Phase 1, Phase 2 각각)
- subagent 응답을 사용자에게 그대로 토하지 않기 — Phase 4에서 통합 보고로 정제할 것
- 합의 verdict가 `request-changes`인데 사용자 확인 없이 머지 진행 (이건 호출자 스킬의 책임)
- 영문으로 코멘트 작성
- 같은 발견을 reviewer마다 중복 카운트 (Phase 3에서 dedupe할 것)
- Phase 2에서 PR에 추가 코멘트 첨부 (Phase 3 통합 코멘트로 한 번만)

## 호출자 스킬과의 연동

이 스킬은 단독 `/triad-review <PR번호>` 로도 사용할 수 있고, `fix-issue` 같은
상위 파이프라인의 한 단계(Stage 4.5)로도 호출된다. 호출자는 합의 verdict와
블로커 목록만 보고 다음 단계(머지/수정/보류)를 결정한다. 이 스킬은 결정을
대신 하지 않는다.
