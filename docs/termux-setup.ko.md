# Termux에서 agentcli 가동 (Android)

[English](termux-setup.md) | [한국어](termux-setup.ko.md)

> agentcli는 `claude`·`codex`·`copilot` 외부 CLI를 subprocess로 감싸는 얇은
> 래퍼다. 그래서 "폰에서 agentcli 가동"의 실제 조건은 **Python이 아니라** 이
> CLI들이 폰에서 설치·인증·실행되는 것이다. Android는 Termux로 POSIX 환경
> (setsid/killpg 포함)을 거의 그대로 얻을 수 있어 현실적으로 동작한다.

대상: arm64 Android(대부분의 최신폰). iOS는 샌드박스가 앱 밖 임의 바이너리
실행/subprocess spawn을 막아 이 경로가 막힌다.

---

## 0. Termux 설치 — Play 스토어 버전 쓰지 말 것

Google Play의 Termux는 **오래되고 망가져 있다.** 반드시 둘 중 하나에서 설치:

- **F-Droid**: <https://f-droid.org/packages/com.termux/>
- **GitHub Releases**: <https://github.com/termux/termux-app/releases>

설치 후 첫 실행:

```bash
pkg update && pkg upgrade -y
```

---

## 1. 기본 패키지

```bash
pkg install -y python nodejs git gh openssh
# 선택: 폰 저장소 접근이 필요하면
termux-setup-storage
```

확인:

```bash
python --version   # 3.11+ 이어야 함 (agentcli 요구사항)
node --version
git --version
```

---

## 2. 프로바이더 CLI 설치 (가장 중요한 단계)

agentcli는 PATH에서 `claude` · `codex` · `copilot`(또는 `gh copilot`)을 찾는다.
**셋 다 필요한 게 아니라, 쓸 프로바이더만** 설치하면 된다.

각 CLI의 설치 명령은 공식 문서를 따르는 게 정석이다(버전에 따라 바뀜). 아래는
흔히 동작하는 형태:

### 2-a. Claude Code (`claude`) — 순수 JS, Termux에서 가장 안정적

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### 2-b. GitHub Copilot (`gh copilot`) — gh 확장, device-flow 인증이라 폰에 적합

```bash
gh auth login        # → "Login with a web browser" / device code 방식 권장
gh extension install github/gh-copilot
gh copilot --version
```

### 2-c. Codex (`codex`) — ⚠️ 네이티브 바이너리 주의

```bash
npm install -g @openai/codex
codex --version
```

> **주의**: codex CLI는 네이티브(Rust) 바이너리를 포함할 수 있어 **arm64-Android
> 프리빌드가 없으면 설치/실행이 실패**할 수 있다. 실패하면 Termux에서 codex는
> 건너뛰고 claude/copilot만 쓰는 게 현실적이다. (셋 중 가장 깨지기 쉬운 지점이다.)

---

## 3. 각 CLI 인증 (구독 로그인 — API 키 아님)

agentcli는 **자격증명을 들고 있지 않는다.** 각 CLI를 폰에서 직접 로그인해야 한다.
health check가 실패하면 알려주는 정석 명령:

```bash
claude auth login     # Claude Pro/Max 구독 OAuth (브라우저 열림)
codex login           # ChatGPT 구독
copilot login         # 또는 위의 gh auth login 으로 이미 됨
```

폰 단독이라 브라우저 콜백이 번거로우면 **device-code 방식**(코드 보여주고 다른
기기/같은 폰 브라우저에서 승인)을 선택하면 헤드리스에서도 된다. `gh auth login`이
대표적으로 이 방식이 깔끔하다.

---

## 4. agentcli 설치

```bash
pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.6.4"
```

> 런타임 의존성 0개라 pip 단계는 가볍다. import 이름은 `agentcli`다.

---

## 5. 동작 검증

먼저 health check로 CLI/인증 상태부터:

```bash
python - <<'PY'
from agentcli import LLMClient, MemoryStore
c = LLMClient(store=MemoryStore())
h = c.health_check("claude")
print("ok:", h.ok, "| msg:", h.message, "| do:", h.suggested_action)
PY
```

`ok: True` 면 실제 한 줄 호출:

```bash
python - <<'PY'
import asyncio
from agentcli import LLMClient, MemoryStore

async def main():
    c = LLMClient(store=MemoryStore())
    r = await c.chat_async(
        "Reply with exactly: termux-ok",
        provider="claude",
        model=c.select_model("claude", "sonnet"),
        owner="phone", alias="smoke",
        wall_timeout=300,
    )
    print(r.content or r.error)

asyncio.run(main())
PY
```

`termux-ok` 가 나오면 폰에서 agentcli가 산다.

---

## 6. (선택) "서버로 열기" — stdlib만, 의존성 0

agentcli엔 내장 서버가 없으니 얇은 HTTP 레이어를 직접 얹는다. 표준 라이브러리만:

```python
# server.py
import asyncio, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from agentcli import LLMClient, MemoryStore

client = LLMClient(store=MemoryStore())

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        prompt = body.get("prompt", "")
        provider = body.get("provider", "claude")
        async def run():
            return await client.chat_async(
                prompt, provider=provider,
                model=client.select_model(provider, "sonnet"),
                owner="phone", alias=body.get("alias", "http"),
                wall_timeout=300,
            )
        r = asyncio.run(run())
        out = json.dumps({"content": r.content, "error": r.error}).encode()
        self.send_response(200 if r.content else 500)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(out)

ThreadingHTTPServer(("0.0.0.0", 8765), H).serve_forever()
```

```bash
python server.py
# 다른 곳에서:
curl -s localhost:8765 -d '{"prompt":"say hi","provider":"claude"}'
```

폰 밖에서 안전하게 접근하려면 포트를 직접 공개하지 말고 **Tailscale**
(`pkg install tailscale` 후 `tailscaled` + `tailscale up`)로 사설 메시에 올려
폰의 tailnet IP로 호출하는 걸 권장.

---

## 7. 트러블슈팅 / 폰 특유의 함정

| 증상 | 원인 / 해결 |
|------|-------------|
| `claude: command not found` | npm 글로벌 bin이 PATH에 없음 → `npm config get prefix` 확인, 글로벌 bin 디렉터리를 `PATH`에 추가 |
| codex 설치/실행 실패 | arm64-Android 네이티브 빌드 부재(2-c 주의). codex 제외하고 운영 |
| 호출이 너무 느림 | 지연 바닥값은 CLI 하네스 부팅(구조적). 단일 completion이면 **lean 모드**로 완화. 폰 CPU에선 특히 체감 큼 |
| 백그라운드에서 죽음 | Android가 백그라운드 프로세스를 종료 → `termux-wake-lock`로 wakelock, Termux 배터리 최적화 예외 설정 |
| OAuth 브라우저 콜백 실패 | device-code 방식 사용(`gh auth login`이 대표적), 또는 같은 폰 브라우저로 콜백 |
| 세션이 안 이어짐 | `owner`+`alias`+`cwd`를 호출마다 동일하게. `cwd`는 Termux 홈 아래 실제 경로로 |

---

## 핵심 요약

1. Termux는 **F-Droid/GitHub**에서.
2. **쓸 프로바이더 CLI만** 설치 — claude(JS, 안정) / copilot(gh extension, device-flow) 우선, **codex는 arm64 빌드 이슈로 깨질 수 있음**.
3. 각 CLI **구독 로그인**(agentcli는 자격증명 없음).
4. `pip install ...@v0.6.4` 로 agentcli(의존성 0).
5. health check → 한 줄 호출로 검증.
6. 서버는 stdlib로 직접 얹고, 외부 노출은 Tailscale.
7. 지연·배터리·백그라운드 종료가 실사용의 진짜 적 → lean 모드 + wakelock.
