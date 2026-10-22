# CLI failure responses and Claude prompt boundaries

## Issue #81: structured failures

Claude and Codex synchronous and asynchronous invocations now parse stdout even
when the CLI exits with a nonzero code. Structured error messages and reported
token usage are retained. If stdout contains no structured error, the response
uses stderr or `exit=<code>` as before. Failed invocations return empty content
and retain the native exit code and raw stderr.

Claude's numeric `api_error_status: 429` is classified as `usage_limit`, including
when its message does not mention a limit. This makes the response eligible for
the existing opt-in fallback behavior; it does not add an automatic retry.

## Issue #82: option-like prompts

Claude argv prompts now follow all CLI options and a `--` separator. Text such as
`--version`, `--help`, or `- hello bullet` is passed as prompt text. New sessions,
resumed sessions, and both JSON output modes use this boundary. Large prompts
continue to use stdin with the existing EOF handling.

## Verification and scope

`tests/test_cli_failure_contract.py` covers both providers, sync/async calls,
public client error normalization, stderr fallback, reported usage, numeric 429,
and Claude prompt boundaries. Existing Claude command tests were updated.
Tests use simulated CLI responses and do not consume model quota.

Validation on Windows, 2026-10-22: the full suite passed with **1,307 passed,
39 skipped**. The JUnit report is saved at
`output/fixes-81-82-20261022/pytest.xml`.

This change does not add reset-coupon lookup or redemption, public cost/status
fields, or new streaming error behavior.
