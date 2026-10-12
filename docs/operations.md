# AgentCli operating runbook

[English](operations.md) | [한국어](operations.ko.md)

Scope: unreleased hardening branch based on v0.7.5. This is a deployment template,
not evidence that the user's servers satisfy it. Last reviewed: 2026-10-12.

## Trust and identity

For services sharing a machine, bind separate
[service auth profiles](service-auth.md) to their clients. Never switch the shared
machine login or process-global default client to select a service account.
Profile isolation is opt-in and does not replace OS/container boundaries.

AgentCli starts locally installed CLIs with the service account's permissions.
It is not an OS sandbox, authentication server or credential broker. Authenticate
users before deriving `owner`; never accept arbitrary owner/cwd/provider options
from an HTTP request. Administrative store APIs and direct provider calls do not
implement application authorization. Limit them to trusted host code.

Use separate OS identities or containers for different trust domains. Keep CLI
credentials, native histories, SQLite databases and diagnostic traces private.
On POSIX use private directories and restrictive file modes; on Windows restrict
ACLs to the service identity and designated administrators. Check inherited ACLs.
Do not commit credentials or raw responses. The parent environment, project
instructions, skills, MCP servers and hooks are executable trust inputs.
Read-only tools alone do not guarantee network isolation or prevent exfiltration;
use host egress controls where required.

## Restricted invocation example

```python
from pathlib import Path
from agentcli import LLMClient, SQLiteStore

workspace = str(Path("/srv/projects/review").resolve())
store = SQLiteStore("/srv/private/agentcli/state.db")  # create private parent first
client = LLMClient(store)
response = client.chat(
    "Review the code; do not modify files.", provider="codex",
    owner="authenticated-user-17", alias="review-only", cwd=workspace,
    provider_options={"sandbox_mode": "read-only", "approval_policy": "never"},
    fallback=False, schema_retries=0, timeout=120, wall_timeout=180,
)
# Check response.error_type; do not expose raw_stderr/error to untrusted users.
# Stop all clients before closing the shared store.
store.close()
```

The prompt is intent, not a security boundary. Verify the actual installed CLI's
sandbox behavior. Use a fresh alias for a different workspace. Restricted legacy
Codex sessions without recorded policy require `new_session=True`. Legacy
conversations without a workspace binding bind on first successful use: verify
their origin first or use a fresh alias. Copilot's native names for new sessions
are derived from the conversation ID, not the human-readable alias; existing
native session IDs remain unchanged. Unknown Windows batch launchers must be
replaced with a native binary, standard npm shim, or supported interpreter wrapper.

## Personal and single-host service

Use MemoryStore for a single application's transient handles. SQLiteStore keeps
handles across restarts and uses per-conversation sidecar locks for local writers.
Do not delete lock files while callers are active. Different memory-store objects
do not share state. Native histories still live in each CLI's own location.
Prefer stable absolute workspace paths and one CLI account per trust domain.

Inventory Python, AgentCli and all CLI versions; pin validated versions. Before
upgrading, run tests and offline `--version`/non-probe health checks, then an
approved restricted smoke test. Generation probes may cost quota and execute tools.

## Multiple servers

Choose one strategy per native session:

1. Route it to a single writer host with its own native histories and account.
2. Supply a tested distributed lease and make native histories available to the
   winning writer using a provider-supported mechanism.

Do not assume copying AgentCli's SQLite database copies native histories. OS file
locks are only validated for local filesystems, not distributed NFS/SMB service.
Separate local databases with the same alias create separate logical sessions.
Avoid shared-file SQLite unless its filesystem and workload are explicitly validated.

The optional `session_lock_factory(store, conversation_id)` returns a lease with
`acquire()` and `release()`. Methods must work across threads, have finite timeouts,
and release safely if acquisition raises. A host must implement ownership checks,
renewal, expiry and fencing/termination of stale native writers. A Redis key without
renewal/fencing is not a sufficient guarantee. This repository does not install a
distributed lock service. Custom-store instances pointing to the same backend need
an appropriate host lease identity.

Record these deployment fields before treating the service as production-ready:

| Field | Required evidence |
| --- | --- |
| Topology | hosts, filesystem types, session routing and failover strategy |
| Identity | authenticated owner mapping, OS accounts, credential ACLs |
| Boundaries | CLI sandbox/tool/network policy, approved MCP/hook/skill sources |
| Coordination | lease renewal/fencing tests or single-writer proof |
| Recovery | encrypted backup location, retention, restore drill and responsible operator |
| Observation | timeouts, exit categories, queue depth, failed/partial calls, quota alerts |

## Failures, retries and observability

Explicit `wall_timeout` shares one execution budget across fallbacks and retries.
Lease waiting and bounded cleanup can delay return beyond the budget; use host
queue limits and operational deadlines as well. Synchronous custom providers must
honor timeout. Windows Job Object assignment is best-effort and follows spawn;
strict containment requires a host-owned job/container. Watch assignment warnings.

Do not retry writes blindly. Tools may have executed before a timeout or invalid
schema response. Use idempotency keys and explicit approval for duplicate-sensitive
actions, disable fallback/schema retries, and inspect the actual target state.
Partial stream text followed by error is not a committed successful result.
Client streams may finish with `done.data.error=True`; always inspect errors.

Automatic logs omit raw MCP/config payloads and stderr. Returned errors/content
remain private data and are not a general secret-scrubbing guarantee. Trace schema
2 retains IDs, paths, tool names, times and lengths. Bound access and retention.
Token counters are estimates/CLI observations, not an exact billing ledger.

## Backup, retention and incident response

- Quiesce writers and close stores before offline SQLite copying; preserve required
  WAL/SHM files consistently, or use a tested SQLite online backup procedure.
- Back up native CLI history separately with provider-compatible procedures.
  Encrypt backups and retain account/workspace identity mappings without plaintext
  credentials in shared manifests. Test restores in an isolated environment.
- Clearing session metadata does not delete native CLI history. Choose retention
  periods separately for metadata, native history, traces and backups. Do not
  automate broad deletion or assume closing a store revokes credentials.
- On suspected exposure: pause the affected writer, isolate credentials/log access,
  preserve restricted evidence, rotate/revoke credentials through the provider,
  inspect executed actions, and restore only after reconciling side effects.

No server changes, credential rotation, native history deletion, remote push or
release publication are performed by this hardening work.

## Merge and release checks

Run the full suite on Windows and the existing Linux/macOS CI matrix. The manual
`Windows security regression` workflow tests Python 3.11 and 3.14 without expanding
every pull request into the previously slow Windows queue. Actions are pinned to
verified full revisions; review upstream changes before advancing pins.
Build from a clean, reviewed commit, run `twine check`, and use a new release version.
Never replace v0.7.5's existing tag/assets. Local builds from a dirty checkout may
include untracked docs/examples in the sdist; they are not release candidates.
