# fix-issue Stages (상세 절차)

SKILL.md는 단계 인덱스 + 철칙 + 절대 하지 말 것만 유지하고, 각 단계의 구체적
명령·게이트·산출물은 이 파일에 둔다. 실제 실행 시 SKILL.md → 이 파일 순으로 읽는다.

---

## Stage 0: 준비 + 브랜치 격리

1. `gh issue view $ARGUMENTS --repo saintiron82/agentcli` 로 이슈 본문 + 코멘트 전체 로드.
2. 이슈에서 다음을 추출:
   - **환경**: OS, Python 버전, agentcli 버전, 관련 외부 CLI(`claude`/`codex`/`copilot`) 버전
   - **재현 시나리오**: 코드 스니펫, 입력 크기, 호출 옵션
   - **증상**: 에러 메시지, 타임아웃, hang, 잘못된 출력
   - **이미 시도한 우회/패치** (있다면 — 그것이 실패한 이유가 진짜 원인의 단서)
   - **사용자가 제안한 옵션** (Option A/B/C 등) — 채택 여부는 보류, 일단 후보로 기록
3. **워킹트리 상태 검증** (실패 시 즉시 중단):
   - `git status --porcelain`가 비어 있어야 한다. 더러우면 사용자에게 `git stash` 또는 정리 후 다시 호출하라고 알리고 멈춰라.
   - `git rev-parse --abbrev-ref HEAD`가 `main` 또는 다른 보호 브랜치여야 한다. 이미 `fix/*` 브랜치 위라면 사용자에게 알리고 그 브랜치에서 이어갈지/새로 만들지 확인.
4. **독립 브랜치 생성 (HARD RULE — Stage 1부터 4의 PR까지 모든 변경은 이 브랜치에서만)**:
   ```bash
   git checkout main
   git pull --ff-only origin main
   git checkout -b fix/issue-${ARGUMENTS}-<short-slug>
   ```
   - 브랜치 이름: `fix/issue-<번호>-<3~5단어-kebab-slug>` (예: `fix/issue-4-claude-p-session-deadlock`)
   - 이 브랜치를 벗어나서 어떤 파일도 만들거나 고치지 말 것.
5. TaskCreate로 6개 태스크 등록: reproduce / root-cause / fix / release-prep / adversarial-review / merge-and-tag.

---

## Stage 1: 재현 (Reproduce)

**목표**: "이 명령을 돌리면 100% 실패한다"는 최소 재현을 손에 쥐는 것.

1. 이슈의 시나리오를 그대로 따라가는 **단일 실행 스크립트 또는 pytest 테스트**를 작성하라. 위치:
   - 정식 회귀 테스트로 남길 만한 것 → `tests/test_issue_${ARGUMENTS}.py` (Stage 3에서 실패 → 통과로 전환됨)
   - 임시 디버깅용 → `/tmp/repro_${ARGUMENTS}.py` (커밋하지 않음)
2. **재현 실패 처리**:
   - 호스트 OS가 이슈 OS와 다르면 (예: 이슈는 Windows, 호스트는 macOS) — 가능한 만큼 OS-독립 부분을 재현하고, OS-의존 부분은 코드 추적 + 로직 시뮬레이션으로 대체. 그리고 그 사실을 명시.
   - 외부 CLI가 미설치면 — `client.health_check()` 분기에서 막혀 재현 불가. 사용자에게 알리고 멈춰라. 짐작으로 진행하지 말 것.
3. **재현 성공 게이트**: 다음을 모두 충족해야 Stage 2 진입:
   - [ ] 스크립트/테스트를 돌리면 이슈에 기술된 증상이 나타난다 (또는 OS-제한 명시)
   - [ ] 증상이 결정적이다 (3번 돌려도 같은 결과 — flaky면 더 좁혀라)
   - [ ] stderr/로그/타임스탬프를 캡처해 두었다
4. TaskUpdate: reproduce = completed.

---

## Stage 2: 원인 파악 (Root Cause)

**목표**: "어느 파일 어느 줄에서, 어떤 조건이 만나면 이 증상이 발생한다"를 한 문장으로 쓸 수 있는 상태.

1. 코드 추적:
   - 사용자 호출 진입점부터 시작 (`LLMClient.chat`/`chat_async`/`chat_stream` → 해당 provider).
   - **3-프로바이더 정규화 계약**(`@CLAUDE.md` 참고)을 위반하지 않는지도 동시에 확인 — 한쪽만 고쳐 다른 프로바이더와 어긋나면 안 됨.
   - SQLiteStore / MemoryStore 메타데이터 키(`session_id:<provider>`, instruction hash)가 어떻게 흐르는지 추적.
2. 가설을 **확률 순위**로 적어라. 최소 2개. 예:
   - 가설 1 (80%): stale `session_id`가 `--resume` 인자로 전달되어 데드락
   - 가설 2 (15%): stdin/stdout 버퍼링 문제
   - 가설 3 (5%): provider별 timeout 누수
3. 가설별로 **검증 실험**을 설계 → 실행. 로그·디버거·바이너리 검색(`grep`)으로 가설을 깎거나 확정.
4. 확정된 원인을 한 문단으로 정리:
   - **어디서**: 파일:줄
   - **어떤 조건**: 입력/상태 조합
   - **왜 그런 증상이**: 인과 사슬
   - **왜 기존 우회 패치가 안 통했는지** (Stage 0에서 수집한 것)
5. **원인 확정 게이트**: 다음을 충족해야 Stage 3 진입:
   - [ ] 원인 위치가 파일:줄로 특정됨
   - [ ] Stage 1의 재현 케이스가 이 원인으로 100% 설명됨
   - [ ] 3-프로바이더 계약을 깨지 않고 고칠 수 있는 경로가 보임 (또는 깬다면 그 이유가 명확함)
6. TaskUpdate: root-cause = completed. 원인 요약을 다음 단계로 전달.

---

## Stage 3: 수정 (Fix)

**목표**: 재현 케이스가 통과하고, 회귀 테스트가 추가되고, 전체 테스트가 깨지지 않은 상태.

1. **회귀 테스트 먼저** (TDD). `tests/test_issue_${ARGUMENTS}.py`에 이슈의 실패 케이스를 pytest로 박는다. 지금은 실패해야 정상.
2. 수정 구현:
   - **하드 제약 준수** (`@CLAUDE.md`):
     - 런타임 의존성 0개 유지 — `pyproject.toml` `[project.dependencies]` 건드리지 말 것
     - 3-프로바이더 정규화 계약 유지 — claude만 고치면 codex/copilot에도 동일 변경 필요한지 검토
     - 세션 = CLI 단일 진실 — 라이브러리가 이전 turn을 prompt에 재주입하지 말 것
   - **최소 변경 원칙** — 이슈가 요구하는 동작 변경만. 주변 리팩토링 금지.
   - 새 동작이 기존 사용자에게 깨짐을 일으키면 (behavior break) 그 사실을 CHANGELOG에 명시할 준비.
3. 검증:
   - `pytest tests/test_issue_${ARGUMENTS}.py` → **통과**해야 함
   - `pytest` 전체 → 다른 테스트가 깨지지 않아야 함
   - 깨진 것이 있다면: 진짜 회귀인지 / 의도된 동작 변경에 따른 기존 테스트 갱신 필요인지 판단
4. **수정 완료 게이트**:
   - [ ] 새 회귀 테스트 통과
   - [ ] 전체 테스트 통과 (또는 갱신된 기존 테스트 포함)
   - [ ] 변경된 파일이 3-프로바이더 계약을 깨지 않음을 한 줄 요약
   - [ ] 이슈 본문에 적힌 모든 증상이 해소됨 (재현 스크립트 재실행으로 확인)
5. TaskUpdate: fix = completed.

---

## Stage 4: 배포 준비 (Release Prep — 여전히 fix 브랜치 위)

**목표**: 태그 + 릴리즈 노트가 준비된 PR이 main을 향해 열린 상태. `merge`/`git tag`/`push`/`twine upload`는 **Stage 5에서만, 사용자 확인 후**.

1. **버전 결정**:
   - 버그 수정 only → patch bump (예: 0.4.2 → 0.4.3)
   - 동작 변경 있음 (behavior break) → minor bump (예: 0.4.2 → 0.5.0)
   - 사용자에게 한 번 확인하라 (AskUserQuestion).
2. **파일 갱신** (한 PR/커밋에 묶을 것 — 모두 fix 브랜치 위에서):
   - `pyproject.toml` `[project].version` ← NEW_VERSION
   - `CHANGELOG.md` — 새 섹션 `## NEW_VERSION` 추가. 이슈 번호 링크 포함.
   - `README.md` — install snippet의 `@v<OLD>` → `@v<NEW>`
   - `README.ko.md` — 동일 변경 (한·영 문서 짝 유지 규칙)
   - `docs/releases/v<NEW>.md` — 릴리즈 노트 (헤드라인 / Highlights / Bug fixes / Upgrade notes)
   - `docs/releases/v<NEW>.ko.md` — 동일 번역
3. **`/release-check` 호출**하여 다음을 검증 (이때 브랜치는 여전히 fix/*):
   - `pytest` 전체 통과
   - `python -m build` 성공
   - `python -m twine check dist/*` 통과
   - 한·영 문서 짝 + CHANGELOG 항목 존재
4. **커밋 + 푸시 + PR** (모두 fix 브랜치 위 — main에 직접 커밋 금지). 모든 텍스트는 **한국어** (철칙 2):
   - 커밋 메시지 형식: `fix(<scope>): <한 줄 요약> (#${ARGUMENTS})` — 한 줄 요약과 본문 모두 한국어
   - `git push -u origin fix/issue-${ARGUMENTS}-<slug>`
   - `gh pr create --base main --head fix/issue-${ARGUMENTS}-<slug>` 로 PR 생성
   - PR 제목: `이슈 #${ARGUMENTS} 수정: <한국어 한 줄 요약>`
   - PR 본문 (한국어): `Fixes #${ARGUMENTS}` 자동 close 트리거, 원인 요약(Stage 2), 변경 요약(Stage 3), 업그레이드 노트
5. TaskUpdate: release-prep = completed. PR URL을 사용자에게 보고하고 **Stage 4.5 (적대적 리뷰)** 진입.

---

## Stage 4.5: 3계층 리뷰 + 통합 조율 (Triad Review)

**목표**: 막 생성된 PR을 세 시각(까칠 senior / 실용주의 베테랑 / 테스트 중심)의 reviewer가 분석 → 상호 평가 → 통합 합의 verdict를 도출. 합의가 `request-changes`면 Stage 5 진입 금지.

1. **`/triad-review <PR번호>` 호출** — 별도 스킬 (`@.claude/skills/triad-review/SKILL.md`).
   - Phase 1: 세 reviewer 병렬 spawn → 각자 PR에 코멘트
   - Phase 2: 상호 평가 (cross-review) — 각자 다른 두 명의 리뷰를 동의/반박/보완으로 재평가
   - Phase 3: 통합 조율 → 합의 verdict + 발견 사항 통합 분류 (블로커 / major-merge-then-followup / minor / 오버스펙)
   - Phase 4: PR에 통합 합의 코멘트 한 번 첨부 + 사용자 보고
2. **합의 verdict 처리**:
   - `approve` → Stage 5 게이트 통과
   - `comment` → 사용자에게 합의된 major-merge-then-followup 항목 전달, Stage 5 진입 여부 명시 확인 (AskUserQuestion). 사용자가 "follow-up issue 만들고 머지" 선택하면 `gh issue create` 로 각 항목별 issue 생성 후 Stage 5.
   - `request-changes` → **Stage 5 차단**. 사용자에게 합의된 블로커 목록 전달, 후속 커밋 추가 (Stage 3로 되돌아가 수정) 또는 명시적 무시 지시 후에만 Stage 5 진입.
3. **사용자 무시 지시**: 사용자가 "이번엔 무시하고 진행" 이라고 명시적으로 말한 경우, 그 결정과 사유를 PR 코멘트로 추가 기록 (`gh pr comment <PR번호> --body "..."` 한국어)한 뒤 Stage 5 진입.
4. TaskUpdate: adversarial-review = completed (합의 verdict 메타데이터 포함).

---

## Stage 5: 병합 + 태그 + 배포 (Merge & Tag & Publish)

**목표**: main에 병합되고, 태그가 푸시되고, (선택) PyPI까지 올라간 상태. **모든 단계는 사용자 확인 후 한 단계씩 진행** — 자동 실행 금지. **사전 조건**: Stage 4.5에서 verdict가 `approve`이거나, `comment`/`request-changes` 후 사용자 명시 진행 지시가 있어야 한다.

1. **병합** (사용자 확인 후):
   ```bash
   gh pr merge <PR번호> --squash --delete-branch
   ```
   - `--squash`가 기본. fast-forward를 원하면 사용자에게 한 번 더 확인.
   - 병합 후 로컬 동기화: `git checkout main && git pull --ff-only origin main`.
2. **태그** (사용자 확인 후 — main에서):
   ```bash
   git tag v<NEW_VERSION>
   git push origin v<NEW_VERSION>
   ```
   - 절대 fix 브랜치에서 태그 찍지 말 것. 반드시 병합된 main 위에서.
3. **PyPI 업로드** (사용자가 원할 때만):
   ```bash
   python -m twine upload dist/*
   ```
4. **이슈 마무리**: 사용자 확인 후 `gh issue comment ${ARGUMENTS}`로 수정된 버전과 PR 링크를 한국어로 남기고, 적절하면 `gh issue close ${ARGUMENTS}`. 자동 close 금지 — PR이 `Fixes #N`을 포함하면 squash merge 시 자동으로 닫히지만, 그것이 의도였는지 확인.
5. **정리**: 로컬 fix 브랜치 삭제 (`gh pr merge --delete-branch`가 원격은 정리하지만 로컬은 별도):
   ```bash
   git branch -d fix/issue-${ARGUMENTS}-<slug>
   ```
6. TaskUpdate: merge-and-tag = completed.
