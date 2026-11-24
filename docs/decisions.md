# Design decisions

- **Official Daemon plus minimal Hermes ACP adapter.** Reuse the provider
  lifecycle rather than implementing server registration or running a model.
  Validate against matching Server and Daemon versions. The current
  [runtime image](../Dockerfile) and [Docker smoke](../e2e/compose.yaml) use
  upstream `v0.11.1` ([exact source commit](https://github.com/multica-ai/multica/tree/e1fe10d6694848e35dd1aa00e08cd11d93e545ac)).
  Use that pin for current contracts. The earlier `220791d` source reference
  in the client records the original adapter review, not the runtime pin.
- **Files before a database.** Private JSON snapshots and one writer per Issue
  suffice for concurrent Runs across distinct Issues. Keep the wire protocol independent of storage;
  revisit SQLite if multi-record transactions, state size or recovery become
  unwieldy. Do not add multi-machine writing to this design by accident.
- **Separate admission, routing, and work.** The Daemon admits Runs and owns
  the slot limit. Bridge workers use independent Issue shelves and locks;
  the operator can switch local tasks without running multiple coding agents.
  An idle bridge still occupies its daemon slot until the Run terminates.
- **Normal completion is explicit.** A separate, local complete request
  authorizes the final comment. A remote cancel, idle timeout, or replacement
  Run is not permission to reply, restart local work, or update Issue state.
- **Conservative ambiguous delivery.** The server has no general comment
  idempotency key. Fail closed on unknown results instead of re-posting. Keep
  task-scoped credentials in the Daemon environment, never the mailbox.
- **No coding-agent runtime dependency.** The operator and coding agent use
  the same Python tool and file protocol. The [shared Skill](../skills/multica-bridge/SKILL.md)
  is optional client guidance, not a runtime dependency of the Go bridge.
