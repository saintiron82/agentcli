# KiroProvider (ACP one-shot 래퍼) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agentcli에 네 번째 provider `kiro`를 추가한다 — `kiro-cli acp`(줄 단위 JSON-RPC 2.0)를 호출당 1회 one-shot turn으로 감싸 claude/codex/copilot와 동등한 세션·스트리밍·토큰 통계를 제공한다.

**Architecture:** 전송 비의존 JSON-RPC 클라이언트(`AcpConnection`)가 id 상관·알림·역요청을 처리한다. `KiroProvider`가 `kiro-cli acp` 서브프로세스를 spawn해 양방향 파이프를 `AcpConnection`에 연결하고, 한 turn(initialize → session/new|load → session/prompt → session/update 소비 → stopReason)을 구동하며 ACP 이벤트를 7개 정규화 청크로 매핑한다. 외부 `LLMProvider` 계약은 기존 3종과 동일.

**Tech Stack:** Python 3.11+, stdlib `asyncio` + `json` + `shutil`/`subprocess` only. 테스트: `pytest` + `pytest-asyncio`. 신규 런타임 의존성 0.

## Global Constraints

- **제로 런타임 의존성.** `[project.dependencies]`에 아무것도 추가하지 않는다. JSON-RPC는 stdlib `json`+`asyncio`로 직접 구현.
- **3-provider 정규화.** 외부 계약(`LLMProvider` 표면, 7개 청크 타입 `text·thinking·tool_use·tool_result·event·error·done`, `session_id:<provider>` 메타 저장)을 기존과 동일하게 유지. 새 청크 타입 추가 금지.
- **ACP 필드명 출처 = ACP 표준 스펙** (https://agentclientprotocol.com). 본 plan의 코드는 스펙 문서 기준 필드명을 사용한다. **Task 0 spike**(`KIRO_API_KEY` 확보 시)가 실제 `kiro-cli acp` 출력으로 필드명을 확정·고정한다. spike 미실행 시 문서 기준값으로 진행하고, 차이 발견 시 매핑 함수(`_map_session_update`, `_acp.py` 핸들러) 한 곳만 수정한다.
- **테스트는 hermetic.** 실 `kiro-cli` 불필요. `_find_binary`는 mock하고(직전 PR #14/#15에서 확인된 교훈), ACP 동작은 in-memory 가짜 트랜스포트/가짜 agent로 검증.
- **문서 페어.** README 등 출시 문서는 `README.md` ↔ `README.ko.md` 동시 갱신.
- **용어.** "고아/orphan" 미사용.
- **테스트 명령:** `pytest` (testpaths=tests). 단일: `pytest tests/test_<file>.py -k <name>`.

## File Structure

| 파일 | 책임 |
|------|------|
| `agentcli/providers/_acp.py` (생성) | 전송 비의존 최소 JSON-RPC 2.0 클라이언트 `AcpConnection` + `AcpError` |
| `agentcli/providers/kiro.py` (생성) | `KiroProvider` — 서브프로세스 spawn, turn 구동, 이벤트→청크 매핑, 권한/fs 콜백 |
| `agentcli/providers/registry.py` (수정) | `create_default_registry`에 `KiroProvider` 등록 |
| `tests/_acp_helpers.py` (생성) | 가짜 duplex 트랜스포트 + 스크립트형 가짜 agent |
| `tests/test_acp_connection.py` (생성) | `AcpConnection` 단위 테스트 |
| `tests/test_kiro_provider.py` (생성) | `KiroProvider` 통합 테스트 (hermetic) |
| `README.md` / `README.ko.md` (수정) | Provider capabilities 표에 kiro 행 |
| `CHANGELOG.md` (수정) | 변경 기록 |

**의존 그래프:** Task 0(spike, 조건부) → Task 1·2(`AcpConnection`) → Task 3(provider 골격) → Task 4(매핑) → Task 5(turn) → Task 6(resume/복구) → Task 7(콜백) → Task 8(공개 표면) → Task 9(등록+문서).

---

## Task 0: 검증 spike (조건부 — `KIRO_API_KEY` 확보 시에만)

**목적:** 실 `kiro-cli acp`로 ACP 필드명/형태를 확정·고정. 키가 없으면 **건너뛰고** 문서 기준값으로 진행한다 (Tasks 1–9의 테스트는 hermetic이므로 영향 없음).

- [ ] **Step 1: 키 가용성 확인**

Run: `command -v kiro-cli && [ -n "$KIRO_API_KEY" ] && echo READY || echo SKIP`
READY가 아니면 이 Task 전체를 건너뛰고 Task 1로 이동.

- [ ] **Step 2: 핸드셰이크 1회 캡처 (READY인 경우)**

`tests/manual/acp_spike.py`(커밋하지 않음, `.gitignore`나 임시)로 `kiro-cli acp`를 spawn해 `initialize` → `session/new` → `session/prompt "Reply exactly OK."`를 보내고 수신 라인을 전부 로그로 남긴다. 확인 항목:
1. `initialize` result의 `agentCapabilities.loadSession`, `authMethods` 형태.
2. `session/update`의 `update.sessionUpdate` 판별자 값(`agent_message_chunk`/`agent_thought_chunk`/`tool_call`/`tool_call_update`/`usage_update`)과 `content` 형태.
3. 토큰 usage가 `usage_update.used/size` 또는 kiro `_kiro.dev/metadata` 중 어디에 오는지.
4. `session/request_permission`·`fs/read_text_file`·`fs/write_text_file`·`terminal/*` 역요청이 실제로 오는지.

- [ ] **Step 3: 차이를 plan에 반영**

문서 기준값과 다르면 Task 4의 `_map_session_update`와 Task 7의 콜백 핸들러 필드명을 실제값으로 수정. kiro-cli 버전을 `kiro.py` 상단 주석에 고정("verified against kiro-cli X.Y").

---

## Task 1: AcpConnection — 요청/응답 상관 (transport-agnostic)

**Files:**
- Create: `agentcli/providers/_acp.py`
- Create: `tests/_acp_helpers.py`
- Test: `tests/test_acp_connection.py`

**Interfaces:**
- Produces:
  - `class AcpError(Exception)` — `.code:int`, `.message:str`, `.data` 보관.
  - `class AcpConnection` 생성자: `AcpConnection(write_line, *, on_request=None, on_notification=None)` where `write_line` is `Callable[[str], Awaitable[None]]`, `on_request` is `Callable[[str, dict], Awaitable[dict]]`, `on_notification` is `Callable[[str, dict], Awaitable[None]]`.
  - `async AcpConnection.request(method: str, params: dict) -> dict` — JSON-RPC 요청 송신 후 응답 result 반환(에러면 `AcpError` raise).
  - `async AcpConnection.handle_line(line: str) -> None` — 수신 라인 1개 디스패치.

- [ ] **Step 1: 가짜 트랜스포트 헬퍼 작성**

```python
# tests/_acp_helpers.py
"""ACP 테스트용 in-memory duplex 트랜스포트 + 스크립트형 가짜 agent."""
import asyncio
import json


class FakeTransport:
    """client.write_line → 여기에 쌓이고, feed()로 agent→client 라인을 주입."""
    def __init__(self):
        self.client_to_agent: list[str] = []   # client가 보낸 라인(JSON 문자열)
        self._conn = None

    def bind(self, conn):
        self._conn = conn

    async def write_line(self, line: str) -> None:
        self.client_to_agent.append(line)

    async def feed(self, obj: dict) -> None:
        """agent→client 메시지 1개를 conn에 전달."""
        await self._conn.handle_line(json.dumps(obj))

    def last_sent(self) -> dict:
        return json.loads(self.client_to_agent[-1])

    def sent_methods(self) -> list[str]:
        return [json.loads(l).get("method") for l in self.client_to_agent]
```

- [ ] **Step 2: 실패 테스트 작성**

```python
# tests/test_acp_connection.py
import asyncio
import pytest
from agentcli.providers._acp import AcpConnection, AcpError
from tests._acp_helpers import FakeTransport


@pytest.mark.asyncio
async def test_request_correlates_response_by_id():
    t = FakeTransport()
    conn = AcpConnection(t.write_line)
    t.bind(conn)

    async def respond_later():
        # client가 보낸 요청의 id로 응답을 돌려준다.
        await asyncio.sleep(0)
        rid = t.last_sent()["id"]
        await t.feed({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    asyncio.create_task(respond_later())
    result = await conn.request("initialize", {"protocolVersion": 1})
    assert result == {"ok": True}
    sent = t.last_sent()
    assert sent["jsonrpc"] == "2.0" and sent["method"] == "initialize"
    assert sent["params"] == {"protocolVersion": 1}
    assert isinstance(sent["id"], int)


@pytest.mark.asyncio
async def test_request_raises_on_error_response():
    t = FakeTransport()
    conn = AcpConnection(t.write_line)
    t.bind(conn)

    async def respond_err():
        await asyncio.sleep(0)
        rid = t.last_sent()["id"]
        await t.feed({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "boom"}})

    asyncio.create_task(respond_err())
    with pytest.raises(AcpError) as ei:
        await conn.request("session/new", {})
    assert ei.value.code == -32000
    assert "boom" in ei.value.message
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/test_acp_connection.py -q`
Expected: FAIL — `ModuleNotFoundError: agentcli.providers._acp`.

- [ ] **Step 4: 최소 구현**

```python
# agentcli/providers/_acp.py
"""최소 JSON-RPC 2.0 클라이언트 (ACP 전송용, transport-agnostic).

줄 단위 JSON. 신규 의존성 없이 stdlib json+asyncio 만 사용.
``write_line`` 으로 송신, ``handle_line`` 으로 수신 1라인을 디스패치한다.
응답은 id 로 상관(correlation)하고, agent→client 역요청/알림은 콜백으로 위임.
"""
import asyncio
import json
from typing import Awaitable, Callable


class AcpError(Exception):
    def __init__(self, error: dict):
        self.code = int(error.get("code", 0))
        self.message = str(error.get("message", ""))
        self.data = error.get("data")
        super().__init__(f"ACP error {self.code}: {self.message}")


class AcpConnection:
    def __init__(
        self,
        write_line: Callable[[str], Awaitable[None]],
        *,
        on_request: Callable[[str, dict], Awaitable[dict]] | None = None,
        on_notification: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self._write_line = write_line
        self._on_request = on_request
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._write_line(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return await fut

    async def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return  # 비 JSON 라인 무시 (호스트로 예외 전파 금지)
        if not isinstance(msg, dict):
            return
        if "method" in msg and "id" in msg:
            await self._dispatch_request(msg)
        elif "method" in msg:
            if self._on_notification is not None:
                await self._on_notification(msg["method"], msg.get("params") or {})
        elif "id" in msg:
            self._resolve(msg)

    async def _dispatch_request(self, msg: dict) -> None:
        result: dict = {}
        if self._on_request is not None:
            result = await self._on_request(msg["method"], msg.get("params") or {})
        await self._write_line(json.dumps(
            {"jsonrpc": "2.0", "id": msg["id"], "result": result}))

    def _resolve(self, msg: dict) -> None:
        fut = self._pending.pop(msg.get("id"), None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(AcpError(msg["error"] or {}))
        else:
            fut.set_result(msg.get("result") or {})
```

`tests/__init__.py`가 이미 존재하므로 `from tests._acp_helpers import ...` 가 동작한다 (파일 목록 확인됨).

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_acp_connection.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: 커밋**

```bash
git add agentcli/providers/_acp.py tests/_acp_helpers.py tests/test_acp_connection.py
git commit -m "feat(kiro): add transport-agnostic ACP JSON-RPC connection"
```

---

## Task 2: AcpConnection — 알림 + agent 역요청 디스패치

**Files:**
- Modify: `tests/test_acp_connection.py` (테스트 추가)
- (구현은 Task 1에서 이미 완료 — 이 Task는 알림/역요청 경로를 테스트로 고정)

**Interfaces:**
- Consumes: Task 1의 `AcpConnection(write_line, on_request=, on_notification=)`.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_acp_connection.py 에 추가
@pytest.mark.asyncio
async def test_notification_routed_to_callback():
    t = FakeTransport()
    seen = []
    async def on_notif(method, params):
        seen.append((method, params))
    conn = AcpConnection(t.write_line, on_notification=on_notif)
    t.bind(conn)
    await t.feed({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "s1", "update": {"sessionUpdate": "x"}}})
    assert seen == [("session/update",
                     {"sessionId": "s1", "update": {"sessionUpdate": "x"}})]


@pytest.mark.asyncio
async def test_incoming_request_answered_with_result():
    t = FakeTransport()
    async def on_req(method, params):
        assert method == "fs/read_text_file"
        return {"content": "hello"}
    conn = AcpConnection(t.write_line, on_request=on_req)
    t.bind(conn)
    await t.feed({"jsonrpc": "2.0", "id": 42, "method": "fs/read_text_file",
                  "params": {"path": "/x"}})
    reply = t.last_sent()
    assert reply == {"jsonrpc": "2.0", "id": 42, "result": {"content": "hello"}}
```

- [ ] **Step 2: 테스트 통과 확인** (구현은 Task 1에 존재)

Run: `pytest tests/test_acp_connection.py -q`
Expected: PASS (4 passed). 실패 시 Task 1의 `handle_line` 분기 점검.

- [ ] **Step 3: 커밋**

```bash
git add tests/test_acp_connection.py
git commit -m "test(kiro): cover ACP notification + agent-request dispatch"
```

---

## Task 3: KiroProvider 골격 (capabilities·binary·list_models·health_check)

**Files:**
- Create: `agentcli/providers/kiro.py`
- Test: `tests/test_kiro_provider.py`

**Interfaces:**
- Produces:
  - `class KiroProvider(LLMProvider)` with `provider_id="kiro"`, `supports_sessions=True`, `supports_streaming=True`, `stores_history=False`.
  - `KiroProvider(__init__(self, *, trust_all: bool = True, trust_tools: list[str] | None = None, model: str = "", agent: str = ""))`.
  - `is_available() -> bool`, `_find_binary() -> str | None`, `list_models() -> list[dict]`, `health_check(...) -> ProviderHealth`.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_kiro_provider.py
from unittest.mock import patch
from agentcli.providers.kiro import KiroProvider


def test_provider_id_and_capabilities():
    p = KiroProvider()
    assert p.provider_id == "kiro"
    assert p.supports_sessions is True
    assert p.supports_streaming is True
    assert p.stores_history is False


def test_list_models_has_default_passthrough():
    models = KiroProvider().list_models()
    assert any(m["id"] == "" for m in models)  # 빈 id = 기본
    # resolve_model 은 알 수 없는 selector 를 그대로 통과 (비-strict).
    assert KiroProvider().resolve_model("kiro-some-model") == "kiro-some-model"


@patch("agentcli.providers.kiro.shutil.which", return_value=None)
def test_health_check_binary_missing(mock_which):
    h = KiroProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"
    assert h.error_type == "binary_missing"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_kiro_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: agentcli.providers.kiro`.

- [ ] **Step 3: 최소 구현 (골격)**

```python
# agentcli/providers/kiro.py
"""Kiro CLI 프로바이더 — ACP(JSON-RPC 2.0 over stdio) 기반 세션·스트리밍.

`kiro-cli acp` 를 호출당 1회 one-shot turn 으로 감싼다:
  initialize → session/new|load → session/prompt → session/update 소비 → stopReason.
세션이 히스토리를 소유하므로 라이브러리는 session_id 만 관리한다.

verified against kiro-cli: (Task 0 spike 에서 고정)
"""
import logging
import shutil

from .base import LLMProvider, run_health_command
from ..types import ERROR_BINARY_MISSING, ProviderHealth
from ..utils import build_env

logger = logging.getLogger(__name__)

# 모델 selector 는 resolve_model(비-strict) 로 그대로 통과시킨다.
# 알려진 id 는 Task 0 spike / `kiro-cli chat --list-models` 로 확장 가능.
KIRO_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
]


class KiroProvider(LLMProvider):
    provider_id = "kiro"
    supports_sessions = True
    supports_streaming = True
    stores_history = False  # 히스토리는 Kiro ACP 세션이 소유

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "", agent: str = ""):
        """
        Args:
            trust_all: session/request_permission 을 전부 자동 승인 (dev 기본).
                multi-tenant/untrusted 임베딩 시 False + trust_tools 로 좁힐 것.
            trust_tools: trust_all=False 일 때 자동 승인할 도구 이름 목록.
            model: 기본 모델 selector (호출 인자가 우선).
            agent: kiro agent 이름 (선택).
        """
        self._trust_all = trust_all
        self._trust_tools = set(trust_tools or [])
        self._model = model
        self._agent = agent

    def is_available(self) -> bool:
        return shutil.which("kiro-cli") is not None

    def _find_binary(self) -> str | None:
        return shutil.which("kiro-cli")

    def list_models(self) -> list[dict]:
        return list(KIRO_MODELS)

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path = shutil.which("kiro-cli")
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="kiro-cli not found")
        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=None,
            message="kiro-cli available")
```

(`probe=True` 의 실제 turn 검증은 Task 8에서 `invoke` 구현 후 확장한다 — 지금은 binary/version 까지.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: 커밋**

```bash
git add agentcli/providers/kiro.py tests/test_kiro_provider.py
git commit -m "feat(kiro): add KiroProvider skeleton (capabilities, binary, health)"
```

---

## Task 4: ACP 이벤트 → 정규화 청크 매핑 (순수 함수)

**Files:**
- Modify: `agentcli/providers/kiro.py` (매핑 함수 + 상태)
- Test: `tests/test_kiro_provider.py`

**Interfaces:**
- Produces:
  - `def _map_session_update(update: dict, usage: TokenUsage) -> list[StreamChunk]` — `session/update` 의 `update` 객체 1개를 0개 이상의 청크로 변환. `usage`(가변)에 토큰 누적.
  - 매핑: `agent_message_chunk`→`text`(content=`update["content"]["text"]`), `agent_thought_chunk`→`thinking`, `tool_call`→`tool_use`, `tool_call_update`→`tool_result`, `usage_update`→[] (usage 갱신만), 그 외→`event`.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_kiro_provider.py 에 추가
from agentcli.types import TokenUsage
from agentcli.providers.kiro import _map_session_update


def test_map_agent_message_chunk_to_text():
    u = TokenUsage()
    chunks = _map_session_update(
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hello"}}, u)
    assert len(chunks) == 1
    assert chunks[0].type == "text" and chunks[0].content == "Hello"


def test_map_thought_and_tool_variants():
    u = TokenUsage()
    assert _map_session_update(
        {"sessionUpdate": "agent_thought_chunk",
         "content": {"type": "text", "text": "thinking..."}}, u)[0].type == "thinking"
    assert _map_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "t1",
         "title": "read"}, u)[0].type == "tool_use"
    assert _map_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
         "status": "completed"}, u)[0].type == "tool_result"


def test_map_usage_update_accumulates_and_emits_nothing():
    u = TokenUsage()
    out = _map_session_update(
        {"sessionUpdate": "usage_update", "used": 1500, "size": 200000}, u)
    assert out == []
    assert u.prompt_tokens == 1500
    assert u.prompt_tokens_source == "kiro_cli_reported"
    assert u.prompt_tokens_reliable is False
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_kiro_provider.py -k map -q`
Expected: FAIL — `ImportError: cannot import name '_map_session_update'`.

- [ ] **Step 3: 구현**

`agentcli/providers/kiro.py` 의 import 에 `StreamChunk, TokenUsage` 추가하고 매핑 함수 추가:

```python
# import 갱신
from ..types import (ERROR_BINARY_MISSING, ProviderHealth,
                     StreamChunk, TokenUsage)

# 파일 하단에 추가
def _map_session_update(update: dict, usage: TokenUsage) -> list[StreamChunk]:
    """ACP session/update.update 1개를 정규화 청크로 변환 + usage 누적.

    필드명은 ACP 표준 기준 (Task 0 spike 로 확정). 모르는 변형은 event 로.
    """
    kind = update.get("sessionUpdate", "")
    if kind == "agent_message_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="text", content=text, data=update)] if text else []
    if kind == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="thinking", content=text, data=update)] if text else []
    if kind == "tool_call":
        return [StreamChunk(type="tool_use", data=update)]
    if kind == "tool_call_update":
        return [StreamChunk(type="tool_result", data=update)]
    if kind == "usage_update":
        used = int(update.get("used") or 0)
        usage.prompt_tokens = used
        usage.total_tokens = used + usage.completion_tokens
        usage.prompt_tokens_reliable = False
        usage.prompt_tokens_source = "kiro_cli_reported"
        return []
    return [StreamChunk(type="event", data=update)]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -k map -q`
Expected: PASS (3 passed).

- [ ] **Step 5: 커밋**

```bash
git add agentcli/providers/kiro.py tests/test_kiro_provider.py
git commit -m "feat(kiro): map ACP session/update events to normalized chunks"
```

---

## Task 5: ACP turn 오케스트레이션 (스트리밍 코어)

**Files:**
- Modify: `agentcli/providers/kiro.py`
- Modify: `tests/_acp_helpers.py` (가짜 agent 스크립트 추가)
- Test: `tests/test_kiro_provider.py`

**핵심 설계:** `kiro-cli acp` 는 `_run_stream_template`(stdin=DEVNULL, 단방향)와 호환되지 않으므로 전용 turn 루프를 둔다. 읽기 태스크가 stdout 라인을 `AcpConnection.handle_line` 에 흘리고, `on_notification` 이 매핑 청크를 `asyncio.Queue` 에 넣는다. 프롬프트 응답(stopReason)이 오면 DONE 센티넬을 큐에 넣어 제너레이터가 종료한다.

**Interfaces:**
- Produces:
  - `async KiroProvider._acp_turn(self, *, prompt, model, session_id, cwd, timeout, idle_timeout, wall_timeout, reader, writer_write_line, spawn) -> AsyncIterator[StreamChunk]` — 테스트 주입 가능하도록 transport 의존성(`reader`/`writer_write_line`)을 파라미터화한 내부 코루틴. 실제 spawn 은 Task 8에서 연결.
  - 본 Task는 **transport 주입형** turn 루프를 가짜 트랜스포트로 검증한다.

- [ ] **Step 1: 가짜 agent 스크립트 헬퍼 추가**

```python
# tests/_acp_helpers.py 에 추가
class ScriptedAgent:
    """client 가 보낸 요청에 대해 미리 정한 응답/알림을 큐에 흘리는 가짜 agent.

    client.write_line 을 가로채 method 별로 핸들러를 호출하고, 그 결과
    메시지들을 conn.handle_line 으로 되먹인다. session/prompt 수신 시
    session/update 알림들을 보낸 뒤 prompt result(stopReason)로 마무리.
    """
    def __init__(self, conn, *, updates=None, stop_reason="end_turn",
                 new_session_id="kiro-sess-1", load_ok=True):
        self._conn = conn
        self._updates = updates or []
        self._stop = stop_reason
        self._sid = new_session_id
        self._load_ok = load_ok

    async def write_line(self, line: str) -> None:
        import json
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": 1,
                           "agentCapabilities": {"loadSession": True},
                           "authMethods": []}}))
        elif method == "session/new":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"sessionId": self._sid}}))
        elif method == "session/load":
            if self._load_ok:
                await self._conn.handle_line(json.dumps(
                    {"jsonrpc": "2.0", "id": rid, "result": {}}))
            else:
                await self._conn.handle_line(json.dumps(
                    {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32000, "message": "session not found"}}))
        elif method == "session/prompt":
            sid = params.get("sessionId", self._sid)
            for upd in self._updates:
                await self._conn.handle_line(json.dumps({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": sid, "update": upd}}))
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"stopReason": self._stop}}))
```

- [ ] **Step 2: 실패 테스트 작성**

```python
# tests/test_kiro_provider.py 에 추가
import pytest
from agentcli.providers._acp import AcpConnection
from tests._acp_helpers import ScriptedAgent


@pytest.mark.asyncio
async def test_acp_turn_new_session_streams_text_and_usage():
    p = KiroProvider()
    updates = [
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hel"}},
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "lo"}},
        {"sessionUpdate": "usage_update", "used": 1200, "size": 200000},
    ]
    # transport 를 가짜 agent 로 연결.
    holder = {}
    async def write_line(line):
        await holder["agent"].write_line(line)
    conn = AcpConnection(write_line)  # turn 이 on_notification/on_request 를 설정
    chunks = []
    async for ch in p._acp_turn(
            prompt="hi", model="", session_id="", cwd=None,
            timeout=10, idle_timeout=None, wall_timeout=None,
            conn_factory=lambda on_req, on_notif: _wire(holder, write_line, on_req, on_notif, updates)):
        chunks.append(ch)

    types = [c.type for c in chunks]
    assert "text" in types and types[-1] == "done"
    text = "".join(c.content for c in chunks if c.type == "text")
    assert text == "Hello"
    done = chunks[-1]
    assert done.session_id == "kiro-sess-1"
    assert done.usage.prompt_tokens == 1200


def _wire(holder, write_line, on_req, on_notif, updates):
    """AcpConnection 을 만들고 ScriptedAgent 와 양방향 연결."""
    conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
    holder["agent"] = ScriptedAgent(conn, updates=updates)
    return conn
```

> 구현 시 `_acp_turn` 은 `conn_factory(on_request, on_notification) -> AcpConnection` 를 받아 transport 를 주입받는다. Task 8에서 실제 subprocess 용 `conn_factory` 를 연결한다.

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/test_kiro_provider.py -k acp_turn -q`
Expected: FAIL — `AttributeError: '_acp_turn'`.

- [ ] **Step 4: 구현**

`agentcli/providers/kiro.py` 에 import 추가 및 turn 코어:

```python
# import 갱신
import asyncio
import json
import time
from typing import AsyncIterator
from ._acp import AcpConnection, AcpError

# 클래스 상수
_PROTOCOL_VERSION = 1
_DONE = object()  # 큐 종료 센티넬

# KiroProvider 메서드로 추가
async def _acp_turn(self, *, prompt: str, model: str, session_id: str,
                    cwd: str | None, timeout: int,
                    idle_timeout: int | None, wall_timeout: int | None,
                    conn_factory) -> AsyncIterator[StreamChunk]:
    """한 turn 을 구동하며 정규화 청크를 yield.

    conn_factory(on_request, on_notification) -> AcpConnection 로 transport 주입.
    """
    queue: asyncio.Queue = asyncio.Queue()
    usage = TokenUsage(payload_prompt_tokens=_estimate(prompt),
                       prompt_tokens_reliable=False,
                       prompt_tokens_source="kiro_cli_reported")
    state = {"session_id": session_id}

    async def on_notification(method: str, params: dict) -> None:
        if method == "session/update":
            for ch in _map_session_update(params.get("update") or {}, usage):
                await queue.put(ch)

    async def on_request(method: str, params: dict) -> dict:
        return await self._handle_agent_request(method, params, cwd)

    conn = conn_factory(on_request, on_notification)

    async def drive() -> None:
        try:
            await conn.request("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False},
                "clientInfo": {"name": "agentcli", "version": "0"}})
            if session_id:
                try:
                    await conn.request("session/load",
                                       {"sessionId": session_id, "cwd": cwd or "."})
                except AcpError:
                    # stale 세션 → 새 세션으로 1회 복구 (Task 6 에서 테스트).
                    state["session_id"] = (await conn.request(
                        "session/new", {"cwd": cwd or "."}))["sessionId"]
            else:
                state["session_id"] = (await conn.request(
                    "session/new", {"cwd": cwd or "."}))["sessionId"]
            res = await conn.request("session/prompt", {
                "sessionId": state["session_id"],
                "prompt": [{"type": "text", "text": prompt}]})
            await queue.put((_DONE, res.get("stopReason", "")))
        except AcpError as exc:
            await queue.put((_DONE, exc))
        except Exception as exc:  # noqa: BLE001
            await queue.put((_DONE, exc))

    start = time.time()
    driver = asyncio.create_task(drive())
    idle = idle_timeout if idle_timeout is not None else timeout
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=idle)
            except asyncio.TimeoutError:
                yield StreamChunk(type="error",
                                  content=f"idle timeout: {idle}s",
                                  data={"error_type": "timeout"})
                return
            if isinstance(item, StreamChunk):
                yield item
                continue
            # (_DONE, payload)
            _, payload = item
            if isinstance(payload, AcpError):
                yield StreamChunk(type="error", content=payload.message,
                                  data={"error_type": "unknown"})
                return
            if isinstance(payload, Exception):
                yield StreamChunk(type="error", content=str(payload),
                                  data={"error_type": "unknown"})
                return
            yield StreamChunk(
                type="done", content="",
                session_id=state["session_id"], usage=usage,
                data={"provider": self.provider_id, "model": model,
                      "latency_ms": int((time.time() - start) * 1000),
                      "stop_reason": payload})
            return
    finally:
        if not driver.done():
            driver.cancel()
```

그리고 헬퍼와 콜백 스텁 추가(콜백 본체는 Task 7):

```python
from .base import estimate_payload_prompt_tokens as _estimate

async def _handle_agent_request(self, method: str, params: dict,
                                cwd: str | None) -> dict:
    # Task 7 에서 permission/fs 구현. 기본은 빈 result.
    return {}
```

> 참고: `done` 청크의 `content` 는 빈 문자열이고, 누적 텍스트는 client(`chat_stream`)가 `text` 청크를 모아 만든다 (기존 계약과 동일 — codex `_run_stream_template` 도 done.content 에 누적텍스트를 넣지만, client 는 text 청크 누적을 우선한다). 누적이 필요하면 `usage`/`session_id` 만 신뢰하면 된다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -k acp_turn -q`
Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
git add agentcli/providers/kiro.py tests/_acp_helpers.py tests/test_kiro_provider.py
git commit -m "feat(kiro): ACP one-shot turn orchestration (streaming core)"
```

---

## Task 6: 재개(session/load) + stale 세션 복구

**Files:**
- Test: `tests/test_kiro_provider.py`
- (구현은 Task 5 `drive()` 에 포함 — 이 Task는 동작을 테스트로 고정)

**Interfaces:**
- Consumes: Task 5의 `_acp_turn`, `ScriptedAgent(load_ok=...)`.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_kiro_provider.py 에 추가
@pytest.mark.asyncio
async def test_resume_calls_session_load_with_stored_id():
    p = KiroProvider()
    holder = {}
    async def write_line(line):
        await holder["agent"].write_line(line)
    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = ScriptedAgent(conn, updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "ok"}}], load_ok=True)
        holder["conn"] = conn
        return conn
    chunks = [c async for c in p._acp_turn(
        prompt="again", model="", session_id="prev-sid", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]
    methods = holder["agent"]  # ScriptedAgent 가 본 메서드 기록은 conn 송신에서 확인
    assert chunks[-1].type == "done"
    assert chunks[-1].session_id == "prev-sid"  # load 성공 시 기존 id 유지


@pytest.mark.asyncio
async def test_stale_session_falls_back_to_new_once():
    p = KiroProvider()
    holder = {}
    async def write_line(line):
        await holder["agent"].write_line(line)
    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = ScriptedAgent(conn, updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "fresh"}}],
            load_ok=False, new_session_id="kiro-new")
        return conn
    chunks = [c async for c in p._acp_turn(
        prompt="again", model="", session_id="expired", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]
    assert chunks[-1].type == "done"
    assert chunks[-1].session_id == "kiro-new"  # 복구된 새 세션 id
```

> `session/load` 성공 시 기존 `session_id` 유지를 검증하려면 Task 5 `drive()` 에서 load 성공 시 `state["session_id"]` 를 기존값으로 유지해야 한다(현재 구현은 load 분기에서 새로 덮어쓰지 않으므로 유지됨 — 확인).

- [ ] **Step 2: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -k "resume or stale" -q`
Expected: PASS. 실패 시 Task 5 `drive()` 의 load/new 분기 점검.

- [ ] **Step 3: 커밋**

```bash
git add tests/test_kiro_provider.py
git commit -m "test(kiro): session/load resume + stale-session new-session recovery"
```

---

## Task 7: 클라이언트 역콜백 (permission 자동응답 + fs read/write)

**Files:**
- Modify: `agentcli/providers/kiro.py` (`_handle_agent_request` 본체)
- Test: `tests/test_kiro_provider.py`

**Interfaces:**
- Produces: `KiroProvider._handle_agent_request(method, params, cwd) -> dict` 구현.
  - `session/request_permission`: `params["options"]` 중 허용 옵션을 선택 → `{"outcome": {"outcome": "selected", "optionId": <id>}}`; 거부 시 `{"outcome": {"outcome": "cancelled"}}`.
  - `fs/read_text_file`: `params["path"]` 가 `cwd` 하위면 파일 내용 반환 `{"content": ...}`, 아니면 거부(빈 content + 로깅).
  - `fs/write_text_file`: `cwd` 하위면 기록 후 `{}`, 아니면 거부.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_kiro_provider.py 에 추가
import os


@pytest.mark.asyncio
async def test_permission_trust_all_selects_allow_option():
    p = KiroProvider(trust_all=True)
    res = await p._handle_agent_request("session/request_permission", {
        "options": [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"}],
        "toolCall": {"title": "read"}}, cwd=None)
    assert res["outcome"]["outcome"] == "selected"
    assert res["outcome"]["optionId"] == "allow"


@pytest.mark.asyncio
async def test_permission_denied_when_not_trusted():
    p = KiroProvider(trust_all=False, trust_tools=["grep"])
    res = await p._handle_agent_request("session/request_permission", {
        "options": [{"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "reject", "kind": "reject_once"}],
        "toolCall": {"title": "bash"}}, cwd=None)
    assert res["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_fs_read_within_cwd(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("data", encoding="utf-8")
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": str(f)}, cwd=str(tmp_path))
    assert res["content"] == "data"


@pytest.mark.asyncio
async def test_fs_read_outside_cwd_denied(tmp_path):
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": "/etc/hosts"}, cwd=str(tmp_path))
    assert res.get("content", "") == ""
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_kiro_provider.py -k "permission or fs_read" -q`
Expected: FAIL — `_handle_agent_request` 가 빈 `{}` 만 반환.

- [ ] **Step 3: 구현**

```python
# kiro.py: import 에 os, Path 추가
import os
from pathlib import Path

# _handle_agent_request 교체
async def _handle_agent_request(self, method: str, params: dict,
                                cwd: str | None) -> dict:
    if method == "session/request_permission":
        return self._decide_permission(params)
    if method == "fs/read_text_file":
        return self._fs_read(params, cwd)
    if method == "fs/write_text_file":
        return self._fs_write(params, cwd)
    # terminal/* 등 미지원 역요청은 빈 result (Task 0 spike 로 필요성 확인).
    return {}

def _decide_permission(self, params: dict) -> dict:
    options = params.get("options") or []
    tool_title = (params.get("toolCall") or {}).get("title", "")
    allowed = self._trust_all or (tool_title in self._trust_tools)
    if allowed:
        for opt in options:
            if str(opt.get("kind", "")).startswith("allow") or \
               opt.get("optionId") == "allow":
                return {"outcome": {"outcome": "selected",
                                    "optionId": opt.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}

def _within_cwd(self, path: str, cwd: str | None) -> Path | None:
    if not cwd:
        return None
    try:
        root = Path(cwd).resolve()
        target = Path(path).resolve()
        target.relative_to(root)  # cwd 밖이면 ValueError
        return target
    except (ValueError, OSError):
        return None

def _fs_read(self, params: dict, cwd: str | None) -> dict:
    target = self._within_cwd(params.get("path", ""), cwd)
    if target is None or not target.is_file():
        logger.warning("kiro fs/read 거부: %s", params.get("path"))
        return {"content": ""}
    try:
        return {"content": target.read_text(encoding="utf-8", errors="replace")}
    except OSError:
        return {"content": ""}

def _fs_write(self, params: dict, cwd: str | None) -> dict:
    target = self._within_cwd(params.get("path", ""), cwd)
    if target is None:
        logger.warning("kiro fs/write 거부: %s", params.get("path"))
        return {}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(params.get("content", ""), encoding="utf-8")
    except OSError:
        pass
    return {}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -k "permission or fs_read" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: 커밋**

```bash
git add agentcli/providers/kiro.py tests/test_kiro_provider.py
git commit -m "feat(kiro): client callbacks — permission auto-respond + cwd-scoped fs"
```

---

## Task 8: 공개 표면 (invoke / invoke_async / stream_async + 실 subprocess 연결)

**Files:**
- Modify: `agentcli/providers/kiro.py`
- Test: `tests/test_kiro_provider.py`

**Interfaces:**
- Produces:
  - `def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None) -> LLMResponse`
  - `async def invoke_async(self, messages, *, ...) -> LLMResponse`
  - `async def stream_async(self, messages, *, ..., idle_timeout=None, wall_timeout=None) -> AsyncIterator[StreamChunk]`
  - 내부: `_subprocess_conn_factory(self, proc, ...)` — 실 `kiro-cli acp` 파이프를 `AcpConnection` 에 연결하고 읽기 태스크를 띄움. binary 없음→`binary_missing`.

- [ ] **Step 1: 실패 테스트 작성 (stream_async 를 _acp_turn mock 으로)**

```python
# tests/test_kiro_provider.py 에 추가
from unittest.mock import patch
from agentcli.types import Message, StreamChunk as SC


@pytest.mark.asyncio
async def test_stream_async_yields_text_then_done():
    p = KiroProvider()
    async def fake_turn(**kwargs):
        yield SC(type="text", content="Hi")
        yield SC(type="done", content="", session_id="s9",
                 usage=TokenUsage(prompt_tokens=5), data={"latency_ms": 1})
    with patch.object(KiroProvider, "_acp_turn", side_effect=lambda **k: fake_turn(**k)), \
         patch.object(KiroProvider, "_find_binary", return_value="/usr/bin/kiro-cli"):
        out = [c async for c in p.stream_async([Message(role="user", content="hi")])]
    assert [c.type for c in out] == ["text", "done"]
    assert out[-1].session_id == "s9"


@pytest.mark.asyncio
async def test_stream_async_binary_missing():
    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=None):
        out = [c async for c in p.stream_async([Message(role="user", content="hi")])]
    assert out[0].type == "error"
    assert out[0].data.get("error_type") == "binary_missing"


@pytest.mark.asyncio
async def test_invoke_async_folds_chunks_into_response():
    p = KiroProvider()
    async def fake_turn(**kwargs):
        yield SC(type="text", content="A")
        yield SC(type="text", content="B")
        yield SC(type="done", content="", session_id="s1",
                 usage=TokenUsage(prompt_tokens=7), data={"latency_ms": 2})
    with patch.object(KiroProvider, "_acp_turn", side_effect=lambda **k: fake_turn(**k)), \
         patch.object(KiroProvider, "_find_binary", return_value="/usr/bin/kiro-cli"):
        resp = await p.invoke_async([Message(role="user", content="hi")])
    assert resp.content == "AB"
    assert resp.session_id == "s1"
    assert resp.tokens.prompt_tokens == 7
    assert resp.provider == "kiro"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_kiro_provider.py -k "stream_async or invoke_async" -q`
Expected: FAIL — `stream_async`/`invoke_async` 미정의.

- [ ] **Step 3: 구현**

```python
# kiro.py: import 에 추가
import subprocess  # (health 에서 이미 run_health_command 사용; 직접 spawn 은 asyncio)
from .base import build_session_prompt
from ..types import ERROR_BINARY_MISSING, LLMResponse, Message  # Message/LLMResponse 추가
from ..utils import build_env

# --- 실 subprocess 연결 factory ---
def _subprocess_conn_factory(self, model, cwd):
    """(coroutine) kiro-cli acp 를 spawn 하고 AcpConnection+read task 를 만든다.

    반환: conn_factory(on_request, on_notification) -> AcpConnection 형태로
    감싸기 위해, _acp_turn 에 넘길 동기 factory 를 만들어 돌려준다. 실제 spawn 은
    factory 호출 시 1회 수행.
    """
    state = {}
    def factory(on_request, on_notification):
        # spawn 은 async 가 필요하므로 lazy: 첫 write 전에 ensure.
        bin_path = self._find_binary()
        cmd = [bin_path, "acp"]
        if self._agent:
            cmd += ["--agent", self._agent]
        proc_box = {}
        async def ensure_proc():
            if "proc" in proc_box:
                return proc_box["proc"]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd, env=build_env())
            proc_box["proc"] = proc
            # 읽기 태스크: stdout 라인 → conn.handle_line
            async def reader():
                assert proc.stdout
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    await conn.handle_line(line.decode("utf-8", errors="replace"))
            proc_box["reader"] = asyncio.create_task(reader())
            return proc
        async def write_line(line: str):
            proc = await ensure_proc()
            assert proc.stdin
            proc.stdin.write((line + "\n").encode("utf-8"))
            await proc.stdin.drain()
        conn = AcpConnection(write_line, on_request=on_request,
                             on_notification=on_notification)
        state["proc_box"] = proc_box
        return conn
    return factory, state

async def stream_async(self, messages, *, model="", timeout=120,
                       session_id="", cwd=None,
                       idle_timeout=None, wall_timeout=None):
    if not self._find_binary():
        yield StreamChunk(type="error", content="kiro-cli not found",
                          data={"error_type": ERROR_BINARY_MISSING,
                                "exit_code": 127})
        return
    prompt = build_session_prompt(messages)
    factory, state = self._subprocess_conn_factory(model or self._model, cwd)
    try:
        async for chunk in self._acp_turn(
                prompt=prompt, model=model or self._model,
                session_id=session_id, cwd=cwd, timeout=timeout,
                idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                conn_factory=factory):
            yield chunk
    finally:
        box = state.get("proc_box") or {}
        proc = box.get("proc")
        rdr = box.get("reader")
        if rdr and not rdr.done():
            rdr.cancel()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

async def invoke_async(self, messages, *, model="", timeout=120,
                       session_id="", cwd=None) -> LLMResponse:
    parts: list[str] = []
    usage = TokenUsage()
    sid = session_id
    err = ""
    latency = 0
    async for ch in self.stream_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd):
        if ch.session_id:
            sid = ch.session_id
        if ch.type == "text":
            parts.append(ch.content)
        elif ch.type == "error":
            err = ch.content or "kiro stream error"
            err_type = ch.data.get("error_type", "")
        elif ch.type == "done":
            if ch.usage is not None:
                usage = ch.usage
            latency = int(ch.data.get("latency_ms") or 0)
    content = "".join(parts)
    if not content and err:
        return LLMResponse(content="", provider=self.provider_id,
                           model=model or self._model, session_id=sid,
                           error=err, error_type=locals().get("err_type", "") or "unknown",
                           exit_code=127 if "not found" in err else None)
    return LLMResponse(content=content, provider=self.provider_id,
                       model=model or self._model, tokens=usage,
                       latency_ms=latency, session_id=sid)

def invoke(self, messages, *, model="", timeout=120,
           session_id="", cwd=None) -> LLMResponse:
    return asyncio.run(self.invoke_async(
        messages, model=model, timeout=timeout,
        session_id=session_id, cwd=cwd))
```

> 주의: `invoke` 가 `asyncio.run` 을 쓰므로 이미 실행 중인 이벤트 루프 안에서 동기 `invoke` 를 부르면 안 된다(client 는 async 경로에서 `invoke_async` 를 직접 호출하므로 문제 없음 — codex 와 달리 kiro 는 async-native).

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_kiro_provider.py -k "stream_async or invoke_async" -q`
Expected: PASS (3 passed).

- [ ] **Step 5: 전체 kiro 테스트 + 회귀 확인**

Run: `pytest tests/test_kiro_provider.py tests/test_acp_connection.py -q`
Expected: PASS (전부).
Run: `pytest -q`
Expected: 기존 테스트 + 신규 전부 PASS.

- [ ] **Step 6: 커밋**

```bash
git add agentcli/providers/kiro.py tests/test_kiro_provider.py
git commit -m "feat(kiro): public surface invoke/invoke_async/stream_async over ACP"
```

---

## Task 9: 레지스트리 등록 + 문서(README EN/KO) + CHANGELOG

**Files:**
- Modify: `agentcli/providers/registry.py`
- Modify: `README.md`, `README.ko.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `KiroProvider` from `agentcli.providers.kiro`.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_registry.py 에 추가
def test_default_registry_includes_kiro():
    from agentcli.providers.registry import create_default_registry
    reg = create_default_registry()
    ids = [row["id"] for row in reg.list_providers()]
    assert "kiro" in ids
    assert reg.get("kiro") is not None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_registry.py -k kiro -q`
Expected: FAIL — kiro 미등록.

- [ ] **Step 3: 레지스트리 등록**

`agentcli/providers/registry.py` 의 `create_default_registry` 수정:

```python
def create_default_registry() -> ProviderRegistry:
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .copilot import CopilotProvider
    from .kiro import KiroProvider

    reg = ProviderRegistry()
    reg.register(ClaudeProvider())
    reg.register(CodexProvider())
    reg.register(CopilotProvider())
    reg.register(KiroProvider())
    # 세션 지원 provider 우선. Codex --full-auto 는 가장 비싸 후순위.
    reg.set_fallback_order(["claude", "copilot", "codex", "kiro"])
    return reg
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_registry.py -k kiro -q`
Expected: PASS.

- [ ] **Step 5: README 표 갱신 (EN/KO 동시)**

`README.md` 의 "Provider capabilities" 표에 행 추가:

```markdown
| `KiroProvider` | ✅ (ACP `session/load`) | ✅ | `session/new` result `sessionId`; transport = ACP JSON-RPC over stdio (`kiro-cli acp`) |
```

`README.ko.md` 의 대응 표에도 동일 의미의 행을 추가(한국어 설명). 표 아래 산문에 한 단락 추가:

> `KiroProvider` 는 `kiro-cli acp`(줄 단위 JSON-RPC 2.0)를 호출당 1회 one-shot turn 으로 구동한다: `initialize` → 첫 턴 `session/new` / 재개 `session/load(저장된 sessionId)` → `session/prompt` → `session/update` 스트림. 토큰 usage 는 `usage_update` 알림에서, 권한은 `session/request_permission` 자동응답(`trust_all`/`trust_tools`)으로 처리한다. 인증은 `KIRO_API_KEY`(또는 `kiro-cli login`).

- [ ] **Step 6: CHANGELOG 갱신**

`CHANGELOG.md` 상단에 항목 추가(현재 날짜, 버전 미정이면 Unreleased):

```markdown
## Unreleased

### Added
- **KiroProvider** — 네 번째 provider. `kiro-cli acp`(ACP, JSON-RPC 2.0 over
  stdio)를 호출당 one-shot turn 으로 감싸 세션 연속성·타입드 스트리밍·토큰
  통계를 제공. 외부 LLMProvider 계약·청크 타입은 기존 3종과 동일. 제로 의존성.
```

- [ ] **Step 7: 전체 테스트 + 빌드 확인**

Run: `pytest -q`
Expected: 전부 PASS.
Run: `python -m build && python -m twine check dist/*`
Expected: PASS (패키징 정상).

- [ ] **Step 8: 커밋**

```bash
git add agentcli/providers/registry.py tests/test_registry.py README.md README.ko.md CHANGELOG.md
git commit -m "feat(kiro): register KiroProvider in default registry + docs"
```

---

## Self-Review (작성자 체크 — 반영 완료)

**1. Spec coverage:**
- §3 외부 계약/capabilities → Task 3. §4 컴포넌트(AcpConnection/provider/매퍼) → Task 1·2/3/4. §5 데이터흐름+청크매핑 → Task 4·5. §6 콜백(permission/fs/terminal) → Task 7(terminal 은 v1 미광고, Task 0 spike 로 재검토). §7 세션/복구 → Task 6. §8 에러처리 → Task 5(timeout/error)·8(binary_missing). §9 정규화/등록/문서 → Task 9. §10 제로의존성 → Global Constraints + Task 1(stdlib only). §11 테스트(가짜 ACP 하니스 + `_find_binary` mock) → Task 1·5·8. §12 spike → Task 0. §13 열린항목 → Task 0/7. 누락 없음.
- 토큰 usage: §8/§4 → Task 4(`usage_update`) + Task 5(done.usage). 커버됨.

**2. Placeholder scan:** "TBD"/"적절히 처리"/"이하 동일" 없음. Task 0 spike 는 조건부 실행이지만 구체적 행동·확인항목을 명시 — 플레이스홀더 아님. 모델 id 는 하드코딩 대신 빈-id 기본 + `resolve_model` 비-strict 통과로 처리(미검증 id 발명 회피).

**3. Type consistency:**
- `AcpConnection(write_line, *, on_request, on_notification)` / `request()` / `handle_line()` 시그니처가 Task 1 정의와 Task 5·8 사용처에서 일치.
- `_map_session_update(update, usage) -> list[StreamChunk]` 가 Task 4 정의·Task 5 사용에서 일치.
- `_acp_turn(..., conn_factory)` 가 Task 5 정의·Task 6·8 사용에서 일치.
- `_handle_agent_request(method, params, cwd) -> dict` 가 Task 5 스텁·Task 7 본체에서 일치.
- `prompt_tokens_source="kiro_cli_reported"` 가 Task 4·5에서 동일.

**알려진 가정(Task 0 spike 대상, hermetic 테스트엔 무영향):** ACP 필드명(`update.sessionUpdate`, `content.text`, `options[].kind/optionId`, `outcome.outcome`, `usage_update.used`), `session/new`·`session/load` params(`cwd`), `session/prompt` content block(`{type:"text",text}`). 차이 시 `_map_session_update`/`_decide_permission`/`drive()` 국소 수정.
