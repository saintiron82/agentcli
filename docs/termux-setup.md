# Running agentcli on Termux (Android)

[English](termux-setup.md) | [한국어](termux-setup.ko.md)

> agentcli is a thin wrapper that drives the `claude` / `codex` / `copilot`
> external CLIs as subprocesses. So the real requirement for "run agentcli on a
> phone" is **not Python** — it's that those CLIs install, authenticate, and run
> on the device. Android works in practice because Termux gives you a near-full
> POSIX environment (including `setsid`/`killpg`).

Target: arm64 Android (most modern phones). iOS is out — its sandbox forbids
spawning arbitrary binaries / subprocesses outside the app sandbox.

---

## 0. Install Termux — not the Play Store build

The Google Play Termux is **outdated and broken.** Install from one of:

- **F-Droid**: <https://f-droid.org/packages/com.termux/>
- **GitHub Releases**: <https://github.com/termux/termux-app/releases>

First run:

```bash
pkg update && pkg upgrade -y
```

---

## 1. Base packages

```bash
pkg install -y python nodejs git gh openssh
# optional: if you need access to phone storage
termux-setup-storage
```

Verify:

```bash
python --version   # must be 3.11+ (agentcli requirement)
node --version
git --version
```

---

## 2. Install the provider CLIs (the step that matters most)

agentcli looks up `claude` · `codex` · `copilot` (or `gh copilot`) on `PATH`.
You don't need all three — **install only the providers you'll use.**

Follow each CLI's official install docs for the canonical command (they change by
version). The commonly-working forms:

### 2-a. Claude Code (`claude`) — pure JS, most reliable on Termux

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### 2-b. GitHub Copilot (`gh copilot`) — gh extension; device-flow auth fits a phone

```bash
gh auth login        # prefer "Login with a web browser" / device code
gh extension install github/gh-copilot
gh copilot --version
```

### 2-c. Codex (`codex`) — ⚠️ native binary caveat

```bash
npm install -g @openai/codex
codex --version
```

> **Caveat**: the codex CLI may ship a native (Rust) binary, so **install/run can
> fail when there's no arm64-Android prebuilt.** If it does, skip codex on Termux
> and run claude/copilot only. (This is the most fragile of the three.)

---

## 3. Authenticate each CLI (subscription login — not API keys)

agentcli **holds no credentials.** Log into each CLI on the device yourself. The
canonical commands a failing health check points you to:

```bash
claude auth login     # Claude Pro/Max subscription OAuth (opens a browser)
codex login           # ChatGPT subscription
copilot login         # or already done via the gh auth login above
```

If a browser callback is awkward on a phone-only setup, pick the **device-code
flow** (shows a code, approve it in any browser) — it works headless.
`gh auth login` is the cleanest example of this.

---

## 4. Install agentcli

```bash
pip install "agentcli @ git+https://github.com/saintiron82/agentcli.git@v0.6.4"
```

> Zero runtime dependencies, so the pip step is light. The import name is
> `agentcli`.

---

## 5. Verify

Start with a health check for CLI/auth state:

```bash
python - <<'PY'
from agentcli import LLMClient, MemoryStore
c = LLMClient(store=MemoryStore())
h = c.health_check("claude")
print("ok:", h.ok, "| msg:", h.message, "| do:", h.suggested_action)
PY
```

If `ok: True`, do a real one-shot call:

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

If you get `termux-ok`, agentcli lives on the phone.

---

## 6. (Optional) "Open a server" — stdlib only, zero deps

agentcli has no built-in server, so add a thin HTTP layer yourself. Standard
library only:

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
# from elsewhere:
curl -s localhost:8765 -d '{"prompt":"say hi","provider":"claude"}'
```

To reach it safely from outside the phone, don't expose the port directly — put
it on a private mesh with **Tailscale** (`pkg install tailscale`, then
`tailscaled` + `tailscale up`) and call the phone's tailnet IP.

---

## 7. Troubleshooting / phone-specific traps

| Symptom | Cause / fix |
|---------|-------------|
| `claude: command not found` | npm global bin not on PATH → check `npm config get prefix`, add the global bin dir to `PATH` |
| codex install/run fails | no arm64-Android native build (see 2-c). Operate without codex |
| Calls are very slow | the latency floor is CLI harness boot (structural). For single completions, mitigate with **lean mode**. Especially noticeable on phone CPUs |
| Dies in the background | Android kills background processes → `termux-wake-lock` for a wakelock, and exempt Termux from battery optimization |
| OAuth browser callback fails | use the device-code flow (`gh auth login` is the model), or callback in the same phone browser |
| Session doesn't resume | keep `owner`+`alias`+`cwd` identical across calls; `cwd` must be a real path under the Termux home |

---

## Key takeaways

1. Get Termux from **F-Droid/GitHub**.
2. Install **only the provider CLIs you'll use** — prefer claude (JS, stable) and copilot (gh extension, device-flow); **codex may break on arm64**.
3. **Subscription-login** each CLI (agentcli has no credentials).
4. `pip install ...@v0.6.4` for agentcli (zero deps).
5. Verify with a health check, then a one-shot call.
6. Add the server yourself with stdlib; expose via Tailscale.
7. Latency, battery, and background kills are the real enemies → lean mode + wakelock.
