# KiroProvider (ACP one-shot 래퍼) 설계

- 상태: 승인됨 (브레인스토밍 → spec)
- 날짜: 2026-06-20
- 대상: `agentcli`에 네 번째 provider `kiro` 추가 — claude/codex/copilot와 동등한 풀-피처
- 관련 제약: `CLAUDE.md`(제로 의존성 · 3-provider 정규화 · 문서 페어), 메모리(`agentcli-vs-omo-positioning`: 의도된 `-p` 서브프로세스 niche)

## 1. 목표와 범위

Kiro CLI를 기존 3종과 **동등한 풀-피처 provider**로 통합한다: 세션 연속성 + 타입드
스트리밍 + 토큰 통계. 외부 계약(`LLMProvider` 인터페이스, 7개 정규화 청크 타입,
`session_id`만 저장)은 기존과 동일하게 유지하고, 내부 전송만 ACP를 사용한다.

**범위 밖 (Out of scope):**
- 영속 ACP 연결 풀 (접근법 ②) — one-shot 모델과 충돌, 추후 최적화로 보류.
- headless-lite 텍스트 전용 (접근법 ③) — 풀-피처 목표에 부적합.

## 2. 배경: 왜 ACP인가 (대안 기각 근거)

Kiro CLI는 두 비대화형 경로를 제공한다.

| 경로 | 형태 | 풀-피처 가능? |
|------|------|---------------|
| headless (`kiro-cli chat --no-interactive`) | one-shot, **평문 stdout** | ✗ — `--json` 없음, session id 미출력, usage는 credits뿐 |
| **ACP (`kiro-cli acp`)** | stdio **JSON-RPC 2.0** 서버 | ✓ — 구조화 스트리밍 + 세션 + 토큰 |

풀-피처(타입드 청크·토큰·세션 회수)는 평문 headless로 불가능하므로 ACP가 강제된다.
ACP는 영속 서버지만 **호출당 1회 one-shot으로 감싸면**(spawn → handshake → 1 turn →
종료) agentcli의 "CLI가 히스토리 소유, 라이브러리는 session_id만 저장" 계약을 보존한다.

근거 문서:
- Kiro ACP: https://kiro.dev/docs/cli/acp/
- ACP 세션 셋업 스펙: https://agentclientprotocol.com/protocol/session-setup
- Kiro headless(평문 확인): https://dev.classmethod.jp/en/articles/kiro-cli-2-0-headless-mode-api-key-auth/
- authMethods 이슈: https://github.com/kirodotdev/Kiro/issues/6603

## 3. 아키텍처 / 외부 계약

`agentcli/providers/kiro.py`에 `LLMProvider` 서브클래스 1개.

- `provider_id = "kiro"`
- `supports_sessions = True` (ACP `session/new` + `session/load`)
- `supports_streaming = True`
- `stores_history = False` — ACP가 `~/.kiro/sessions/cli/`에 히스토리를 소유하므로
  라이브러리는 대화 내용을 messages 테이블에 저장하지 않고 이전 턴을 재주입하지 않는다.
- 표면 메서드: `invoke` · `invoke_async` · `stream_async` · `list_models` ·
  `is_available` · `health_check` · `resolve_model`(base 상속).
- `list_models()`: `kiro-cli chat --list-models` 출력 파싱(가능 시) 또는 정적 known
  리스트로 fallback. `resolve_model`은 base 구현 재사용.
- `health_check()`: binary 존재 + `kiro-cli --version`/login 상태 확인. `probe=True`면
  최소 ACP turn 1회로 인증/쿼터까지 확인(다른 provider와 동일 계약).
- 메타데이터: `session_id:kiro`만 저장 (기존 키 규약 그대로).

전송이 ACP JSON-RPC라는 점은 **내부 구현 세부**이며, 외부에서 본 계약은 다른 3종과
동일하다. 이것이 "3-provider 정규화" 제약을 충족하는 방식이다.

## 4. 컴포넌트

1. **`KiroProvider`** — 표면 메서드 + 설정(권한/모델/agent/binary override).
2. **최소 ACP 클라이언트 (내부, 같은 파일 또는 `_acp.py` 헬퍼)** — 줄 단위
   JSON-RPC 2.0:
   - 요청 송신 + `id` 상관(correlation)으로 응답 매칭.
   - 알림(`session/update`) 소비.
   - **에이전트→클라이언트 역요청 응답**(아래 §6).
   - 기존 `base._run_stream_template`의 readline → `json.loads` → dispatch 골격을
     최대한 재사용하되, 단순 이벤트 스트림이 아니라 요청/응답+알림 혼합이므로 얇은
     상관 레이어를 추가한다.
3. **이벤트 매퍼** — ACP 이벤트를 agentcli 청크로 변환(`_dispatch_stream_event`
   override와 동일 역할).

## 5. 데이터 흐름 (1 turn = 1 invoke/stream 호출)

`kiro-cli acp` 서브프로세스는 호출의 `cwd`로 spawn한다(kiro 세션 DB가 디렉터리
스코프이고, fs 콜백 루팅도 cwd 기준이므로 — 다른 provider의 cwd 제어와 동일).

```
spawn `kiro-cli acp`  (cwd = 호출 cwd, env에 KIRO_API_KEY 상속)
  → initialize            (클라이언트 capability 선언; loadSession·authMethods 확인)
  → [첫 턴] session/new    (새 session id 회수)
    [재개] session/load(저장된 session_id)
  → session/prompt(prompt)
  → session/update 알림 소비 (스트리밍 청크 방출)
  → session/prompt 결과(stopReason)에서 turn 종료
  → done 청크(session_id·usage·latency) + 프로세스 정리
```

**청크 매핑 (ACP → agentcli):**

| ACP | agentcli 청크 |
|-----|---------------|
| AgentMessageChunk (text) | `text` |
| AgentThoughtChunk | `thinking` |
| ToolCall | `tool_use` |
| ToolCallUpdate | `tool_result` |
| `session/update` usage / `_kiro.dev/metadata` | `TokenUsage` 누적(payload 추정 포함) |
| session/prompt 결과 (stopReason) | `done` (session_id, usage, latency_ms) |
| JSON-RPC error / 프로토콜 오류 | `error` |

`invoke`(동기)는 위 turn을 단일 이벤트 루프로 구동해 최종 `LLMResponse`로 접는다.
`stream_async`는 같은 turn을 청크 단위로 yield한다 (공유 내부 코루틴 1개).

## 6. 클라이언트 역콜백 정책 (v1)

ACP 에이전트는 클라이언트에 역요청한다. 풀-피어가 되려면 최소 응답이 필요하다.

- **`session/request_permission`** → 권한 설정에 따라 자동 응답.
  - 결정(v1 기본): 기존 provider의 "dev 편의를 위한 permissive 기본" 관행과 동등하게
    **기본 auto-approve**, `trust_tools: list[str]`/`trust_all: bool` 생성자 인자로
    조정 가능. README 보안 노트와 동일하게 "multi-tenant/untrusted 시 조이라"를 문서화.
- **`fs/read_text_file` · `fs/write_text_file`** → `cwd` 하위로 경로 검증 후 실제 FS
  처리(에이전트가 프로젝트 파일을 편집 가능). cwd 밖 접근은 거부.
- **`terminal/*`** → 결정(v1): 클라이언트 capability로 **광고하지 않음**(기본). spike
  결과 kiro가 일반 turn에 terminal 위임을 요구하면 cwd-스코프 최소 핸들러를 추가하고,
  아니면 follow-up으로 분리.

## 7. 세션 / 계약 보존

- `session_id:kiro`만 저장. 첫 턴 `session/new` → id 회수, 재개 `session/load(id)`.
- `session/load` 실패(만료/없음) 시 `session/new`로 **1회 자동 복구** (codex/claude의
  stale-session 복구 패턴과 동일).
- `reset_on_instruction_change` · `new_session` · `inject_context`는 기존 client 로직을
  그대로 탄다 (provider는 받은 메시지를 충실히 직렬화).

## 8. 에러 처리

| 상황 | 결과 |
|------|------|
| binary 없음 (`shutil.which("kiro-cli")` None) | `binary_missing`, exit 127 |
| 인증 실패(authMethods 미충족 / `KIRO_API_KEY` 부재) | `error_type=auth` |
| idle/wall timeout | 기존 `_run_stream_template` 타임아웃 재사용 |
| JSON-RPC/프로토콜 오류, 비-객체 라인 | `error` 청크(raw 보존), 스트림 비중단 |
| 프로세스 종료(GeneratorExit 포함) | `try/finally`로 kill+wait (좀비 방지, 기존 골격) |

issue #6603: `initialize`가 인증돼 있어도 authMethods를 반환할 수 있으므로, authMethods
존재 ≠ 미인증으로 단정하지 않는다. `KIRO_API_KEY` 있으면 진행, 없고 authMethods가
요구되면 `auth` 에러로 정규화.

## 9. 정규화 · 등록 · 문서

- README "Provider capabilities" 표에 `KiroProvider` 행 추가: `supports_sessions ✅
  (ACP session/load)`, `supports_streaming ✅`, session id source = `session/new` 결과,
  전송 = ACP JSON-RPC(주석). **README.md ↔ README.ko.md 페어 동시 갱신**(CLAUDE.md).
- `create_default_registry`에 `KiroProvider` 등록. fallback 순서는 사용자 제어.
- 스트리밍 청크 계약 문서(README/CLAUDE.md)에 새 청크 타입 추가 없음 — 7개 그대로 매핑.

## 10. 제약 준수

- **제로 의존성**: JSON-RPC 2.0 over stdio = stdlib `json` + `asyncio`. 신규 의존성 0.
- **문서 페어**: 출시 문서(README 등)는 EN/KO 동시 갱신.
- **용어**: "고아/orphan" 미사용.

## 11. 테스트 전략 (hermetic — 실 kiro-cli 불필요)

- `tests/_acp_helpers.py`: **가짜 ACP 서버 하니스** — 캔드 JSON-RPC 교환(줄 단위 JSON)을
  주입하는 mock subprocess. initialize 결과, session/new 결과, session/update 알림
  (text/thinking/tool), prompt 결과(usage 포함)를 시나리오로 구성.
- `tests/test_kiro_provider.py`: `_find_binary` mock(← 직전 PR에서 배운 교훈: 실 바이너리
  의존 금지)로 hermetic 보장. 검증 항목:
  - 청크 매핑(text/thinking/tool_use/tool_result/done)
  - 첫 턴 session id 회수, 재개 시 `session/load` 호출 + 저장 id 전달
  - usage 파싱(토큰 + payload 추정)
  - `session/request_permission` 자동응답(trust 설정별)
  - stale 세션 → `session/new` 1회 복구
  - `binary_missing` / `auth` / timeout
  - GeneratorExit/조기 종료 시 프로세스 정리
- CI 매트릭스(3.11–3.14 × macOS/Linux) 재사용 — 실 kiro-cli 미요구.

## 12. 검증 spike (구현 plan의 첫 태스크 — 키 확보 시점에 실행)

현재 `KIRO_API_KEY` 미확보로 **spike는 보류**, 설계/스펙은 문서 기반으로 진행한다.
키 확보 시 실 `kiro-cli acp` 대상 ~30줄 스크립트로 아래 **가정**을 확정하고, 동작을
특정 kiro-cli 버전에 고정한다("Claude Code 2.1.x 검증"과 동일 관행):

검증할 가정:
1. `initialize` 응답의 `loadSession` capability + `authMethods` 형태.
2. `session/update` 알림의 정확한 필드명(text/thought/tool 구분)과 usage 위치.
3. 토큰 카운트가 `session/update`/`_kiro.dev/metadata` 중 어디에, 어떤 단위로 오는지.
4. `fs/*` · `terminal/*` 위임 여부 (= §6 terminal 결정 확정).
5. JSON-RPC 프레이밍이 줄 단위 개행인지(스펙상 그렇다고 명시됨) 재확인.

## 13. 열린 항목 (spec 내 잠정 결정 — 리뷰 대상)

- **terminal/* (v1)**: 광고 안 함이 기본, spike 결과에 따라 추가. (§6)
- **권한 기본값**: auto-approve 기본 + `trust_tools`/`trust_all` 조정. (§6)

두 항목 모두 §12 spike에서 확정되며, 그 전까지는 위 잠정 결정으로 구현을 시작할 수 있다.
