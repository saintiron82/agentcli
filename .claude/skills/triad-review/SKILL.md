---
name: triad-review
description: agentcli PR에 세 시각(까칠 senior · 실용주의 베테랑 · 테스트 중심)의 reviewer를 동시 호출하여 6축 분석을 PR에 코멘트로 박고, 서로의 리뷰를 상호 평가한 뒤, 통합 합의 verdict를 도출한다. 인자로 PR 번호를 받는다 (예 `/triad-review 5`). Triggers: '리뷰 시스템', 'triad review', '3계층 리뷰', 'PR 합의', '리뷰어 호출', 'reviewer panel'.
disable-model-invocation: true
---

# 3계층 리뷰 + 상호 평가 + 통합 조율 시스템

인자: `$ARGUMENTS` = GitHub PR 번호 (필수). 예: `/triad-review 5` → `saintiron82/agentcli#5`.

이 스킬은 **4 Phase**를 순서대로 수행한다:

| Phase | 내용 | 실행 | 산출물 |
|---|---|---|---|
| 1 | 3 reviewer 1차 분석 | 병렬 spawn | 각자 PR에 코멘트 + verdict 반환 |
| 2 | 상호 평가 (cross-review) | 병렬 spawn | 각자 다른 두 명의 리뷰를 평가 + 자기 verdict 변경 여부 |
| 3 | 통합 조율 | 단일 spawn 또는 main agent | 합의 verdict + 통합 코멘트 |
| 4 | 사용자 보고 | main agent | 최종 verdict, 블로커·major·minor 분리표, 다음 결정 위임 |

**철칙 (이 스킬이 만드는 모든 텍스트)**: PR 코멘트, gh pr review body, 사용자 보고 모두 **한국어**. 코드/CLI/외부 식별자는 영어 유지.

---

## Phase 0: 입력 확인

1. `gh pr view $ARGUMENTS --repo saintiron82/agentcli --json number,title,headRefName,baseRefName,url,state` — PR 메타데이터 로드. state가 `OPEN`이 아니면 사용자에게 알리고 중단.
2. `gh pr diff $ARGUMENTS --repo saintiron82/agentcli` — 변경 diff 미리 한 번 캐시 (subagent들이 다시 부를 것).
3. `cat /Users/saintiron/Projects/agentcli/CLAUDE.md` — 하드 제약 캐시 (subagent들에게 페르소나 컨텍스트로 전달).
4. TaskCreate 4개: phase1-3-reviewers / phase2-cross-review / phase3-integration / phase4-report.

---

## Phase 1: 3 reviewer 1차 분석 (병렬)

**한 메시지에 3개 Agent 호출을 동시에 발행**한다. 직렬 spawn 금지.

### Reviewer A: 까칠 senior staff engineer

- **페르소나**: "매우 까칠하고 비판적이다. 본 PR을 절대 호의적으로 보지 않는다. 결함을 찾아내려 노력한다."
- **검사 6축**: 보안 / 성능 / 아키텍처(3-프로바이더 정규화 계약 위반 여부 포함) / 컨벤션(한·영 doc 짝, 커밋·PR 한국어, '고아/orphan' 금지 용어) / 복잡도(죽은 코드, 의미 함정) / 에러 핸들링·테스트 커버리지
- **출력 형식**:
  - 발견 사항 표 (확실도 high/medium/low × 심각도 blocker/major/minor/nit)
  - 분석 요약 (한국어 단락)
  - 마지막 줄: `VERDICT: approve|comment|request-changes`
- **PR 첨부**: `gh pr review $ARGUMENTS --repo saintiron82/agentcli --<verdict-flag> --body "## 까칠 senior reviewer 의견\n\n<발견사항 한국어 본문>\n\nVERDICT: <...>"`

### Reviewer B: 실용주의 베테랑

- **페르소나**: "15년 차 운영·전달 중심 베테랑. 헐렁하지 않다. 보안·데이터 무결성·명시 약속 위반은 단호하게 잡는다. 그러나 오버스펙·미래의 가설적 위반·미적 정합·인접 청소 욕구는 거부한다. 머지가 가능한지, 사용자 통증을 정말 해결하는지, 운영 중인 사용자에게 catastrophic 회귀를 일으키는지를 본다."
- **판단 기준**:
  - **진짜 블로커**: 보안 결함 / 데이터 무결성 / catastrophic 회귀 / 릴리즈 노트가 거짓 약속 / 사용자에게 직접 잘못된 정보 노출 (한·영 짝 위반 포함 검토)
  - **머지 후 follow-up OK**: 죽은 코드 경로 / 추상 계약과 실 동작의 의미 불일치(인터페이스 동일하면) / 누락된 테스트 케이스 / 코드 코멘트 부족
  - **오버스펙 거부**: "더 깔끔하게" / 가설 / 미래 잘못 사용 / 범위 밖 인접 정리
- **출력 형식**: 동일 (표 + 요약 + 마지막 줄 verdict)
- **PR 첨부**: `gh pr review` 같은 방식. 본문 첫 줄 `## 실용주의 베테랑 reviewer 의견`.

### Reviewer C: 테스트 중심

- **페르소나**: "테스트가 코드의 실제 invariant를 결정적으로 잠그는가, 누락된 케이스가 있는가, 회귀 위험은 어디 있는가 하나에 집중한다. 코드 스타일·아키텍처 미적 정합·문서 짝은 보지 않는다."
- **검사 항목**:
  1. 신규 회귀 테스트가 진짜 invariant를 잠그는가, 아니면 surface 수준 cmd 단언인가
  2. 갱신된 기존 테스트가 충분한가
  3. 누락된 회귀 케이스 (sync/async/stream 경로 균일성, 통합 테스트, fallback 경로, 동시성)
  4. 기존 skipped 테스트의 정체와 이 PR과의 관련성
  5. TDD 게이트 진정성 (fix 전 실패가 보장되는 형태인가, 사실상 fix 후 거울 단언인가)
- **출력 형식**: 동일
- **PR 첨부**: `gh pr review` 같은 방식. 본문 첫 줄 `## 테스트 중심 reviewer 의견`.

### Phase 1 결과 수집

세 subagent의 응답을 그대로 받는다. 각 응답의 마지막 줄에서 `VERDICT:` 추출. 발견 사항 표를 (reviewer, 발견 #, 위치, 확실도, 심각도, 발견 본문) 형태로 한 표에 통합. 코멘트가 PR에 실제 첨부됐는지 `gh pr view $ARGUMENTS --comments` 로 확인.

TaskUpdate: phase1-3-reviewers = completed (verdict 3개 메타데이터로 기록).

---

## Phase 2: 상호 평가 (Cross-Review, 병렬)

세 reviewer가 다른 두 명의 리뷰를 정독하고 평가한다. **한 메시지에 3개 Agent 호출 동시 발행**.

각 cross-review subagent에게 전달할 입력:
- 자기 페르소나 (Phase 1과 동일)
- 자기의 Phase 1 발견 사항 표 + verdict (참고)
- 다른 두 reviewer의 코멘트 본문 (Phase 1 결과에서 추출)
- 평가 임무:
  1. 다른 두 reviewer의 발견 사항을 항목별로 **동의 / 부분 동의 / 반박 / 보완** 분류
  2. 자기 시각에서 그 발견의 심각도가 다르다고 보면 재분류 사유 명시
  3. 자기 verdict가 다른 두 명의 리뷰를 본 뒤 **변경**되는가? (예: 까칠이 major로 본 것을 실용주의가 follow-up OK로 재분류하면 까칠도 verdict를 comment로 누그러뜨릴 수 있음 — 단, 반박할 근거가 더 강하면 유지)
- 출력 형식:
  - 평가 표 (대상 reviewer / 발견 # / 자기 의견 / 자기 시각 등급 / 자기 verdict 변경 여부)
  - 한국어 평가 요약 단락
  - 마지막 줄: `REVISED_VERDICT: approve|comment|request-changes` (Phase 1 verdict와 같을 수 있음)

### Phase 2 결과 수집

각자의 REVISED_VERDICT와 평가 표를 모은다. **이 단계에서는 PR 코멘트 추가 첨부 없음** — Phase 3에서 통합 코멘트 한 번만 박는다.

TaskUpdate: phase2-cross-review = completed.

---

## Phase 3: 통합 조율

main agent가 직접 통합한다 (별도 subagent 불필요 — 컨텍스트가 이미 main에 있음).

### 합의 verdict 규칙 (REVISED_VERDICT 3개를 받아)

다음 우선순위로 결정:

1. REVISED_VERDICT 중 **`request-changes`가 1개 이상이면** → 합의 `request-changes`. 단, 다른 둘이 같은 발견을 follow-up OK로 분류했으면 본문에 그 사실 명시.
2. 셋 다 `approve`면 → 합의 `approve`.
3. 그 외 (`comment` 섞임) → 합의 `comment`.

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

---

## 절대 하지 말 것

- 3 reviewer를 직렬로 spawn (반드시 한 메시지에 3개 동시 발행 — Phase 1, Phase 2 각각)
- subagent 응답을 사용자에게 그대로 토하지 않기 — Phase 4에서 통합 보고로 정제할 것
- 합의 verdict가 `request-changes`인데 사용자 확인 없이 머지 진행 (이건 호출자 스킬, 예: `fix-issue`의 책임)
- 영문으로 코멘트 작성
- 같은 발견을 reviewer마다 중복 카운트 (Phase 3에서 dedupe할 것)
- Phase 2에서 PR에 추가 코멘트 첨부 (Phase 3 통합 코멘트로 한 번만)

## 호출자 스킬과의 연동

이 스킬은 단독 `/triad-review <PR번호>` 로도 사용할 수 있고, `fix-issue` 같은 상위 파이프라인의 한 단계로도 호출된다. 호출자는 합의 verdict와 블로커 목록만 보고 다음 단계(머지/수정/보류)를 결정한다. 이 스킬은 결정을 대신 하지 않는다.
