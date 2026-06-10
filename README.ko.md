# agentcli

[English](README.md) | [한국어](README.ko.md)

**Claude Code, Codex, GitHub Copilot CLI를 Python 앱의 세션형 AI 백엔드로 붙이기 위한 경량 클라이언트입니다.**

`agentcli`는 또 하나의 agent CLI가 아닙니다. 이미 사용자의 컴퓨터에 설치되고 로그인된 agentic CLI를 Python 제품이나 자동화 코드 안에서 호출할 때 필요한 세션, 스트리밍, 사용량 기록, 지시문 변경 감지, provider fallback을 하나의 API로 묶는 작은 라이브러리입니다.

---

## 이 프로젝트는 무엇인가

제품이나 자동화 파이프라인에서 Claude Code, Codex, GitHub Copilot CLI를 장기 실행 AI 백엔드처럼 다루고 싶을 때 사용합니다.

```python
resp = await client.chat_async(
    "이 저장소를 검토하고 주요 위험을 요약해줘.",
    provider="codex",
    owner="my-app",
    alias="repo-reviewer",
    cwd="/path/to/project",
)
```

호스트 앱은 일반적인 Python API를 사용합니다. 실제 도구 실행, 인증, 로컬 설정, 네이티브 세션은 각 provider CLI가 계속 소유합니다.

## 이 프로젝트가 아닌 것

- Claude Code, Codex, GitHub Copilot CLI의 대체품이 아닙니다.
- hosted LLM API 클라이언트나 인증 정보 중계기가 아닙니다.
- 자체 tool loop를 가진 풀 agent framework가 아닙니다.
- 여러 CLI의 네이티브 히스토리를 복사/동기화하는 session-sync 제품이 아닙니다.

각 사용자는 자기 환경에서 provider CLI를 설치하고 로그인해야 합니다. `agentcli`는 인증 정보, 세션, provider 바이너리를 포함하지 않습니다.

## 왜 필요한가

Claude Code, Codex CLI, GitHub Copilot CLI는 비슷한 문제를 풀지만 세션 방식, 출력 이벤트, 권한 옵션이 서로 다릅니다.

| | Claude Code | Codex CLI | GitHub Copilot CLI |
|---|---|---|---|
| 세션 | `--session-id`, `--resume <sid>` | `codex exec resume <sid>` | `--resume=<sid>`, `--name=<alias>` |
| 스트리밍 | `--output-format stream-json` | `--json` JSONL | `--output-format json` JSONL |
| 권한/샌드박스 | `--permission-mode`, `--allowedTools` | `-s <mode>`, `-a <policy>` | `--allow-tool`, `--deny-tool`, `--add-dir` |
| 세션 저장 | `~/.claude/projects/<cwd>/<sid>.jsonl` | `~/.codex/sessions/.../<sid>.jsonl` | Copilot CLI가 관리 |

`agentcli`는 이 차이를 하나의 계약으로 감싸서 앱이 provider별 glue code를 직접 다시 만들지 않게 합니다.

중요한 경계는 단순 `subprocess.run(...)`보다 위에 있습니다. `agentcli`는 `owner + alias + cwd` 기반 agent identity, 네이티브 세션 handle, 사용량 집계, 지시문 freshness, 안전한 health output, 표준화된 streaming error, 명시적 fallback 정책을 제공합니다.

## 프로젝트 상태

현재 상태는 public beta입니다. API는 테스트되어 있고 실제 통합에 사용할 수 있지만, provider CLI들이 빠르게 바뀌기 때문에 1.0 전까지는 작은 breaking change가 있을 수 있습니다.

더 자세한 제품 경계는 [docs/positioning.ko.md](docs/positioning.ko.md)를 보세요.

## 핵심 설계 원칙

**대화 히스토리의 single source of truth는 provider CLI 세션입니다.** 라이브러리는 provider별 `session_id`만 저장하고 이전 대화를 prompt에 다시 주입하지 않습니다. 이 원칙 때문에 토큰 중복, 히스토리 중복 저장, 예측하기 어려운 context 증가를 피할 수 있습니다.

- 새 호출은 새 CLI 세션을 만들거나 기존 `session_id`로 resume합니다. 단 하나의 예외는 Windows의 Claude로, `-p` + `--resume` 조합이 인터랙티브 입력 대기로 빠져 행이 걸릴 수 있어(issue #4) 무상태로 동작합니다. macOS/Linux의 Claude는 첫 호출에서 `--session-id`를 발급하고 이후 같은 대화에서 `--resume <sid>`로 이어갑니다(Claude Code 2.1.x에서 검증, resume해도 동일 ID 유지).
- `Conversation.metadata["session_id:<provider>"]`만 저장합니다.
- `system_prompt`와 `AgentProfile.instructions`는 해당 지시문 hash를 세션이 아직 보지 않았거나 변경되었을 때만 주입합니다.
- 세션이 없는 custom provider를 추가할 경우에만 라이브러리가 이전 messages를 직렬화할 수 있습니다.

이 원칙은 프로젝트의 4대 불변식 중 하나입니다. 전체 목록(런타임 의존성 0, 세션 = 단일 진실 소스, 세 provider 정규화, 한/영 문서 페어링)은 [설계 불변식](docs/positioning.ko.md#설계-불변식)을 보세요.

### 히스토리 사용 방식 선택

모든 호출은 세 가지 히스토리 모드 중 하나를 명시적으로 선택합니다.

| 모드 | 방법 |
|---|---|
| CLI 네이티브 세션 (기본) | 같은 `owner` + `alias` → provider 자체 세션을 resume. 이전 턴은 CLI가 기억. |
| 호스트 주입 컨텍스트 | `inject_context=[{"conversation_id": ..., "limit": 10, "agent": ""}]` — 호스트가 큐레이션한 메시지를 라벨된 컨텍스트 블록으로 프롬프트에 직렬화. 세션 provider에도 적용됨. |
| 히스토리 미사용 | `new_session=True` — 이 호출만 새 CLI 세션에서 시작 (이후 alias는 새 세션을 추적). 또는 새 alias 사용. |

provider CLI가 자기 실행 환경을 소유하므로, `cwd`의 프로젝트 레벨
`CLAUDE.md`/`AGENTS.md`, Agent Skills(`.claude/skills/`), 커스텀
서브에이전트(`.claude/agents/`)는 CLI가 네이티브로 로드합니다 —
Claude Code 2.1.x 대상 E2E로 검증.

## 저장소 모델

`agentcli`의 저장소는 chat transcript 보관용이 아니라 **세션 라우팅과 사용량 감사**를 위한 것입니다.

- `MemoryStore`는 기본 경량 저장소입니다.
- `SQLiteStore`는 alias, provider session ID, instruction hash, usage row를 프로세스 재시작 후에도 유지합니다.
- Claude/Codex/Copilot은 자체 세션 히스토리를 소유하므로 SQLite `messages` 테이블은 비어 있습니다.
- message API는 나중에 추가될 수 있는 세션 없는 custom provider를 위해 남아 있습니다.

## 설치

```bash
# 첫 PyPI 릴리즈 이후:
pip install agentcli-py

# 그 전에는 공개 GitHub 저장소에서 직접 설치:
pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.5.1"

# 로컬 개발:
pip install -e /path/to/agentcli
```

Python 3.11 이상이 필요합니다. 런타임 의존성은 없습니다.

실제 호출 시점에는 아래 CLI가 사용자의 `PATH`에서 발견되어야 합니다.

- `claude` (Claude Code)
- `codex`
- `copilot` 또는 `gh copilot`

## 빠른 시작

```python
import asyncio
from agentcli import LLMClient, MemoryStore

async def main():
    client = LLMClient(store=MemoryStore())

    health = client.health_check("claude")
    if not health.ok:
        raise RuntimeError(health.suggested_action or health.message)

    resp = await client.chat_async(
        "이 저장소를 세 문장으로 요약해줘.",
        provider="claude",
        model=client.select_model("claude", "sonnet"),
        strict_model=True,
        owner="demo",
        alias="repo-summary",
        cwd="/path/to/workspace",
        reset_on_instruction_change=True,
        wall_timeout=300,
    )
    if not resp.content:
        raise RuntimeError(resp.suggested_action or resp.error)
    print(resp.content)

asyncio.run(main())
```

명시한 provider 호출은 기본적으로 그 provider만 시도합니다. 다른 provider로 넘어가는 fallback은 명시적으로 켜야 합니다.

```python
resp = await client.chat_async(
    "Claude를 먼저 시도하고 실패하면 fallback chain을 사용해줘.",
    provider="claude",
    fallback=True,
)
```

## 주요 기능

- `LLMClient.chat`, `chat_async`, `chat_stream`
- `owner + alias + cwd` 기반 장기 세션 identity
- provider별 session ID 저장과 resume
- 명시적 provider/model 선택
- opt-in fallback
- health check와 UI/log-safe `ProviderHealth.public_dict()`
- usage stats와 cached token 집계
- `AGENTS.md`, `CLAUDE.md`, `GUIDE.md`, `AGENTS.override.md` 변경 감지
- `AgentProfile`과 `AgentRegistry` 기반 instruction materialization
- `MemoryStore`와 `SQLiteStore`

## 보안과 운영 경계

기본 provider 설정은 개발 편의성에 맞춰 비교적 permissive합니다. 멀티테넌트 또는 신뢰할 수 없는 작업공간에 넣을 때는 provider별 권한 옵션을 직접 좁혀야 합니다.

```python
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli import ProviderRegistry

registry = ProviderRegistry()
registry.register(ClaudeProvider(
    permission_mode="default",
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash"],
))
registry.register(CodexProvider(
    sandbox_mode="workspace-write",
    full_auto=False,
))
registry.register(CopilotProvider(
    allow_all_tools=False,
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash"],
    add_dirs=["/tmp"],
))
```

## 테스트

```bash
pip install -e ".[dev]"
pytest
```

현재 344개 테스트가 session routing, async/streaming parity, alias resolution, health check, drift detection, usage aggregation, profile materialization, SQLite session persistence, 같은 conversation 동시 호출 직렬화, Codex/Copilot JSONL parsing을 다룹니다.

## 릴리즈

- 현재 릴리즈: `0.5.1`
- 릴리즈 노트: [docs/releases/v0.5.1.ko.md](docs/releases/v0.5.1.ko.md)
- 릴리즈 절차: [docs/release.ko.md](docs/release.ko.md)

## 라이선스

MIT. [LICENSE](LICENSE)를 보세요.
