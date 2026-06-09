# Project Positioning

[English](positioning.md) | [한국어](positioning.ko.md)

## One-sentence definition

`agentcli` is a session-aware Python client layer for embedding the user's
installed Claude Code, Codex, and GitHub Copilot CLI as application backends.

## Why this exists

Agentic coding CLIs have become useful runtime tools, but they were designed
first for humans in terminals. They differ in session flags, output events,
resume behavior, permission controls, model selectors, and auth failure shapes.

Most apps start by calling one CLI with `subprocess.run(...)`. That works until
the app needs more than one provider, resumable agents, streaming, health
checks, usage accounting, fallback rules, and instruction freshness.

`agentcli` packages that application-level client layer without taking ownership
of the provider's native session history.

Stated plainly, the role is narrow. When an application needs an agent
backend, the usual route is to build an agent loop on a hosted API.
`agentcli` takes the other route: reuse the agentic CLI the user already
has installed and authenticated — typically on a flat-rate subscription —
as that backend. The CLI keeps its own harness (tool loop, skills, project
instructions, session history); this library is only the embedding client
around it.

Two limits follow from that choice. It fits personal and internal
automation, not serving third parties on someone else's subscription. And
CLI interfaces change faster than versioned APIs — absorbing that churn
behind a stable Python contract is the maintenance this library exists to
do.

## Core boundary

The provider CLI owns:

- login and credentials
- native tool loop
- native session history
- provider-specific config and policy

`agentcli` owns:

- a stable Python client API
- `owner + alias + cwd` app-level identity
- provider session handles
- usage and latency rows
- instruction hash freshness
- health-check normalization
- streaming chunk and error normalization
- explicit provider/model/fallback policy

## Design invariants

These four constraints are non-negotiable. They are what the project pursues at
the code level, not just stated preferences — every change is expected to hold
them.

- **Zero runtime dependencies.** `[project.dependencies]` stays empty; dev tools
  live in `[project.optional-dependencies].dev` only. This is what keeps the
  library safe to embed in any host app.
- **The CLI session is the single source of truth for history.** The library
  stores only `session_id` per provider and never re-injects prior turns into
  prompts. `system_prompt` / `AgentProfile.instructions` are sent only when the
  instruction hash changes. This keeps the layer lightweight and token usage
  predictable.
- **The three providers stay normalized.** `ClaudeProvider`, `CodexProvider`,
  and `CopilotProvider` expose one unified contract for session flags, streaming
  chunk types, and permission flags. A provider change that breaks parity is a
  regression.
- **Korean and English docs are paired.** Every `.md` has a matching `.ko.md`;
  changes to one are mirrored in the other.

## Who should use it

Use `agentcli` if you are building:

- a desktop or web app that needs an AI coding/review/research backend
- a multi-agent workflow where each role should keep its own native CLI session
- a project automation tool that wants provider switching without rewriting CLI glue
- a product that needs usage logs and session routing, not raw transcript storage

## Who should not use it

Do not use `agentcli` if you need:

- a hosted API client for OpenAI/Anthropic/GitHub
- a complete agent framework with its own planner and tool loop
- a session capture/sync product that copies histories between tools
- a CLI end-user app for voice, local models, or terminal UX

## Difference from adjacent projects

Some public packages focus on lower-level CLI execution contracts: command
construction, subprocess lifecycle, provider facts, and transcript discovery.
`agentcli` sits one level higher. It is designed for a host application that
wants to keep named agents alive through `owner + alias + cwd` and persist only
session handles plus operational metadata.

Other packages focus on session capture, context transfer, or end-user CLI
tools. `agentcli` is not in those categories. It is an embedding SDK for
applications that already want to use the user's existing provider CLIs.

## Release posture

The project is public-beta quality. The code has package metadata, tests,
typing marker, examples, changelog, and release notes. It is suitable for
developer integration and feedback. It is not a 1.0 stability promise because
provider CLIs change quickly.
