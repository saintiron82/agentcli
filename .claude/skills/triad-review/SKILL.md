---
name: triad-review
description: Use when `/triad-review <PR번호>`로 agentcli PR에 세 시각(까칠 senior · 실용주의 베테랑 · 테스트 중심)의 합의 리뷰가 필요할 때. 글로벌 triad-review core의 agentcli adapter — repo·페르소나 언어·convention axis만 override하고 phase 흐름·합의 규칙은 글로벌 core를 그대로 따른다. 인자로 PR 번호 필수.
disable-model-invocation: true
---

# Triad Review — agentcli Adapter

이 스킬은 글로벌 `triad-review` core의 **agentcli local adapter**다. Phase 흐름,
spawn 규칙, 합의 verdict 규칙, 통합 코멘트 본문 형식은 모두 글로벌 core가 정의한다.
이 adapter는 agentcli에 한정된 override만 담는다.

## 글로벌 core 위치 (먼저 읽기)

- 코어 SKILL: `~/.claude/skills/triad-review/SKILL.md`
- Phase 절차: `~/.claude/skills/triad-review/references/phases.md`
- 기본 reviewer 페르소나 (영어): `~/.claude/skills/triad-review/references/reviewers.md`

위 셋을 먼저 로드한 뒤 아래 override를 적용한다.

## agentcli Overrides

| 항목 | 글로벌 기본 | agentcli override |
|---|---|---|
| Repo | `gh repo view` 자동 감지 | `saintiron82/agentcli` 고정 |
| Reviewer 페르소나 언어 | 영어 (Strict senior / Pragmatic veteran / Test-centric) | 한국어 (까칠 senior / 실용주의 베테랑 / 테스트 중심) — [references/reviewers.md](references/reviewers.md) |
| Reviewer A convention 축 | repo rule 파일 일반 점검 | agentcli 룰 추가 — [references/agentcli-rules.md](references/agentcli-rules.md) |
| PR 코멘트 / 통합 코멘트 / 사용자 보고 | 영어 | **한국어** |
| 프로젝트 rule 파일 캐시 (Phase 0 step 4) | `CLAUDE.md` 또는 `AGENTS.md` | `/Users/saintiron/Projects/agentcli/CLAUDE.md` 명시 |
| 호출 컨텍스트 | 독립 / 임의 호출자 | 독립 호출 + `fix-issue` Stage 4.5 호출자 |

## 인자

`$ARGUMENTS` = GitHub PR 번호 (필수). 예: `/triad-review 5` → `saintiron82/agentcli#5`.

## 호출자 메모

- `fix-issue` 스킬의 Stage 4.5가 이 adapter를 호출한다. 호출자는 합의 verdict
  (`approve` / `comment` / `request-changes`)와 블로커 목록만 보고 Stage 5(머지·태그)
  진행 여부를 결정한다. 이 adapter는 글로벌 core와 마찬가지로 결정을 대신 하지 않는다.
- agentcli 외부에서 `/triad-review`를 호출하면 글로벌 core (영어, 범용)가 직접 실행된다
  — project-local인 이 adapter는 매칭되지 않는다. 의도된 동작.
