"""Request-local authentication snapshots for shared clients/providers."""
import contextvars
import functools
import inspect
import threading
from contextlib import contextmanager
from .auth import AuthProfileError
from .types import ProviderHealth
from .async_utils import complete_in_thread

_active = contextvars.ContextVar("agentcli_auth_profile", default=None)
_client = contextvars.ContextVar("agentcli_auth_client", default=None)
_snapshot = contextvars.ContextVar("agentcli_auth_snapshot", default=None)
AUTH_SCOPE_KEY = "service_auth_profile"
SERVICE_AUTH_ACTION = "Update this service's auth profile and retry; do not switch shared login"


def current_auth_profile():
    return _active.get()


@contextmanager
def auth_scope(profile, client=None, snapshot=None):
    if profile is None and _active.get() is None:
        yield
        return
    token = _active.set(profile)
    client_token = _client.set(client)
    snapshot_token = _snapshot.set(snapshot if snapshot is not None else (threading.RLock(), {}))
    try:
        yield
    finally:
        _active.reset(token)
        _client.reset(client_token)
        _snapshot.reset(snapshot_token)


def auth_scoped(function):
    """Snapshot the client's profile once; never hold a ContextVar over yield."""
    if inspect.isasyncgenfunction(function):
        @functools.wraps(function)
        async def stream(self, *args, **kwargs):
            profile = current_auth_profile() if _client.get() is self else self._auth_profile
            snapshot = _snapshot.get() if _client.get() is self else (threading.RLock(), {})
            iterator = function(self, *args, **kwargs)
            try:
                while True:
                    try:
                        with auth_scope(profile, self, snapshot):
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    yield chunk
            finally:
                with auth_scope(profile, self, snapshot):
                    await iterator.aclose()
        return stream
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(self, *args, **kwargs):
            profile = current_auth_profile() if _client.get() is self else self._auth_profile
            if profile is None and current_auth_profile() is None:
                return await function(self, *args, **kwargs)
            snapshot = _snapshot.get() if _client.get() is self else (threading.RLock(), {})
            with auth_scope(profile, self, snapshot):
                return await function(self, *args, **kwargs)
        return asynchronous
    @functools.wraps(function)
    def synchronous(self, *args, **kwargs):
        profile = current_auth_profile() if _client.get() is self else self._auth_profile
        if profile is None and current_auth_profile() is None:
            return function(self, *args, **kwargs)
        snapshot = _snapshot.get() if _client.get() is self else (threading.RLock(), {})
        with auth_scope(profile, self, snapshot):
            return function(self, *args, **kwargs)
    return synchronous


def provider_environment(provider, legacy_builder):
    profile = current_auth_profile()
    return dict(_environment_snapshot(provider)[0]) if profile else legacy_builder()


def _environment_snapshot(provider):
    profile = current_auth_profile()
    snapshot = _snapshot.get()
    if snapshot is None:
        snapshot = (threading.RLock(), {})
    lock, cache = snapshot
    with lock:
        if provider not in cache:
            credentials = profile.validate(provider)
            cache[provider] = (profile._environment(provider, credentials), credentials)
        return cache[provider]


def service_credentials(provider):
    """Bounded credential metadata from this request, never global files."""
    return dict(_environment_snapshot(provider)[1])


def require_auth_scope(conv):
    profile = current_auth_profile()
    bound = conv.metadata.get(AUTH_SCOPE_KEY)
    expected = profile.binding if profile else None
    if bound is not None and bound != expected:
        raise PermissionError("Conversation authentication profile mismatch")
    if bound is None and profile and any(k.startswith("session_id:") and v
                                        for k, v in conv.metadata.items()):
        raise PermissionError("Legacy native session has no auth binding; use a fresh conversation")


def validate_auth_options(provider, options=None):
    profile = current_auth_profile()
    if profile is None:
        return
    if options and options.get("oauth_token") is not None:
        raise AuthProfileError("An auth profile cannot be overridden by per-call credentials")
    _environment_snapshot(provider)


async def validate_auth_options_async(provider, options=None):
    snapshot = _snapshot.get()
    if current_auth_profile() is None or (snapshot is not None and provider in snapshot[1]):
        validate_auth_options(provider, options)
    else:
        await complete_in_thread(validate_auth_options, provider, options)


def require_auth_method(conv, provider):
    profile = current_auth_profile()
    if profile is None:
        return
    previous = conv.metadata.get("service_auth_method:" + provider)
    if previous is not None and previous != profile.method(provider):
        raise PermissionError("Conversation authentication method mismatch; use a fresh conversation")


def service_auth_response(response):
    if current_auth_profile() is not None and response.error_type == "auth":
        response.suggested_action = SERVICE_AUTH_ACTION
    return response


def configured_health(provider, binary, version, returncode=0):
    if returncode:
        return ProviderHealth(provider=provider, ok=False, status="cli_error",
                              available=True, binary=binary, auth_ok=None,
                              exit_code=returncode, message="CLI version check failed")
    return ProviderHealth(provider=provider, ok=True, status="configured",
                          available=True, binary=binary, version=version,
                          auth_ok=None, message="Service credentials configured; probe to verify provider acceptance")
