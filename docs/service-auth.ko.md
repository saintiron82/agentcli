# 서비스별 독립 인증

[English](service-auth.md) | [한국어](service-auth.ko.md)

상태: `fix/security-hardening`에 구현했으며 미배포입니다. 런타임 의존성은 0개입니다.
하나의 Python 프로세스나 같은 머신의 여러 서비스에서 인증 선택을 분리합니다.
같은 OS 사용자로 실행되는 악성 코드까지 격리하는 기능은 아닙니다.

## 서비스별 client 연결

```python
import asyncio
from dataclasses import replace
from agentcli import AuthProfile, LLMClient, MemoryStore

# 각 서비스의 secret manager에서 값을 읽고 실제 토큰을 코드에 저장하지 않습니다.
auth_a = AuthProfile(
    "service-a", "/srv/private/agentcli/service-a",
    claude_oauth_token=token_a,
).prepare()
auth_b = AuthProfile(
    "service-b", "/srv/private/agentcli/service-b",
    claude_oauth_token=token_b,
).prepare()
client_a = LLMClient(MemoryStore(), auth_profile=auth_a)
client_b = LLMClient(MemoryStore(), auth_profile=auth_b)

a, b = await asyncio.gather(
    client_a.chat_async("A 서비스의 작업", provider="claude",
                        owner="service-a", alias="worker", cwd=workspace_a,
                        provider_options={"permission_mode": "plan"}),
    client_b.chat_async("B 서비스의 작업", provider="claude",
                        owner="service-b", alias="worker", cwd=workspace_b,
                        provider_options={"permission_mode": "plan"}),
)

# 같은 계정의 A 토큰을 교체합니다. B와 이미 시작한 A 요청은 기존 스냅샷을 유지합니다.
client_a.set_auth_profile(replace(auth_a, claude_oauth_token=new_token_a))
# 이후 A의 Claude 호출을 차단합니다. 인증 파일 삭제나 B 변경은 하지 않습니다.
client_a.set_auth_profile(replace(client_a.auth_profile, disabled_providers={"claude"}))
```

Windows에서도 플랫폼에 맞는 절대 경로를 사용하고 저장소 밖에 배치하십시오.
`prepare()`는 비공개 디렉터리와 비밀이 아닌 소유권 marker를 만듭니다. 토큰 저장·
로그인·공용 인증 복사·계정 전환·provider 토큰 폐기는 수행하지 않습니다.
POSIX 디렉터리는 비공개 권한(0700)이 필요하며 Windows의 비공개 ACL은 호스트가
설정해야 합니다. 라이브러리가 Windows ACL을 자동 설정하지 않습니다.

저장소를 공유한다면 서비스마다 다른 `owner`를 사용하십시오. alias 유일성은
여전히 `(owner, alias)`입니다. 연결된 대화를 다른 인증 프로필이나 프로필 없는
client로 재사용하면 거부합니다. 인증 연결이 없는 기존 네이티브 세션은 새 대화·
alias가 필요하며 자동으로 가져오지 않습니다. metadata에는 프로필 ID·경로·인증
방식만 기록하고 토큰은 기록하지 않습니다. 새 handle은 실행 전에 연결해 취소된
호출이 다른 서비스에서 가져갈 수 있는 handle을 남기지 않도록 했습니다.

## Provider 지원

| Provider | 프로필 입력 | 자식 프로세스 상태·인증 경로 |
| --- | --- | --- |
| Claude | `claude_oauth_token` 또는 `claude_api_key`; 없으면 로컬 `claude/.credentials.json` | `CLAUDE_CONFIG_DIR=<root>/claude` |
| Codex | `codex_api_key`; 없으면 로컬 `codex/auth.json` | `CODEX_HOME=<root>/codex`, 파일 인증 저장 강제 |
| Copilot | 명시적 `github_token` | `COPILOT_HOME=<root>/copilot`, `GH_CONFIG_DIR=<root>/gh` |
| Kiro·커스텀 provider | 이번 버전 미지원 | 거부하며 공용 인증으로 전환하지 않음 |

네이티브 CLI 규약은
[Claude](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts),
[공식 OpenAI 인증 문서](https://learn.chatgpt.com/docs/auth#credential-storage),
[Codex 자동화 문서](https://learn.chatgpt.com/docs/non-interactive-mode#authenticate-in-automation),
[Copilot](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)을
참고하십시오. provider에서 지원하고 사용 권한이 있는 토큰을 사용해야 합니다.
Codex 파일 인증은 서비스 폴더에 안전하게 준비하십시오. 기본 인증 파일·keychain을
빌리지 않습니다. CLI의 Codex 토큰 자동 갱신은 해당 서비스 경로 안에서 수행됩니다.
Claude 파일 인증은 access token으로 읽으며 AgentCli는 갱신하지 않습니다. Copilot은
OS keychain·gh 인증 검색 대신 명시적인 토큰이 필요합니다. standalone Copilot을
권장하며 gh wrapper는 서비스별 gh 경로에 extension 설치가 필요할 수 있습니다.
공용 머신 로그인을 전환하는 방식으로 준비하지 마십시오.

## 환경·수명 규칙

- 각 요청은 불변 프로필의 스냅샷을 사용합니다. 전역 `os.environ`·registry 토큰을
  변경하거나 공용 GitHub 토큰 캐시를 사용하지 않습니다. 같은 provider 객체를
  서로 다른 프로필의 client가 공유할 수 있습니다.
- 실행·로케일·CA·proxy 변수만 상속하고 인증·라우팅 변수 및 임의의 부모 비밀은
  상속하지 않습니다. HOME·USERPROFILE·AppData·XDG config는 서비스 root 안으로
  지정합니다. 도구 설정은 `extra_env={...}`로 명시하며 인증·라우팅·홈 경로 override는
  금지합니다. extra_env와 proxy 설정은 신뢰하는 호스트의 비공개 입력입니다.
- 프로젝트·관리자 설정·플러그인·hook·도구는 여전히 신뢰해야 하는 실행 입력입니다.
  실제 CLI 동작과 함께 검증하십시오. OS·네트워크 샌드박스가 아니므로 적대적인
  tenant는 OS 계정·컨테이너로 분리해야 합니다.
- 인증 누락·비활성화·오류는 실패 처리합니다. 프로필에 연결된 client에서 호출별
  `oauth_token`·provider 생성자 OAuth·사용량 조회의 별도 인증 경로를 덮어쓰면
  거부합니다. fallback도 같은 서비스 프로필의 인증만 사용하며 실행 정책을 유지해야 합니다.
- 교체는 프로필 ID·root·인증 방식을 유지해야 합니다. 새 인증이 같은 계정에
  속하는지는 호스트가 확인해야 합니다. 다른 계정·인증 방식은 새 프로필·root·
  대화를 사용하십시오. 온라인 계정 동일성 증명은 구현하지 않았습니다.
- 프로필 변경·비활성화는 이후 호출에 적용합니다. 진행 중인 요청 종료나 provider
  토큰 폐기는 하지 않습니다. 즉시 종료가 필요하면 해당 요청을 취소·종료하십시오.
- warm 프로세스는 `open_warm(auth_profile=...)`로 열고 토큰 교체 시 닫았다가 다시
  여십시오. 환경은 생성 시 고정됩니다. 프로필과 별도 warm env를 함께 지정할 수
  없고 resume 히스토리는 해당 프로필의 작업 경로 안에 있어야 합니다.
- `AgentProfile.chat/chat_async/chat_stream`에 서비스 client를 명시적으로 전달하십시오.
  독립 서비스를 위해 프로세스 전역 `set_default_client`를 반복 변경하지 마십시오.
- 인증은 도구·파일 권한을 제한하지 않습니다. 위 예제의 plan 모드처럼 최소 권한
  실행 정책을 별도로 구성하십시오.

## 상태·사용량·이행

`health_check(probe=False)`는 로컬 준비 상태를 확인하고 준비된 프로필에
`configured`, `auth_ok=None`을 반환합니다. 원격 서버가 토큰을 승인했다는 뜻이
아닙니다. `probe=True`는 모델을 호출해 사용량을 소비하거나 도구를 실행할 수
있습니다. CLI version 확인 실패는 준비 완료로 간주하지 않습니다.

계정 사용량과 세션 liveness는 같은 프로필의 토큰·경로를 사용합니다. API-key
방식의 구독 사용량 조회는 미지원으로 반환하며 다른 계정을 조회하지 않습니다.
반환 오류·응답 본문은 비공개 데이터이므로 로깅하지 마십시오. `public_dict()`와
repr은 토큰을 제외하지만 인증 필드·`environment()`를 직접 출력하면 노출됩니다.
프로필 root와 진단 파일의 접근 권한을 보호하십시오.

`auth_profile` 없는 기존 client는 이전의 공유 환경·로그인 동작을 유지합니다.
기능을 설치했다고 기존 서비스가 자동 격리되지는 않습니다. 각 서비스를 명시적으로
이행하고 실제 계정을 확인하십시오. 오프라인 테스트는 원격 인증·실제 토큰 교체·
유료 생성을 수행하지 않습니다.

## 검증 기록 — 2026-10-12

코드 커밋: `c6f1f79`; 추적 파일만 추출한 검증 소스: `5072c53`.

- 인증 전용 테스트 **46개 통과**. 지원하는 세 provider의 실제 가짜 CLI 자식
  프로세스, 동기·비동기·스트리밍 동시 실행, 요청 중 토큰 교체, 비활성화·인증 누락,
  세션 소유권, 사용량 조회 경로, Windows junction 거부를 검증했습니다.
  실제 원격 계정 인증 테스트는 아닙니다.
- 깨끗한 소스의 Windows 전체 테스트 **1,066개 통과, 39개 건너뜀**.
  POSIX 전용 검사·사용할 수 없는 선택적 연동·symlink 권한 검사는 이 환경에서
  검증하지 못했습니다. 이번 검증에서 Linux/macOS CI는 실행하지 않았습니다.
- 격리 환경 sdist·wheel 빌드, `twine check`, 런타임 의존성 0개, 인증 모듈·테스트·
  문서 포함 여부, 한영 UTF-8 문서·로컬 링크·Python 예제 문법 검사를 통과했습니다.
- 설치된 Claude **2.1.296**, Codex **0.161.0**은 가짜 토큰의 전용 프로필 환경에서
  오프라인 `--version`을 통과했습니다. Codex의 파일 인증 저장 설정도 `exec --help`로
  확인했습니다. 이 호스트에는 Copilot이 설치되어 있지 않습니다.
- 모델·자식 프로세스 없는 가짜 provider의 기존 경로 측정은 수정 전 동기
  186–201 µs / 비동기 343–370 µs, 수정 후 동기 188–192 µs / 비동기 382–393 µs였습니다.
  작은 표본이며 스케줄링 변동을 포함합니다. 모델 처리량 측정이나 성능 퇴보가
  전혀 없다는 증거로 해석하면 안 됩니다.

공용 로그인·실제 계정 인증·운영 배포는 변경하지 않았습니다. push·태그·PyPI 업로드·
유료 생성도 수행하지 않았습니다. 빌드 메타데이터 0.7.5는 브랜치 내부 검증용이며
이미 배포된 0.7.5에는 이번 기능이 없습니다. 운영 적용 전 서비스별로 안전하게
준비한 실제 인증과 설치된 CLI 버전으로 계정 동일성·오류·토큰 교체를 확인하십시오.
