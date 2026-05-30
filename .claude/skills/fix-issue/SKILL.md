---
name: fix-issue
description: Use when `/fix-issue <번호>`로 agentcli GitHub 이슈를 격리 fix 브랜치 위에서 끝까지 처리해야 할 때 (이슈 분류부터 머지·태그까지 한 사이클). 인자로 이슈 번호 필수. Triggers include '이슈 수정', '버그 수정', '재현해서 고쳐', 'fix issue', 'reproduce and fix'.
disable-model-invocation: true
---

# agentcli 이슈 수정 → 릴리즈 파이프라인

인자: `$ARGUMENTS` = GitHub 이슈 번호 (필수). 예: `/fix-issue 4` → `saintiron82/agentcli#4`.

전체 단계별 절차는 [references/stages.md](references/stages.md). SKILL.md는
철칙·단계 인덱스·금지사항만 유지하고, 각 단계의 명령·게이트·산출물은
참조 파일에 둔다.

## 철칙

1. **브랜치 격리** — 모든 변경은 Stage 0에서 만든 `fix/issue-<번호>-<slug>` 브랜치 위에서만.
   `main`에 직접 커밋·푸시 금지. 병합은 Stage 5에서, 태그는 병합된 `main` 위에서만.
2. **언어** — 이 스킬이 만드는 모든 **커밋 메시지**, **PR 제목**, **PR 본문**,
   **이슈 코멘트**, **릴리즈 노트 한국어 파일** 의 한국어 부분은 **반드시 한국어**.
   코드 주석·변수명·CLI 명령·외부 식별자(예: `--resume`, `session_id`)는 영어 그대로.
   (영문 릴리즈 노트 `.md`는 영어 유지 — 한·영 짝 규칙 그대로.)
3. **3계층 합의 통과** — Stage 4.5에서 `/triad-review` 스킬을 호출. 합의 verdict가
   `request-changes`면 Stage 5(병합) 진입 금지. 사용자가 (a) 블로커 수정 커밋을
   추가하거나 (b) 명시적으로 무시하라고 지시한 뒤에만 Stage 5 진입.

## 단계 인덱스 (게이트 = 다음 단계 진입 조건)

| Stage | 목표 | 게이트 |
|---|---|---|
| 0 | 이슈 로드 + 워킹트리 검증 + `fix/*` 브랜치 생성 + TaskCreate × 6 | 깨끗한 트리 + 브랜치 생성 |
| 1 | 결정적 재현 스크립트/테스트 작성 | 3회 반복 동일 결과 |
| 2 | 원인을 파일:줄 단위로 특정, 3-프로바이더 계약 영향 검토 | 재현이 원인으로 100% 설명됨 |
| 3 | TDD로 회귀 테스트 추가 → 수정 → 전체 테스트 통과 | 새 테스트 + 전체 pass |
| 4 | 버전 bump · CHANGELOG · README · 릴리즈 노트 (한·영) · `/release-check` · PR 생성 | `/release-check` pass + PR open |
| 4.5 | `/triad-review <PR번호>` → 합의 verdict | `approve` 또는 사용자 명시 진행 |
| 5 | 사용자 확인 단위로: 병합 → 태그(main 위) → (선택) PyPI 업로드 → 이슈 마무리 | 사용자 확인 후 단계별 |

모든 명령·체크리스트·산출물 형식은 [references/stages.md](references/stages.md) 참조.

## 절대 하지 말 것

- `main` 브랜치에서 직접 커밋·푸시 — 모든 변경은 `fix/issue-*` 브랜치 위에서만
- 더러운 워킹트리에서 시작 — Stage 0 검증 통과 전 한 줄도 고치지 말 것
- 병합 전에 태그 찍기 — 태그는 반드시 PR이 main에 병합된 직후 main 위에서만
- **Stage 4.5 적대적 리뷰를 건너뛰고 Stage 5로 직행 — verdict가 `request-changes`인데 무시하고 머지하기**
- 커밋 메시지·PR 제목·PR 본문을 영어로 작성 (이 스킬의 규칙은 한국어. 영문 릴리즈 노트 `.md`만 예외)
- 재현 못한 채로 수정 들어가기 — "아마 여기겠지" 추측 수정 금지
- `[project].dependencies`에 새 패키지 추가
- claude만 고치고 codex/copilot 정규화 계약 깨기
- 사용자 확인 없이 `gh pr merge`, `git tag`, `git push`, `twine upload`, `gh issue close` 실행
- README.md만 고치고 README.ko.md 빼먹기 (또는 그 반대)
- 한 PR에 무관한 리팩토링 끼워 넣기
- "고아" / "orphan" 용어 사용 (`@CLAUDE.md` 글로벌 룰)

## 임시 파일 정리

스킬 종료 전 `/tmp/repro_${ARGUMENTS}.py` 등 임시 디버깅 파일을 제거하라.
회귀 테스트(`tests/test_issue_${ARGUMENTS}.py`)는 커밋에 포함된다.
