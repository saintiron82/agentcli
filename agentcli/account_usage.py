"""Subscription quota lookups using only stdlib; never refresh/write credentials.

Endpoints and header formats are based on the local ClaudeAuthSwitch checkout
inspected on 2026-10-08. These CLI-internal endpoints can change independently
of the library. API-key billing usage is deliberately outside this contract.
"""

import base64
import http.client
import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .types import (AccountUsage, UsageWindow, ERROR_AUTH, ERROR_NETWORK,
                    ERROR_TIMEOUT, ERROR_UNKNOWN, ERROR_USAGE_LIMIT)

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROBE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_PROBE_MODEL = "claude-haiku-4-5-20251001"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_MAX_BYTES = 1024 * 1024


def _finite(value) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def validate_timeout(timeout: float) -> None:
    if not _finite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _epoch(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            number = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number / 1000 if number > 100_000_000_000 else number)


def _window(used, reset=None, seconds=None) -> UsageWindow | None:
    if not _finite(used):
        return None
    duration = int(seconds) if _finite(seconds) and seconds > 0 else None
    return UsageWindow(round(min(100.0, max(0.0, float(used))), 2),
                       _epoch(reset), duration)


def _read_json(path: Path) -> dict:
    with path.open("rb") as file:
        raw = file.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("credentials file too large")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("credentials must be an object")
    return data


def _failure(provider: str, status: str, message: str, *, source: str = "",
             code: int | None = None, probe: bool = False,
             error_type: str = "", action: str = "") -> AccountUsage:
    return AccountUsage(provider=provider, status=status, message=message,
                        source=source, http_status=code, probe_performed=probe,
                        error_type=error_type, suggested_action=action)


def _auth_failure(provider: str, *, expired: bool = False) -> AccountUsage:
    return _failure(provider, "token_expired" if expired else "auth_required",
                    "Login token expired." if expired else "Readable OAuth credentials are required.",
                    error_type=ERROR_AUTH,
                    action="Run the provider CLI to authenticate/refresh, then retry; or supply credentials_path.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward a subscription bearer token to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(provider: str, url: str, headers: dict, *, timeout: float,
             source: str, body: bytes | None = None):
    probe = body is not None
    request = urllib.request.Request(url, headers=headers, data=body,
                                     method="POST" if probe else "GET")
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Error bodies can echo credentials. Only retain status/headers.
            with exc:
                return exc.code, exc.headers, b"", None
        with response:
            raw = b"" if probe else response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return None, {}, b"", _failure(
                    provider, "invalid_response", "Usage response exceeded size limit.",
                    source=source, probe=probe, error_type=ERROR_UNKNOWN)
            return response.status, response.headers, raw, None
    except (TimeoutError, socket.timeout):
        status, kind = "timeout", ERROR_TIMEOUT
    except urllib.error.URLError as exc:
        status, kind = (("timeout", ERROR_TIMEOUT) if isinstance(exc.reason, TimeoutError)
                        else ("network_error", ERROR_NETWORK))
    except (OSError, http.client.HTTPException, ValueError):
        status, kind = "network_error", ERROR_NETWORK
    return None, {}, b"", _failure(provider, status, "Usage request failed.",
                                   source=source, probe=probe, error_type=kind)


def _http_failure(provider: str, code: int, source: str, *, probe: bool = False):
    if code in (401, 403):
        status, kind = "auth_required", ERROR_AUTH
        message = "Usage endpoint rejected the login token or its scopes."
        action = "Authenticate with the provider CLI and retry."
    elif code == 429:
        status, kind = "rate_limited", ERROR_USAGE_LIMIT
        message = "Usage lookup was rate-limited; this does not establish account utilization."
        action = "Wait before requesting usage again."
    else:
        status, kind = "http_error", ERROR_UNKNOWN
        message, action = "Usage endpoint returned an unexpected HTTP status.", ""
    return _failure(provider, status, message, source=source, code=code,
                    probe=probe, error_type=kind, action=action)


def _parse_body(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot(provider, windows, source, code, *, probe=False, plan=""):
    if not windows:
        return _failure(provider, "invalid_response", "Response contains no usable limit windows.",
                        source=source, code=code, probe=probe, error_type=ERROR_UNKNOWN)
    return AccountUsage(provider=provider, ok=True, status="ok", windows=windows,
                        source=source, plan=plan, observed_at=time.time(),
                        probe_performed=probe, http_status=code)


def _probe_required():
    return _failure("claude", "probe_required",
                    "This token requires an inference probe to read limit headers.",
                    action="Set allow_probe=True only if a model request consuming usage is acceptable.")


def _claude_probe(token: str, timeout: float) -> AccountUsage:
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
               "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json",
               "User-Agent": "claude-code/2.1.0"}
    body = json.dumps({"model": CLAUDE_PROBE_MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode()
    code, headers, _, error = _request("claude", CLAUDE_PROBE_URL, headers,
                                     timeout=timeout, source="probe", body=body)
    if error:
        return error
    if code not in (200, 429):
        return _http_failure("claude", code, "probe", probe=True)
    windows = {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        prefix = f"anthropic-ratelimit-unified-{label}-"
        try:
            used = float(headers.get(prefix + "utilization", ""))
        except (TypeError, ValueError):
            continue
        if 0 <= used <= 1:
            used *= 100
        window = _window(used, headers.get(prefix + "reset"))
        if window:
            windows[key] = window
    return _snapshot("claude", windows, "probe", code, probe=True)


def claude_account_usage(*, token: str | None = None,
                         credentials_path: str | None = None,
                         timeout: float = 15, allow_probe: bool = False) -> AccountUsage:
    validate_timeout(timeout)
    if credentials_path is None:
        token = token or _text(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        if not token and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return _failure("claude", "unsupported_auth", "API-key/gateway billing usage is not supported.")
    oauth = {}
    if not token:
        try:
            path = (Path(credentials_path).expanduser() if credentials_path is not None
                    else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json")
            data = _read_json(path)
            oauth = data.get("claudeAiOauth")
            oauth = oauth if isinstance(oauth, dict) else {}
            token = _text(oauth.get("accessToken"))
        except (OSError, ValueError, RuntimeError):
            return _auth_failure("claude")
    if not token:
        return _auth_failure("claude")
    expires = oauth.get("expiresAt")
    if _finite(expires) and expires <= time.time() * 1000:
        return _auth_failure("claude", expired=True)
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and "user:profile" not in scopes:
        return _claude_probe(token, timeout) if allow_probe else _probe_required()
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
               "User-Agent": "claude-code/2.1.0"}
    started = time.monotonic()
    code, _, raw, error = _request("claude", CLAUDE_USAGE_URL, headers,
                                  timeout=timeout, source="oauth")
    if error:
        return error
    # Unknown-scope tokens (e.g. env setup-token) may require a probe. Never
    # switch credentials/account or silently consume inference quota on failure.
    if code == 403 and not (isinstance(scopes, list) and "user:profile" in scopes):
        if not allow_probe:
            result = _probe_required()
            result.http_status, result.source = code, "oauth"
            return result
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return _failure("claude", "timeout", "Usage lookup timeout exhausted.",
                            source="oauth", error_type=ERROR_TIMEOUT)
        return _claude_probe(token, remaining)
    if code != 200:
        return _http_failure("claude", code, "oauth")
    data = _parse_body(raw)
    windows = {}
    for key in ("five_hour", "seven_day"):
        item = data.get(key)
        if isinstance(item, dict):
            window = _window(item.get("utilization"), item.get("resets_at"))
            if window:
                windows[key] = window
    return _snapshot("claude", windows, "oauth", code)


def _jwt_exp(token: str):
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data.get("exp") if isinstance(data, dict) else None
    except (ValueError, IndexError, UnicodeError):
        return None


def codex_account_usage(*, credentials_path: str | None = None,
                        timeout: float = 15) -> AccountUsage:
    validate_timeout(timeout)
    try:
        path = (Path(credentials_path).expanduser() if credentials_path is not None
                else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json")
        auth = _read_json(path)
    except (OSError, ValueError, RuntimeError):
        return _auth_failure("codex")
    if auth.get("auth_mode") == "apikey":
        return _failure("codex", "unsupported_auth", "API-key billing usage is not supported.")
    tokens = auth.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    token = _text(tokens.get("access_token"))
    if not token:
        if auth.get("OPENAI_API_KEY"):
            return _failure("codex", "unsupported_auth", "API-key billing usage is not supported.")
        return _auth_failure("codex")
    expires = _jwt_exp(token)
    if _finite(expires) and expires <= time.time():
        return _auth_failure("codex", expired=True)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli",
               "OpenAI-Beta": "codex-1", "originator": "Codex Desktop",
               "Accept": "application/json"}
    account = _text(tokens.get("account_id"))
    if account:
        headers["ChatGPT-Account-Id"] = account
    code, _, raw, error = _request("codex", CODEX_USAGE_URL, headers,
                                  timeout=timeout, source="oauth")
    if error:
        return error
    if code != 200:
        return _http_failure("codex", code, "oauth")
    data = _parse_body(raw)
    limits = data.get("rate_limit")
    limits = limits if isinstance(limits, dict) else {}
    found = []
    for index, key in enumerate(("primary_window", "secondary_window")):
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        reset = item.get("reset_at")
        after = item.get("reset_after_seconds")
        if _epoch(reset) is None and _finite(after) and after >= 0:
            reset = time.time() + after
        window = _window(item.get("used_percent"), reset, item.get("limit_window_seconds"))
        if window:
            seconds = window.limit_window_seconds
            label = ("five_hour" if seconds <= 21600 else "seven_day") if seconds else (
                "five_hour" if index == 0 else "seven_day")
            found.append((label, window))
    if len(found) == 2 and found[0][0] == found[1][0]:
        ordered = sorted((w for _, w in found), key=lambda w: w.limit_window_seconds or math.inf)
        windows = dict(zip(("five_hour", "seven_day"), ordered))
    else:
        windows = dict(found)
    plan = _text(data.get("plan_type"))
    # No raw server strings/credential echoes in snapshots or repr().
    if len(plan) > 40 or any(_text(v) and _text(v) in plan for v in tokens.values()):
        plan = ""
    return _snapshot("codex", windows, "oauth", code, plan=plan)
