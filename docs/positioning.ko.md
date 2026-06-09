# 프로젝트 포지셔닝

[English](positioning.md) | [한국어](positioning.ko.md)

## 한 문장 정의

`agentcli`는 사용자의 컴퓨터에 설치된 Claude Code, Codex, GitHub Copilot CLI를 Python 앱의 세션형 AI 백엔드로 붙이기 위한 클라이언트 레이어입니다.

## 왜 필요한가

agentic coding CLI는 실제 런타임 도구로 쓸 만큼 유용해졌지만, 처음부터 Python SDK처럼 앱에 임베드하라고 설계된 것은 아닙니다. 각 CLI는 세션 flag, 출력 이벤트, resume 방식, 권한 옵션, 모델 selector, 인증 실패 메시지가 다릅니다.

대부분의 앱은 처음에는 `subprocess.run(...)`으로 하나의 CLI를 호출합니다. 하지만 provider가 늘고, 장기 세션이 필요하고, streaming, health check, usage accounting, fallback, instruction freshness가 필요해지는 순간 매번 같은 glue code가 반복됩니다.

`agentcli`는 그 application-level client layer를 제공합니다. 대신 provider의 네이티브 세션 히스토리는 가져오거나 복제하지 않습니다.

## 핵심 경계

provider CLI가 소유하는 것:

- 로그인과 인증 정보
- 네이티브 tool loop
- 네이티브 세션 히스토리
- provider별 설정과 정책

`agentcli`가 소유하는 것:

- 안정적인 Python client API
- `owner + alias + cwd` 기반 앱 레벨 identity
- provider session handle
- 사용량과 latency row
- instruction hash freshness
- health check 정규화
- streaming chunk/error 정규화
- 명시적 provider/model/fallback 정책

## 설계 불변식

이 네 가지 제약은 타협 대상이 아닙니다. 단순한 선호가 아니라 코드 레벨에서
프로젝트가 추구하는 바이며, 모든 변경은 이 불변식을 지키는 것을 전제로 합니다.

- **런타임 의존성 0.** `[project.dependencies]`는 비워 두고, 개발 도구는
  `[project.optional-dependencies].dev`에만 둡니다. 그래야 어떤 호스트 앱에도
  안전하게 임베드할 수 있습니다.
- **CLI 세션이 히스토리의 단일 진실 소스.** 라이브러리는 provider별
  `session_id`만 저장하고 이전 턴을 프롬프트에 다시 주입하지 않습니다.
  `system_prompt` / `AgentProfile.instructions`는 instruction hash가 바뀔 때만
  전송됩니다. 이 덕분에 레이어가 가볍고 토큰 사용량이 예측 가능합니다.
- **세 provider는 정규화 상태를 유지.** `ClaudeProvider`, `CodexProvider`,
  `CopilotProvider`는 세션 flag, streaming chunk 타입, 권한 flag에 대해 하나의
  통일된 계약을 노출합니다. 이 동등성(parity)을 깨는 provider 변경은 회귀입니다.
- **한/영 문서는 짝을 이룸.** 모든 `.md`에는 대응하는 `.ko.md`가 있으며, 한쪽
  변경은 다른 쪽에도 반영합니다.

## 누가 쓰면 좋은가

다음과 같은 것을 만들 때 적합합니다.

- AI coding/review/research 백엔드가 필요한 데스크톱 또는 웹 앱
- 각 역할이 자기 native CLI 세션을 유지해야 하는 multi-agent workflow
- provider switching을 원하지만 provider별 CLI glue code를 다시 만들고 싶지 않은 자동화 도구
- raw transcript 저장보다 usage log와 session routing이 필요한 제품

## 누가 쓰지 않는 게 좋은가

다음이 필요하다면 `agentcli`가 목적에 맞지 않습니다.

- OpenAI/Anthropic/GitHub hosted API용 일반 SDK
- 자체 planner와 tool loop를 가진 완성형 agent framework
- 여러 도구의 history를 캡처하고 동기화하는 session-sync 제품
- 음성, 로컬 모델, 터미널 UI를 제공하는 최종 사용자용 CLI 앱

## 주변 프로젝트와의 차이

일부 공개 패키지는 command construction, subprocess lifecycle, provider facts, transcript discovery 같은 low-level CLI runtime contract에 집중합니다. `agentcli`는 그보다 한 단계 위에 있습니다. 호스트 앱이 `owner + alias + cwd`로 이름 있는 agent를 유지하고, session handle과 운영 메타데이터만 저장하도록 설계되었습니다.

다른 패키지들은 session capture, context transfer, end-user CLI app에 가깝습니다. `agentcli`는 그 범주가 아니라, 이미 설치된 provider CLI를 앱 내부 기능으로 붙이려는 개발자를 위한 embedding SDK입니다.

## 릴리즈 기준

현재 프로젝트는 public beta 수준입니다. 패키지 메타데이터, 테스트, typing marker, 예제, changelog, release note가 있고 개발자 통합과 피드백에는 적합합니다. 다만 provider CLI들이 빠르게 변하기 때문에 1.0 안정성을 약속하는 단계는 아닙니다.
