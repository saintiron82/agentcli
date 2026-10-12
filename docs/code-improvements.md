# Code improvement record — 2026-10-12

[English](code-improvements.md) | [한국어](code-improvements.ko.md)

Status: local, unreleased `fix/security-hardening` branch. Review baseline:
`e8300c1`; implementation: `1a7d29f`. Execution cards and package results:
[GitHub issue 79](https://github.com/saintiron82/agentcli/issues/79).
No push, PR, merge, tag, PyPI upload, real account switch or paid model call.

## Completed corrections

- Built-in stores synchronize complete operations. MemoryStore pins active and
  queued handles against capacity/TTL eviction. Capacity is a soft bound while
  every candidate is pinned; host admission control still bounds concurrent work.
- Reset generations prevent old turns from restoring cleared session handles.
  SQLite persistence/clear use one atomic write transaction across local
  connections, preserving successful usage when an old generation finishes.
  A late creator failure cannot delete another turn's successful handle/usage.
- Async preparation, policy checks and cleanup execute in workers. Worker
  completion is protected during both ordinary cancellation and event-loop
  shutdown; thread-backed providers cannot release a lease while still running.
  Cancellation can wait for a synchronous operation to finish; it cannot kill a
  Python thread. Consecutive validation/planning work is batched into one worker.
- Auth marker publication is atomic and never replaces another profile's marker.
  Profile roots must support hard links. Validated environments/credentials are
  reused only within one request; the next request revalidates files and paths.
  Claude quota lookup preserves expiry/scopes. Profile auth errors retain local
  remediation; unavailable quota credentials return structured status.
- Empty instruction-file/system-prompt snapshots participate in explicit reset.
  File hashing uses 64 KiB blocks and a synchronized 1,024-entry LRU cache.
- Codex async/stream greeting recovery and payload accounting match the normal
  sync path. Native retries preserve observed usage/uncertainty and do not
  reissue a greeting after observed tool execution. Async/stream stale/greeting
  recovery has a shared two-attempt limit; no async recursive retry cycle remains.
- Schema/retry definitions fail before model execution. Enum comparison respects
  JSON boolean semantics, fallback receives correction feedback, and token
  accumulation preserves payload estimates, uncertainty and source metadata.
- SQLite statistics use SQL aggregation under one read snapshot rather than
  materializing all matching usage rows in Python. Existing filters/groups and
  unknown/empty-value semantics are covered by MemoryStore parity tests.
- Bulk clear no longer silently truncates at 1,000 handles. Additive
  `ConversationStore.list_all_by_owner()` preserves the old `list_by_owner`
  limit semantics. Custom stores with internal result caps should override it.

## Responsibility split and compatibility

`ContextSession` moved to `context_session.py`; previous package/client imports
remain valid. Omitted provider selection is resolved when the handle is created.
Async refinement offloads liveness checks; streamed refinement starts lazily
when consumed and closes its iterator on early exit.

`planning.py` defines frozen `CallPlan`: the six execution-path unpack sites no
longer depend on twelve positional return values. `async_utils.py` owns protected
worker completion; `store/locking.py` owns synchronization and turn reservations.
The unused session-refresh helper and Python SQLite grouping pipeline were removed.
`client.py` decreased from 1,889 to 1,767 lines, but remains a large coordinator.
`providers/base.py` and Claude's provider have not been comprehensively split.

## Offline performance evidence

Same Windows host, fake Claude output, temporary workspaces, no native process
or model execution. Medians use three batches of 40 calls after warmup. Baseline
source `5072c53` has the same runtime code as `e8300c1`; measured new source is
`d34f267` (the later listing compatibility change does not affect this workload).
Numbers are internal overhead, not production throughput or model latency.

| Store/auth | Sync before → after | Async before → after | Stream before → after |
| --- | --- | --- | --- |
| Memory/legacy | 0.590 → 0.540 ms | 0.774 → 1.174 ms | 0.920 → 1.449 ms |
| Memory/profile | 4.175 → 1.838 ms | 4.075 → 2.238 ms | 4.062 → 2.515 ms |
| SQLite/legacy | 2.235 → 1.804 ms | 2.406 → 3.462 ms | 2.630 → 2.862 ms |
| SQLite/profile | 6.960 → 3.242 ms | 8.279 → 4.003 ms | 10.150 → 4.730 ms |

Profile validation fell from three to one per request, safe-path checks from
14 to 4, and resolves from 30 to 10. Legacy async paths incur worker-hop and
lifetime-protection overhead. This is **not** proof of zero performance
regression; measure service concurrency/tail latency before rollout.

For the same 100,000-row, one-provider, ungrouped SQLite fixture with tracemalloc
enabled: 0.583 → 0.0565 seconds; Python peak traced allocation 29.8525 → 0.0047 MiB.
This excludes SQLite's internal allocations; returning many distinct groups
necessarily allocates a larger result. No audit data or runtime feature was
deleted to obtain these reductions.

## Validation and remaining work

Clean tracked-source Windows/Python 3.14 regression result: **1,188 passed,
39 skipped**. New boundary regressions cover active eviction, reset races,
cross-connection atomicity, loop shutdown, late creator failure, SQLite heartbeat,
auth initialization/snapshots, instruction removal, provider parity, schema and
statistics. The three final-audit defects were repaired and original reproductions
rerun by their reviewers. Build/twine and zero-runtime-dependency metadata checks
are part of the final gate; artifact version 0.7.5 is internal validation, not a
replacement of the published release.

Remaining: legacy async overhead; broader coordinator/provider decomposition;
configurable MemoryStore usage retention and subprocess total-output limits.
Retention defaults were not changed because silent truncation/deletion would
change audit and response behavior. Native schema correction feedback remains
the existing explicit exception, not a library transcript-replay feature.
Linux/macOS, actual provider accounts/models, production workload and distributed
server leases have not been verified. OS ACLs, trusted native configuration,
admission control and cross-server coordination remain host responsibilities.
