# agentcli Convention Rules (Reviewer A 검사 6축 보강)

글로벌 core의 Reviewer A는 "repo rule 파일을 일반적으로 점검"하는 컨벤션 축을
가진다. agentcli에서는 그 축에 다음 룰들을 명시적으로 추가한다. Reviewer A가
컨벤션 축을 평가할 때 이 목록을 체크리스트로 사용한다.

## 1. 3-프로바이더 정규화 계약 (`CLAUDE.md`)

agentcli는 `claude`, `codex`, `copilot` 세 외부 CLI를 단일 API로 normalize한다.
한 프로바이더만 고치고 다른 둘에 동일 변경이 필요한데 빠뜨리면 계약 위반이다.

- `ClaudeProvider` / `CodexProvider` / `CopilotProvider` 시그니처·세션 플래그·
  스트리밍 chunk 타입·permission 플래그가 동일한지 확인
- 정규화된 chunk 타입: `text` · `thinking` · `tool_use` · `tool_result` · `event`
  · `error` · `done` — 신규 chunk 추가 시 세 provider 모두 갱신 필요
- README 비교표(provider별 컬럼)도 같이 갱신됐는지

## 2. 한·영 doc 짝

모든 `.md` 문서는 `.ko.md` 짝을 가진다.

- `README.md` ↔ `README.ko.md`
- `docs/positioning.md` ↔ `docs/positioning.ko.md`
- `docs/release.md` ↔ `docs/release.ko.md`
- `docs/releases/v*.md` ↔ `docs/releases/v*.ko.md`

한쪽만 수정된 PR은 컨벤션 위반. 짝 동기화 누락이 사실상 사용자에게 잘못된 정보
노출이면 (예: 영문 README에는 새 버전 install snippet, 한국어 README는 옛 버전)
Reviewer B의 "진짜 블로커" 기준에도 해당한다.

## 3. 런타임 의존성 0개

`pyproject.toml`의 `[project.dependencies]`에 새 패키지를 추가하면 안 됨.
개발 도구는 `[project.optional-dependencies].dev`에만.

## 4. 세션 = CLI 단일 진실

라이브러리는 `session_id`만 저장하고 이전 user/assistant turn을 prompt에 재주입하지
않는다. `system_prompt` / `AgentProfile.instructions`는 instruction hash가 바뀔
때만 주입.

## 5. 용어 금지

- **"고아"** / **"orphan"**: 어떤 맥락에서도 사용 금지. 대안: "참조 없는
  (unreferenced)", "매칭되지 않는 (unmatched)", "잔여 (residual)".

## 6. 커밋 메시지 / PR 제목 / PR 본문 / 이슈 코멘트 한국어

`fix-issue`가 생성하는 텍스트의 한국어 부분은 모두 한국어로 작성. 코드 주석·
변수명·CLI 명령·외부 식별자는 영어 그대로. 영문 릴리즈 노트 `.md`만 예외.
