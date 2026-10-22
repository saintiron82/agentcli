# Independent service authentication

[English](service-auth.md) | [한국어](service-auth.ko.md)

Status: included in v0.7.6. Runtime dependencies
remain zero. Profiles isolate credential selection within one Python process or
across services on one machine. They do not isolate hostile code under one OS user.

## Bind a client to a service

```python
import asyncio
from dataclasses import replace
from agentcli import AuthProfile, LLMClient, MemoryStore

# Read these variables from your service's secret manager; never hardcode tokens.
auth_a = AuthProfile(
    "service-a", "/srv/private/agentcli/service-a",
    claude_oauth_token=token_a,
).prepare()
auth_b = AuthProfile(
    "service-b", "/srv/private/agentcli/service-b",
    claude_oauth_token=token_b,
).prepare()
client_a = LLMClient(MemoryStore(), auth_profile=auth_a)
client_b = LLMClient(MemoryStore(), auth_profile=auth_b)

a, b = await asyncio.gather(
    client_a.chat_async("Service A's task", provider="claude",
                        owner="service-a", alias="worker", cwd=workspace_a,
                        provider_options={"permission_mode": "plan"}),
    client_b.chat_async("Service B's task", provider="claude",
                        owner="service-b", alias="worker", cwd=workspace_b,
                        provider_options={"permission_mode": "plan"}),
)

# Rotate A for the SAME account. B and already-started A requests keep their snapshot.
client_a.set_auth_profile(replace(auth_a, claude_oauth_token=new_token_a))
# Disable future A calls to Claude, without deleting credentials or affecting B.
client_a.set_auth_profile(replace(client_a.auth_profile, disabled_providers={"claude"}))
```

Use platform-appropriate absolute paths, outside repositories, on Windows too.
`prepare()` creates private directories and a non-secret ownership marker; it
does not write tokens, log in, copy global credentials, switch accounts or revoke
tokens at the provider. POSIX roots/directories must be private (0700). The host
must configure private Windows ACLs; the library does not provision ACLs.

Give services separate `owner` values when sharing a store: alias uniqueness is
still `(owner, alias)`. Reusing a bound conversation from a different profile,
or without its profile, is rejected. A legacy native session without an auth
binding needs a new conversation/alias; it is not silently adopted. Only profile
ID, root and authentication method are stored, never credentials. New handles
are bound before invocation, so cancellation cannot leave a borrowable handle.

Profile roots must support hard links for atomic marker publication. The
[code improvement record](code-improvements.md) supersedes the initial test and
performance record below and explains request-local credential/path snapshots.

## Provider support

| Provider | Profile input | Child state/auth directory |
| --- | --- | --- |
| Claude | `claude_oauth_token` OR `claude_api_key`; otherwise local `claude/.credentials.json` | `CLAUDE_CONFIG_DIR=<root>/claude` |
| Codex | `codex_api_key`; otherwise local `codex/auth.json` | `CODEX_HOME=<root>/codex`, file credential storage forced |
| Copilot | explicit `github_token` | `COPILOT_HOME=<root>/copilot`, `GH_CONFIG_DIR=<root>/gh` |
| Kiro/custom providers | unsupported in this version | rejected; no global-auth fallback |

Native CLI conventions are documented by
[Claude](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts),
[official OpenAI authentication docs](https://learn.chatgpt.com/docs/auth#credential-storage),
[Codex automation docs](https://learn.chatgpt.com/docs/non-interactive-mode#authenticate-in-automation),
and [Copilot](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli).
Use supported, entitled tokens for each provider. Provision profile-local Codex
file credentials securely before use; no default auth file/keychain is borrowed.
File-based Codex token refresh, if performed by the CLI, stays in its service root.
Claude file credentials are read as an explicit access token; AgentCli does not
refresh them. Copilot requires an explicit token rather than OS keychain/gh discovery.
The standalone Copilot CLI is preferred; a gh wrapper may need its extension
installed in the profile-specific gh directory. Never provision it by switching
the shared machine login.

## Environment and lifecycle rules

- Requests snapshot an immutable profile. No `os.environ` mutation, registry-wide
  token mutation or profile-global GitHub-token cache is used. A shared provider
  object can serve clients with different profiles.
- Only launch/locale/CA/proxy variables are inherited. Auth/routing variables and
  arbitrary parent secrets are not inherited. HOME, USERPROFILE, AppData and XDG
  config paths point inside the service root. `extra_env={...}` supplies explicit
  service tool settings; credential/routing/home overrides are reserved. Treat
  `extra_env` and proxy configuration as private, trusted host inputs.
- Native project/managed configuration, plugins, hooks and tools remain trusted
  execution inputs. Validate them and actual CLI behavior; this is not an OS or
  network sandbox. Use separate OS users/containers for adversarial tenants.
- Missing, disabled or invalid credentials fail closed. Per-call `oauth_token`,
  constructor OAuth overrides and usage credential-path overrides conflict with
  a bound profile and are rejected. Fallback may use only credentials available
  in the same profile, and still must preserve execution policy.
- Rotation preserves profile ID/root and auth method. The host must ensure new
  credentials belong to the same account. Use a new profile/root/conversation for
  a different account or auth method. No online account-identity proof is performed.
- Disabling/replacing a profile affects future calls; it does not terminate active
  requests or revoke provider-issued tokens. Cancel/close those requests yourself
  when immediate shutdown is required.
- Use `open_warm(auth_profile=...)` for a dedicated warm process. Its environment
  is fixed at creation: close and reopen it to rotate credentials. Do not combine
  an auth profile with a custom warm environment. Resume history must exist in
  that profile's workspace directory.
- Pass the service's client explicitly to `AgentProfile.chat/chat_async/chat_stream`; do not
  repeatedly replace the process-wide `set_default_client` for separate services.
- Authentication does not restrict tools or filesystem permissions. Configure
  least-privilege execution policy separately, as in the plan-mode example above.

## Status, quota and migration

`health_check(probe=False)` checks local readiness and returns `configured` with
`auth_ok=None` for a ready profile. It does not claim the remote token is accepted.
`probe=True` makes a model call and may consume quota/execute tools. Failed CLI
version checks do not count as configured readiness.

Account usage and session liveness use the same profile's token/directories.
Subscription quota lookup is unsupported in API-key mode, rather than querying
another account. Returned error bodies/content remain private; do not log them.
`AuthProfile.public_dict()` and repr omit tokens, but dumping credential fields or
`environment()` does not. Protect profile roots and diagnostic files.

Clients without `auth_profile` retain legacy shared-environment/login behavior;
installing the feature does not automatically isolate existing services. Migrate
each service explicitly, test its configured identity, and keep accounts pinned.
No remote authentication, real credential rotation or paid generation is part of
the offline tests described here.

## Validation record — 2026-10-12

Code commit: `c6f1f79`; clean tracked-source snapshot: `5072c53`.

- Service authentication tests: **46 passed**, including real fake-CLI child
  processes for all three supported providers, sync/async/stream concurrency,
  rotation during a request, disabled/missing credentials, session ownership,
  quota routing and Windows junction rejection. These are not remote-account tests.
- Full clean-source Windows suite: **1,066 passed, 39 skipped**. Platform-specific
  POSIX checks, unavailable optional integrations and symlink privilege checks
  remain unverified on this host. Linux/macOS CI was not run in this validation.
- Isolated sdist and wheel builds, `twine check`, zero runtime dependencies,
  packaged auth modules/tests/docs, paired UTF-8 documentation, local links and
  Python example syntax checks passed.
- Installed Claude **2.1.296** and Codex **0.161.0** passed offline `--version`
  under a private profile with fake tokens. Codex also accepted the file-store
  config override with `exec --help`. Copilot is not installed on this host.
- A no-model/no-subprocess fake-provider benchmark of the legacy path measured
  sync 186–201 µs / async 343–370 µs before this feature and sync 188–192 µs /
  async 382–393 µs afterward. This small sample includes scheduling noise and is
  not a model-throughput benchmark or proof of zero performance regression.

During the original branch validation, no global login, actual account credentials
or deployment were changed, and no push, tag, PyPI upload or paid generation was
performed. Those internal artifacts used 0.7.5 metadata; the published 0.7.5 does
not contain this feature. The feature is included in v0.7.6.
Before rollout, test real account identity and failure/rotation behavior with
each service's securely provisioned credentials and its installed CLI version.
