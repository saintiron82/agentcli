"""Request-local authentication snapshots for shared clients/providers."""
import contextvars
import functools
import inspect
from contextlib import contextmanager
from .auth import AuthProfileError
from .types import ProviderHealth

_active = contextvars.ContextVar("agentcli_auth_profile", default=None)
_client = contextvars.ContextVar("agentcli_auth_client", default=None)
AUTH_SCOPE_KEY = "service_auth_profile"
SERVICE_AUTH_ACTION = "Update this service's auth profile and retry; do not switch shared login"


def current_auth_profile():
    return _active.get()


@contextmanager
def auth_scope(profile, client=None):
    if profile is None and _active.get() is None:
        yield
        return
    token = _active.set(profile)
    client_token = _client.set(client)
    try:
        yield
    finally:
        _active.reset(token)
        _client.reset(client_token)


def auth_scoped(function):
    """Snapshot the client's profile once; never hold a ContextVar over yield."""
    if inspect.isasyncgenfunction(function):
        @functools.wraps(function)
        async def stream(self, *args, **kwargs):
            profile = current_auth_profile() if _client.get() is self else self._auth_profile
            iterator = function(self, *args, **kwargs)
            try:
                while True:
                    try:
                        with auth_scope(profile, self):
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    yield chunk
            finally:
                with auth_scope(profile, self):
                    await iterator.aclose()
        return stream
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(self, *args, **kwargs):
            profile = current_auth_profile() if _client.get() is self else self._auth_profile
            with auth_scope(profile, self):
                return await function(self, *args, **kwargs)
        return asynchronous
    @functools.wraps(function)
    def synchronous(self, *args, **kwargs):
        profile = current_auth_profile() if _client.get() is self else self._auth_profile
        with auth_scope(profile, self):
            return function(self, *args, **kwargs)
    return synchronous


def provider_environment(provider, legacy_builder):
    profile = current_auth_profile()
    return profile.environment(provider) if profile else legacy_builder()


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
    profile.validate(provider)


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
