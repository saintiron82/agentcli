# AgentCli 운영 지침

[English](operations.md) | [한국어](operations.ko.md)

범위: v0.7.6 보안 보강. 실제 서버가 아래 조건을 충족한다는
증명이 아니라 배포용 운영 문서 양식입니다. 마지막 검토: 2026-10-22.

[코드 개선 기록](code-improvements.ko.md)에 동시성 수정·측정 오버헤드·호환성과
남은 운영 적용 게이트를 정리했습니다.

## 신뢰 경계와 식별

같은 머신의 여러 서비스는 각 client에 별도의
[서비스 인증 프로필](service-auth.ko.md)을 연결하십시오. 서비스 계정 선택을 위해
공용 머신 로그인이나 전역 기본 client를 전환하지 마십시오. 프로필 격리는
선택 사항이며 OS·컨테이너 경계를 대체하지 않습니다.

AgentCli는 서비스 계정 권한으로 설치된 CLI를 실행합니다. OS 샌드박스·인증 서버·
인증 정보 중계기가 아닙니다. 사용자 인증 후 `owner`를 결정하고 HTTP 요청에서
임의의 owner/cwd/provider 옵션을 그대로 받아 전달하지 마십시오. 저장소 관리 API와
provider 직접 호출은 앱의 권한 검사를 구현하지 않습니다. 신뢰하는 호스트 코드로 제한하십시오.

신뢰 영역마다 OS 계정 또는 컨테이너를 분리하십시오. CLI 인증 정보·네이티브
히스토리·SQLite·진단 trace를 비공개로 유지하십시오. POSIX는 비공개 디렉터리와
제한된 파일 권한을, Windows는 서비스 계정과 지정 관리자만 허용하는 ACL을
사용하고 상속 ACL도 확인하십시오. 인증 정보·원본 응답을 Git에 올리지 마십시오.
부모 환경 변수·프로젝트 지시문·스킬·MCP 서버·hook은 실행 가능한 신뢰 입력입니다.
읽기 전용 도구만으로 네트워크 격리나 유출 방지를 보장하지 않습니다. 필요한 경우
호스트에서 외부 통신도 제한하십시오.

## 제한된 호출 예제

```python
from pathlib import Path
from agentcli import LLMClient, SQLiteStore

workspace = str(Path("/srv/projects/review").resolve())
store = SQLiteStore("/srv/private/agentcli/state.db")  # 비공개 부모 폴더를 먼저 준비
client = LLMClient(store)
response = client.chat(
    "코드를 검토하되 파일을 변경하지 마십시오.", provider="codex",
    owner="authenticated-user-17", alias="review-only", cwd=workspace,
    provider_options={"sandbox_mode": "read-only", "approval_policy": "never"},
    fallback=False, schema_retries=0, timeout=120, wall_timeout=180,
)
# response.error_type 확인. raw_stderr/error를 신뢰하지 않는 사용자에게 노출하지 않음.
# 이 저장소를 사용하는 모든 클라이언트를 중지한 뒤 닫음.
store.close()
```

프롬프트는 의도이지 보안 경계가 아닙니다. 설치된 CLI의 실제 샌드박스 동작을
검증하십시오. 다른 작업 경로에는 새 alias를 사용하십시오. 제한 정책 기록이 없는
기존 Codex 세션은 `new_session=True`가 필요합니다. 작업 경로 기록이 없는 기존
대화는 첫 성공 호출에서 경로가 연결되므로 출처를 먼저 확인하거나 새 alias를
사용하십시오. 새 Copilot 네이티브 이름은 사람이 읽는 alias 대신 대화 ID로
만들며 기존 네이티브 세션 ID는 바꾸지 않습니다. 알 수 없는 Windows batch
launcher는 네이티브 실행 파일·표준 npm shim·지원되는 해석기 wrapper로 바꿔야 합니다.

## 개인 및 단일 호스트 서비스

MemoryStore는 한 앱의 임시 handle에, SQLiteStore는 재시작 후에도 필요한 handle에
사용하십시오. SQLiteStore는 세션별 sidecar 파일 잠금으로 로컬 writer를 조정합니다.
호출 중 잠금 파일을 삭제하지 마십시오. 별도 MemoryStore 객체는 상태를 공유하지
않습니다. 실제 히스토리는 여전히 각 CLI의 네이티브 위치에 있습니다.
안정적인 절대 작업 경로와 신뢰 영역별 CLI 계정을 권장합니다.

Python·AgentCli·CLI 버전을 기록하고 검증한 버전을 고정하십시오. 업그레이드 전
테스트·`--version`·생성 probe 없는 health check를 실행한 뒤 승인된 제한 환경에서
smoke test를 수행하십시오. 생성 probe는 사용량을 소비하고 도구를 실행할 수 있습니다.

## 다중 서버

네이티브 세션마다 다음 중 하나를 선택하십시오.

1. 해당 세션을 네이티브 히스토리와 계정을 가진 단일 writer 호스트로 라우팅합니다.
2. 검증된 분산 lease를 제공하고 provider가 지원하는 방식으로 선택된 writer에서
   네이티브 히스토리에 접근할 수 있도록 구성합니다.

AgentCli SQLite를 복사한다고 네이티브 히스토리까지 복사되지 않습니다. 파일
잠금은 로컬 파일시스템만 검증했으며 분산 NFS/SMB 서비스를 보증하지 않습니다.
별도 로컬 DB의 같은 alias는 별개 논리 세션입니다. 파일시스템·부하를 검증하지
않은 공유 파일 SQLite는 피하십시오.

선택 사항인 `session_lock_factory(store, conversation_id)`는 `acquire()`·`release()`
메서드를 가진 lease를 반환합니다. 메서드는 스레드 사이에서 동작하고 유한한
시간 상한이 있으며 획득 실패 후 해제도 안전해야 합니다. 호스트가 소유권 확인·
갱신·만료·오래된 네이티브 writer 차단 또는 종료를 구현해야 합니다. 갱신·fencing
없는 Redis 키 하나만으로는 충분하지 않습니다. 이 저장소는 분산 잠금 서비스를
설치하지 않습니다. 동일 백엔드를 가리키는 별도 커스텀 저장소도 호스트 잠금 식별자가 필요합니다.

실서비스 준비 완료로 판단하기 전에 아래 배포 정보를 기록하십시오.

| 항목 | 필요한 근거 |
| --- | --- |
| 토폴로지 | 호스트·파일시스템·세션 라우팅·장애 전환 방식 |
| 식별 | 인증된 owner 매핑·OS 계정·인증 파일 ACL |
| 실행 경계 | CLI 샌드박스·도구·네트워크 정책 및 승인된 MCP/hook/스킬 |
| 조정 | lease 갱신·오래된 writer 차단 테스트 또는 단일 writer 근거 |
| 복구 | 암호화 백업 위치·보존 기간·복구 훈련·담당 운영자 |
| 관측 | timeout·종료 분류·대기열·실패/부분 호출·사용량 알림 |

## 실패·재시도·관측

명시한 `wall_timeout`은 fallback과 재시도 전체에 하나의 실행 예산을 사용합니다.
잠금 대기·제한된 종료 정리 때문에 반환은 더 늦을 수 있으므로 호스트 대기열과
운영 시간 상한도 설정하십시오. 동기 커스텀 provider는 timeout을 준수해야 합니다.
Windows Job Object 연결은 프로세스 생성 후의 best-effort이며 엄격한 격리는
호스트 Job Object/컨테이너가 필요합니다. 연결 실패 경고를 관측하십시오.

변경 작업을 무작정 재시도하지 마십시오. timeout·스키마 오류 전에 도구가 실행됐을
수 있습니다. 중복에 민감한 작업은 멱등성 키·명시적 승인·fallback/스키마 재시도
비활성화와 실제 대상 상태 확인을 사용하십시오. 부분 텍스트 뒤 오류는 성공한
결과가 아닙니다. 클라이언트 스트림의 마지막 `done.data.error=True`도 확인하십시오.

자동 로그는 MCP/config 본문·원본 stderr를 제외하지만 반환된 오류·응답은 여전히
비공개 데이터이며 일반적인 비밀 제거를 보장하지 않습니다. Trace schema 2에는
ID·경로·도구 이름·시간·길이가 남으므로 접근·보존을 제한하십시오. 토큰 카운터는
추정값 또는 CLI 관측값이며 정확한 청구 원장이 아닙니다.

## 백업·보존·사고 대응

- 오프라인 SQLite 복사 전 writer를 멈추고 저장소를 닫으십시오. 필요한 WAL/SHM도
  일관되게 보존하거나 검증된 SQLite 온라인 백업 절차를 사용하십시오.
- 네이티브 CLI 히스토리를 별도로 provider 호환 절차에 따라 백업하십시오.
  백업은 암호화하고 계정·작업 경로 매핑을 기록하되 공유 명세에 평문 인증 정보를
  넣지 마십시오. 격리 환경에서 복구를 시험하십시오.
- 세션 metadata 삭제는 네이티브 CLI 히스토리 삭제가 아닙니다. metadata·히스토리·
  trace·백업의 보존 기간을 별도로 결정하십시오. 넓은 범위의 자동 삭제를 하지 말고
  저장소를 닫았다고 인증 정보가 폐기된 것으로 간주하지 마십시오.
- 노출 의심 시 해당 writer를 멈추고 인증·로그 접근을 격리하며 제한된 증거를
  보존하십시오. provider에서 인증 정보를 회전/폐기하고 실행된 작업을 확인한 뒤
  외부 부작용을 정합화하고 복구하십시오.

이번 보강은 실제 서버 변경·인증 정보 회전·네이티브 히스토리 삭제·원격 push·
릴리즈 공개를 수행하지 않습니다.

## 병합 및 배포 검증

Windows 전체 테스트와 기존 Linux/macOS CI를 통과해야 합니다. 수동
`Windows security regression` workflow는 Python 3.11·3.14를 검증하며 느렸던
Windows 대기열을 모든 PR에 추가하지 않습니다. Actions는 확인한 전체 revision에
고정했으며 변경 시 upstream을 검토하십시오. 검토된 깨끗한 commit에서 빌드하고
`twine check`를 실행한 뒤 새 버전으로 배포하십시오. 기존 v0.7.5 태그·자산을
교체하지 마십시오. 변경 중인 체크아웃의 로컬 sdist에는 추적되지 않는 문서·예제가
포함될 수 있으므로 릴리즈 후보로 사용하지 마십시오.
