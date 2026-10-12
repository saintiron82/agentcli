# Security hardening status

[English](security-hardening.md) | [한국어](security-hardening.ko.md)

Branch: `fix/security-hardening`. Baseline: published v0.7.5.
These changes are not yet released. The existing v0.7.5 tag/wheel is unchanged.

## Step 1 — SEC-005: automatic diagnostic payload exposure

The patch omits complete MCP/config payloads from diagnostic argv rather than
trying to recognize every possible credential field. Raw stderr and subprocess
exception bodies are omitted from automatic logs. Trace schema 2 stores stderr
length markers and keeps exit codes, elapsed time, call/session IDs and chunk
timelines. The trace writer sanitizes argv/stderr even for direct helper callers.

Actual execution argv, native session handling, tool permissions, CLI errors,
response content, token accounting and fallback behavior are unchanged. No
runtime dependency or model request is added. Raw errors remain available to the
host application; the host must control their display and logging. This patch
is not a general-purpose secret scrubber for arbitrary metadata or application
logs. Traces still require restricted access. Existing logs are not rewritten;
review their access and retention separately.

The regression tests use fake credentials and subprocesses only. They cover
configuration redaction, trace-boundary sanitization, sync/async failures for
Claude/Codex/Copilot, client failure logging, streaming stderr and exception
messages. The new tests fail against the baseline and pass with the patch.

An offline Windows microbenchmark compared argv redaction plus JSON encoding:
short debug records took 2.05 µs at baseline versus 2.82 µs patched; a 32,000-char
MCP payload took 36.93 µs versus 2.55 µs. These are local diagnostic-path
measurements, not model latency or production performance guarantees. Successful
calls with debug disabled do not run this redaction path.

## Step 2 — SEC-003: profile permission propagation

Non-empty profile tool restrictions and MCP configuration now merge into
`provider_options`; explicit per-call options take precedence without mutating
the profile. Copilot accepts per-call tool lists and omits `--allow-all-tools`
when an allowlist is supplied. Unsupported security options and routing overrides
in `provider_options` fail before creating a conversation or launching a CLI.
Ordinary unsupported diagnostic options remain ignorable. Relevant tests: 126
passed; all three profile call paths are covered.

## Step 3 — SEC-002 / SEC-008: conversation scope

Explicit conversation lookup and liveness checks require matching `owner`.
Cross-owner context injection requires a host `context_authorizer(owner, source)`
returning exactly `True`; unavailable context is rejected before target creation.
Successful conversations bind to the canonical working directory. A different
directory requires another alias, not a silent session reset. Legacy handles
without a binding acquire it on first successful use; verify legacy history or
start a new session before adopting an unverified handle. Copilot native named
sessions are derived from the app conversation ID, preventing identical aliases
of different owners from resuming an account-global session. Relevant tests:
121 passed, 6 platform skips. Supply the original owner when resuming by ID.

## Step 4 — SEC-004: policy-preserving fallback

Sync, async and stream fallback skip providers whose native execution policy
cannot be verified equivalent. Explicit security/auth options or restrictive
constructor settings require the same implementation and matching policy
settings. Unrestricted fallback remains available. Blocked fallback is reported
without launching the candidate; unsupported options cannot disappear silently.
Relevant tests: 106 passed, including all three call paths.

Codex also records the effective sandbox/approval policy on success. Resume
cannot silently change it; use `new_session=True` to change policy. Restricted
legacy sessions without a recorded policy must also start fresh. Resume under
the same recorded policy preserves the native session. An additional regression
test covers this boundary without model calls.

## Step 5 — SEC-006: safe materialization

Materialization accepts root filenames only and rejects symlinks/junctions below
the workspace or in skill bundles. Unmanaged destination skills are skipped,
not deleted. Managed skills use staging plus rename; failed copies leave the
old skill intact, and failed rollback preserves the backup for manual recovery.
Instruction ownership requires an exact first-line marker. Source/destination
checks do not replace OS isolation against concurrent hostile filesystem actors.
Relevant tests: 62 passed, including a real Windows junction and rollback failure.

## Step 6 — SEC-001: Windows launcher arguments

Prefer native Claude executables. Standard npm shims launch their Node entrypoint
directly; supported interpreter/script wrappers launch the interpreter directly.
No user argument passes through cmd.exe. Unknown batch wrappers are rejected and
must be replaced by a native executable or supported shim. Resolution is cached
against launcher mtime/size. Invocation, streaming, warm, Kiro and health paths
share this boundary. Argument tests include shell metacharacters, quotes, percent
expansion and newlines. Relevant tests: 187 passed. Installed Claude 2.1.296 and
Codex 0.161.0 version checks succeeded without model calls.

## Step 7 — SEC-007: Kiro filesystem policy

Filesystem capabilities are explicit: `allow_file_reads=True` and
`allow_file_writes=None` (derive from `trust_all`). With `trust_all=False`, writes
are denied unless explicitly enabled, independently of agent-supplied tool
titles. Denial becomes a safe JSON-RPC error instead of crashing the transport.
Reads/writes default to a 1 MiB cap (`max_file_bytes`), honor read line ranges,
and reject workspace links. This is not an OS sandbox for native agent tools.
Relevant tests: 35 passed; the existing privileged symlink test is excluded on
this Windows host and real junction coverage is tested separately.

## Step 8 — OPS-002: process and stream cleanup

All streaming paths drain stderr, retaining at most 64 KiB; debug timelines keep
at most 10,000 entries. A nonzero exit after partial text ends with `error`, not
`done`. Process-exit and post-kill waits are bounded. Kiro fails pending ACP
requests on EOF; warm sessions retain and collect their drain task.

Codex/Copilot synchronous calls now use the shared owned-process runner. Windows
uses a best-effort kill-on-close Job Object attached immediately after spawn;
POSIX uses a dedicated process group. Assignment is not atomic with spawn and
can be rejected by host policy, with a warning. This is cleanup, not a sandbox.
Strict containment requires a host-owned job/container. See the
[Windows job limit reference](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information).

## Step 9 — OPS-001: session coordination

Clients sharing a store coordinate sync, async and streaming calls across loops.
File-backed SQLite stores also acquire a sidecar OS file lock per conversation,
across local processes. Different conversations remain parallel. Preparation is
refreshed after acquisition so history, instructions and session IDs are current.
Async waiters do not monopolize executor workers. Cancellation collects a late
acquisition instead of leaking it. Related regression gate: 96 passed, 5 skipped,
including 64 concurrent callers and a separate Windows process.

`LLMClient(..., session_lock_factory=factory)` accepts a host-managed distributed
lease. `factory(store, conversation_id)` returns an object with `acquire()` and
`release()`; these must be thread-independent, bounded, and safe to release after
a failed acquisition. Hosts own lease expiry, renewal and fencing of stale
writers. Sidecar locks do not certify NFS/SMB or cross-server coordination.
Use a single session writer or a verified distributed lease plus shared native
session availability. Separate custom-store objects referring to one backend
also require a host lease. Lock files are not unlinked while callers may use them.

## Step 10 — OPS-003: bounded retry budgets

Explicit `wall_timeout` now shares one monotonic execution budget across fallback,
schema correction and native subprocess retries. Zero means no provider call;
omitting it retains per-attempt `timeout`. Async calls and stream reads enforce
the remaining budget even for custom providers. Synchronous custom providers
must honor the supplied timeout; Python cannot safely terminate arbitrary code.
The execution budget starts at request entry, but waiting for a conversation
lease and bounded cleanup can make return time exceed it. It is not a hard
end-to-end latency guarantee or a transactional rollback.

Tool actions may already have happened before timeout/fallback/schema retry.
Use `schema_retries=0`, `fallback=False`, appropriate tool restrictions and host
idempotency keys for mutating tasks. Token metrics are observations, not a billing
ledger, especially after interrupted attempts. No automatic native history
deletion or external side-effect rollback is introduced.

## Finding status

| ID | Scope | Status |
| --- | --- | --- |
| SEC-001 | Windows batch argument interpretation | Fixed, real Windows argument tests |
| SEC-002 | Conversation and injected-context ownership | Fixed, regression tested |
| SEC-003 | Profile permission/config propagation | Fixed, regression tested |
| SEC-004 | Permission preservation during fallback | Fixed, regression tested |
| SEC-005 | Automatic diagnostic payload exposure | Fixed; raw returned errors remain private |
| SEC-006 | Materialization boundaries and user skill protection | Fixed, regression tested |
| SEC-007 | Explicit Kiro filesystem access policy | Fixed, regression tested |
| SEC-008 | Workspace/session binding | Fixed; legacy migration caveat above |
| OPS-001 | Coordination of concurrent session callers | Fixed locally; distributed lease is host configuration |
| OPS-002 | Streaming pipe drainage and bounded teardown | Fixed, regression tested |
| OPS-003 | Retry time budgets and side effects | Shared budget added; host idempotency required |
| OPS-004 | Cross-server native history/account availability | Host configuration; runbook required |
| OPS-005 | Native history retention and restore | Host policy; no destructive automation |

Review and test each change independently. Do not combine security fixes with
a broad refactor, change existing session identifiers silently, or serialize
unrelated sessions globally. Closing a code finding does not certify an actual
server deployment or all versions of the external CLIs.
