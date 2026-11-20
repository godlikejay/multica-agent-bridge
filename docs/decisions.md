# Design decisions

- **Official Daemon plus minimal Hermes ACP adapter.** Reuse the provider
  lifecycle rather than implementing server registration or running a model.
  Validate against matching Server and Daemon versions. Version baseline:
  upstream `v0.11.1`; contract reviewed at
  [220791d](https://github.com/multica-ai/multica/tree/220791df038b0a507556d91d7be899c4ba8a463d).
- **Files before a database.** A single writer and a private JSON snapshot
  suffice for the first loop. Keep the wire protocol independent of storage;
  revisit SQLite if multi-record transactions, state size or recovery become
  unwieldy. Do not add multi-machine writing to this design by accident.
- **Normal completion is explicit.** A separate, local complete request
  authorizes the final comment. A remote cancel, idle timeout, or replacement
  Run is not permission to reply, restart local work, or update Issue state.
- **Conservative ambiguous delivery.** The server has no general comment
  idempotency key. Fail closed on unknown results instead of re-posting. Keep
  task-scoped credentials in the Daemon environment, never the mailbox.
- **No persistent agent dependencies.** Human and agent use the same
  documented interface. No repository-specific Skill is needed until an
  optional stable client integration justifies one.
