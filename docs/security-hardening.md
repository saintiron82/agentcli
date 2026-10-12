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

## Remaining work

| ID | Scope | Status |
| --- | --- | --- |
| SEC-001 | Windows batch argument interpretation | Pending |
| SEC-002 | Conversation and injected-context ownership | Fixed, regression tested |
| SEC-003 | Profile permission/config propagation | Fixed, regression tested |
| SEC-004 | Permission preservation during fallback | Pending |
| SEC-006 | Materialization boundaries and user skill protection | Pending |
| SEC-007 | Explicit Kiro filesystem access policy | Pending |
| SEC-008 | Workspace/session binding | Fixed; legacy migration caveat above |
| OPS-001 | Coordination of concurrent session callers | Pending |
| OPS-002 | Streaming pipe drainage and bounded teardown | Pending |

Review and test each change independently. Do not combine security fixes with
a broad refactor, change existing session identifiers silently, or serialize
unrelated sessions globally. Closing a code finding does not certify an actual
server deployment or all versions of the external CLIs.
