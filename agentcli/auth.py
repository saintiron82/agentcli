"""Explicit service credentials and private native-CLI directories.

This isolates configuration selection, not hostile code running as the same OS
user. Credentials are never written by this module or borrowed from global login.
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from .filesystem import safe_workspace_path

_BASE_ENV = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
                      "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
                      "TZ", "TERM", "COLORTERM", "NO_COLOR", "CI", "SSL_CERT_FILE",
                      "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY",
                      "ALL_PROXY", "NO_PROXY"})
_AUTH_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "AGENTCLI_CLAUDE_", "COPILOT_", "CODEX_")
_RESERVED_ENV = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
                          "LOCALAPPDATA", "XDG_CONFIG_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                          "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                          "GH_CONFIG_DIR", "GH_HOST"})


class AuthProfileError(ValueError):
    """Safe, payload-free error for incomplete or conflicting service auth."""


@dataclass(frozen=True)
class AuthProfile:
    profile_id: str
    root: str
    claude_oauth_token: str | None = field(default=None, repr=False)
    claude_api_key: str | None = field(default=None, repr=False)
    codex_api_key: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)
    disabled_providers: frozenset[str] = field(default_factory=frozenset)
    extra_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        additions = dict(self.extra_env)
        for name, value in additions.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    or name.upper() in _RESERVED_ENV or name.upper().startswith(_AUTH_PREFIXES)
                    or not isinstance(value, str) or "\0" in value):
                raise AuthProfileError("Invalid or reserved service environment override")
        object.__setattr__(self, "extra_env", MappingProxyType(additions))
        disabled = frozenset(self.disabled_providers)
        if not disabled.issubset({"claude", "codex", "copilot"}):
            raise AuthProfileError("Invalid disabled provider")
        object.__setattr__(self, "disabled_providers", disabled)
        if not isinstance(self.profile_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.profile_id):
            raise AuthProfileError("Invalid auth profile ID")
        path = Path(self.root).expanduser()
        if not path.is_absolute():
            raise AuthProfileError("Auth profile root must be absolute")
        if path == Path(path.anchor):
            raise AuthProfileError("Auth profile root must not be a filesystem root")
        safe_workspace_path(path.parent, path)
        object.__setattr__(self, "root", str(path.resolve()))
        for name in ("claude_oauth_token", "claude_api_key", "codex_api_key", "github_token"):
            token = getattr(self, name)
            if token is not None:
                if (not isinstance(token, str) or not token.strip()
                        or any(c in token for c in "\0\r\n")):
                    raise AuthProfileError("Credentials must be nonempty single-line strings")
                object.__setattr__(self, name, token.strip())
        if self.claude_oauth_token is not None and self.claude_api_key is not None:
            raise AuthProfileError("Choose Claude OAuth or API-key authentication, not both")

    @property
    def binding(self):
        return {"profile_id": self.profile_id, "root": os.path.normcase(self.root)}

    def directory(self, provider):
        if provider not in ("claude", "codex", "copilot", "gh", "home"):
            raise AuthProfileError("Provider does not support service authentication profiles")
        root = Path(self.root)
        return safe_workspace_path(root, root / provider)

    def _marker(self):
        root = Path(self.root)
        return safe_workspace_path(root.parent, root), safe_workspace_path(root, root / ".agentcli-auth-profile.json")

    def _check_private(self, path):
        if not path.is_dir():
            raise AuthProfileError("Service authentication directory is unavailable")
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise AuthProfileError("Auth profile directories must be private (mode 0700)")

    def prepare(self):
        """Create private directories and an ownership marker, never credentials.

        Windows ACLs must be established by the host. Existing roots belonging
        to another profile are rejected; no existing credential is overwritten.
        """
        root, marker = self._marker()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_private(root)
        fd, temporary = tempfile.mkstemp(prefix=".agentcli-auth-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self.binding, output)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, marker)  # Publish complete bytes without replacing a marker.
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._check_marker()
        for provider in ("claude", "codex", "copilot", "gh", "home"):
            path = self.directory(provider)
            path.mkdir(mode=0o700, exist_ok=True)
            self._check_private(path)
        return self

    def _check_marker(self):
        root, marker = self._marker()
        try:
            self._check_private(root)
            with marker.open("rb") as source:
                data = source.read(4097)
            if len(data) > 4096 or json.loads(data) != self.binding:
                raise AuthProfileError("Auth profile root belongs to another profile or is invalid")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raise AuthProfileError("Auth profile root is not prepared or is invalid") from None

    def _credential_file(self, provider, name):
        path = safe_workspace_path(Path(self.root), self.directory(provider) / name)
        try:
            with path.open("rb") as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise AuthProfileError("Service credential file exceeds the size limit")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise AuthProfileError("Service credential file is invalid")
            return result
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raise AuthProfileError("Service credentials are missing or invalid; global login is not used") from None

    def method(self, provider):
        if provider == "claude":
            return "api_key" if self.claude_api_key is not None else "oauth"
        if provider == "codex":
            return "api_key" if self.codex_api_key is not None else "file"
        if provider == "copilot":
            return "github_token"
        raise AuthProfileError("Provider does not support service authentication profiles")

    def claude_credentials(self):
        if self.claude_oauth_token is not None:
            return {"accessToken": self.claude_oauth_token}
        oauth = self._credential_file("claude", ".credentials.json").get("claudeAiOauth")
        value = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\0\r\n"):
            raise AuthProfileError("Service Claude OAuth credential is missing or invalid")
        return {**oauth, "accessToken": value.strip()}

    def claude_token(self):
        return self.claude_credentials()["accessToken"]

    def validate(self, provider):
        if provider in self.disabled_providers:
            raise AuthProfileError("Authentication for this service provider is disabled")
        self._check_marker()
        self.method(provider)
        self._check_private(self.directory(provider))
        if provider == "claude" and self.claude_api_key is None:
            return self.claude_credentials()
        elif provider == "codex" and self.codex_api_key is None:
            auth = self._credential_file("codex", "auth.json")
            token = (auth.get("tokens") or {}).get("access_token") if isinstance(auth.get("tokens"), dict) else None
            key = auth.get("OPENAI_API_KEY")
            if not ((isinstance(token, str) and token.strip()) or (isinstance(key, str) and key.strip())):
                raise AuthProfileError("Service Codex credential is missing or invalid")
            return auth
        elif provider == "copilot" and self.github_token is None:
            raise AuthProfileError("Service GitHub token is required; gh/global login is not used")
        return {}

    def environment(self, provider):
        """Private child environment. Never modify os.environ or run auth/login."""
        return self._environment(provider, self.validate(provider))

    def _environment(self, provider, credentials):
        # Inherit launch/transport settings only, never arbitrary host secrets.
        env = {k: v for k, v in os.environ.items() if k.upper() in _BASE_ENV}
        env.update(self.extra_env)
        home_path = self.directory("home")
        self._check_private(home_path)
        home = str(home_path)
        env.update(HOME=home, USERPROFILE=home)
        env.update(APPDATA=str(home_path / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home_path / "AppData" / "Local"),
                   XDG_CONFIG_HOME=str(home_path / ".config"))
        if provider == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(Path(self.root) / "claude")
            env["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] = "1"
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
            if self.claude_api_key is not None:
                env["ANTHROPIC_API_KEY"] = self.claude_api_key
            else:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = credentials["accessToken"]
        elif provider == "codex":
            env["CODEX_HOME"] = str(Path(self.root) / "codex")
            if self.codex_api_key is not None:
                env["CODEX_API_KEY"] = self.codex_api_key
        elif provider == "copilot":
            env["COPILOT_HOME"] = str(Path(self.root) / "copilot")
            gh_path = self.directory("gh")
            self._check_private(gh_path)
            env["GH_CONFIG_DIR"] = str(gh_path)
            env["GH_HOST"] = "github.com"
            env["COPILOT_GITHUB_TOKEN"] = self.github_token
            env["GH_TOKEN"] = self.github_token
            env["GITHUB_TOKEN"] = self.github_token
        return env

    def public_dict(self):
        """Non-secret profile identity only, not credentials or environment."""
        return dict(self.binding)
