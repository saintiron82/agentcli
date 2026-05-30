# Triad Review — Phases (상세 절차)

SKILL.md는 Phase 인덱스 + 철칙 + 합의 verdict 규칙만 유지. 각 Phase의 명령·
spawn 방식·출력 형식·PR 코멘트 본문은 이 파일에 둔다.

---

## Phase 0: 입력 확인

1. `gh pr view $ARGUMENTS --repo saintiron82/agentcli --json number,title,headRefName,baseRefName,url,state` —
   PR 메타데이터 로드. state가 `OPEN`이 아니면 사용자에게 알리고 중단.
2. `gh pr diff $ARGUMENTS --repo saintiron82/agentcli` — 변경 diff 미리 한 번 캐시
   (subagent들이 다시 부를 것).
3. `cat /Users/saintiron/Projects/agentcli/CLAUDE.md` — 하드 제약 캐시 (subagent들에게
   페르소나 컨텍스트로 전달).
4. TaskCreate 4개: phase1-3-reviewers / phase2-cross-review / phase3-integration / phase4-report.

---

## Phase 1: 3 reviewer 1차 분석 (병렬)

**한 메시지에 3개 Agent 호출을 동시에 발행**한다. 직렬 spawn 금지.

각 reviewer의 페르소나·검사 항목·출력 형식·PR 첨부 방식은 [reviewers.md](reviewers.md) 참조.

### Phase 1 결과 수집

세 subagent의 응답을 그대로 받는다. 각 응답의 마지막 줄에서 `VERDICT:` 추출.
발견 사항 표를 (reviewer, 발견 #, 위치, 확실도, 심각도, 발견 본문) 형태로
한 표에 통합. 코멘트가 PR에 실제 첨부됐는지 `gh pr view $ARGUMENTS --comments` 로 확인.

TaskUpdate: phase1-3-reviewers = completed (verdict 3개 메타데이터로 기록).

---

## Phase 2: 상호 평가 (Cross-Review, 병렬)

세 reviewer가 다른 두 명의 리뷰를 정독하고 평가한다. **한 메시지에 3개 Agent
호출 동시 발행**.

각 cross-review subagent에게 전달할 입력:

- 자기 페르소나 ([reviewers.md](reviewers.md))
- 자기의 Phase 1 발견 사항 표 + verdict (참고)
- 다른 두 reviewer의 코멘트 본문 (Phase 1 결과에서 추출)
- 평가 임무:
  1. 다른 두 reviewer의 발견 사항을 항목별로 **동의 / 부분 동의 / 반박 / 보완** 분류
  2. 자기 시각에서 그 발견의 심각도가 다르다고 보면 재분류 사유 명시
  3. 자기 verdict가 다른 두 명의 리뷰를 본 뒤 **변경**되는가? (예: 까칠이
     major로 본 것을 실용주의가 follow-up OK로 재분류하면 까칠도 verdict를
     comment로 누그러뜨릴 수 있음 — 단, 반박할 근거가 더 강하면 유지)
- 출력 형식:
  - 평가 표 (대상 reviewer / 발견 # / 자기 의견 / 자기 시각 등급 / 자기 verdict 변경 여부)
  - 한국어 평가 요약 단락
  - 마지막 줄: `REVISED_VERDICT: approve|comment|request-changes` (Phase 1 verdict와 같을 수 있음)

### Phase 2 결과 수집

각자의 REVISED_VERDICT와 평가 표를 모은다. **이 단계에서는 PR 코멘트 추가
첨부 없음** — Phase 3에서 통합 코멘트 한 번만 박는다.

TaskUpdate: phase2-cross-review = completed.

---

## Phase 3: 통합 조율

main agent가 직접 통합한다 (별도 subagent 불필요 — 컨텍스트가 이미 main에 있음).

### 발견 사항 통합 분류

Phase 1에서 모은 통합 표 + Phase 2의 재분류 의견을 합쳐 다음 표 생성:

| # | 위치 | 발견 | 처음 발견자 | 합의 등급 | 합의 사유 |
|---|---|---|---|---|---|

합의 등급 규칙:

- **블로커**: REVISED 평가에서 2명 이상이 blocker / 또는 request-changes의 직접 근거
- **major-merge-then-followup**: 한 명만 blocker이고 나머지가 follow-up OK라고 보면 머지 후 follow-up issue로 이동
- **minor**: 다수가 minor 또는 한 명만 major
- **nit / 오버스펙**: 다수가 오버스펙으로 분류

### PR에 최종 통합 코멘트 한 번 첨부

본문 형식 (한국어):

```
## 통합 리뷰 합의 (triad-review)

세 시각(까칠 senior / 실용주의 베테랑 / 테스트 중심) 리뷰 + 상호 평가 결과를 통합한 합의입니다.

### 1차 verdict
- 까칠: <verdict>
- 실용주의: <verdict>
- 테스트: <verdict>

### 상호 평가 후 REVISED verdict
- 까칠: <revised>
- 실용주의: <revised>
- 테스트: <revised>

### 합의 verdict: <CONSENSUS>

### 합의된 처리 (발견별)
[위 통합 표를 마크다운으로]

### 머지 권장 절차
- 블로커 X건 → 머지 전 수정 필요
- major-merge-then-followup Y건 → 머지 후 follow-up issue로 트래킹
- minor/nit Z건 → 무시 또는 다음 PR
```

코멘트 첨부:

- 합의 verdict가 `request-changes`면 `gh pr review $ARGUMENTS --request-changes --body "<위 본문>"`
- `approve`면 `--approve`
- `comment`면 `--comment`

TaskUpdate: phase3-integration = completed.

---

## Phase 4: 사용자 보고

main agent가 직접 사용자에게 한국어로 보고:

1. 합의 verdict 한 줄
2. 블로커 목록 (있다면)
3. major-merge-then-followup 목록 (있다면 — follow-up issue 자동 생성 여부 질문)
4. minor/nit/오버스펙 한 줄 합계
5. **사용자 결정 위임** (AskUserQuestion):
   - 블로커 있는 경우: "지금 블로커를 수정할까요 / 명시적으로 무시할까요 / 머지를 보류할까요"
   - 블로커 없는 경우: "지금 머지할까요 / follow-up issue 먼저 만들고 머지할까요 / 추가 검토 필요"

TaskUpdate: phase4-report = completed.
