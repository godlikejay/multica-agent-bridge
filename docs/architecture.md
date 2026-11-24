# Architecture and maintenance map

English | [简体中文](zh-CN/architecture.md)

Multica owns Issues, assignment and Run scheduling. Bridge converts an
assigned Run into local mailbox files; the operator decides which local
coding agent, project and permissions to use. Bridge does not launch that
agent, and the agent does not need a Multica token.

1. The [installed Compose service](../deploy/compose.yaml) starts
   `multica-bridge serve`. The [supervisor](../internal/supervisor) manages one
   daemon per enabled connection. This is the production entry point;
   repository-root Compose files are contributor tooling, and
   [e2e/compose.yaml](../e2e/compose.yaml) is the disposable upstream test stack.
2. The Multica daemon starts `multica-bridge acp` for an assigned Run, not a
   general-purpose coding runtime. The
   [entry point](../cmd/multica-bridge/main.go) and
   [ACP adapter](../internal/acp) handle handshake and cancellation.
   [Multica transport](../internal/multica) verifies the task identity and
   handles comments and attachments.
3. The [Run worker](../internal/bridge) binds that task to its connection,
   workspace, Issue, Run and epoch. [Mailbox I/O](../internal/mailbox) owns
   scoped paths and local persistence. Different Issues can progress
   independently; tasks on the same Issue are serialized. If a reply may
   have landed but cannot be confirmed, the worker does not post it again.
4. The operator uses the [Skill and Python mailbox tool](../skills/multica-bridge/SKILL.md)
   against those files. Taking a Run and acknowledging read context are local
   operations. An authorized reply keeps the Run open; an authorized
   completion requests normal termination of that exact Run, not an Issue
   status change. Confirm remote completion in Multica. A selected local
   Run is not evidence it is still live.

The default installation mounts configuration, credentials, runtime files and
the mailbox, not a host checkout, home directory or Docker socket. Content
from an Issue or attachment is untrusted data, never permission to execute
locally or to reply or complete.

## Where to start a change

| Change | Read first |
| --- | --- |
| Connection lifecycle or routing | [Supervisor](../internal/supervisor) and [connection settings](../deploy/README.md#connection-settings) |
| ACP launch, handshake, or cancellation | [Entry point](../cmd/multica-bridge/main.go) and [ACP adapter](../internal/acp) |
| Run/Issue identity, claiming, comment visibility, or upstream transport | [Pinned upstream baseline](decisions.md) and [Multica transport](../internal/multica) |
| Mailbox schema, attachments, reply reconciliation, or Run termination | [File protocol](protocol.md), [Run worker](../internal/bridge) and [mailbox](../internal/mailbox); change the Go producer and Python consumer together if the wire contract changes |
| Operator commands or pickup/acknowledgment behavior | [Agent integration](agent-integration.md), [Skill](../skills/multica-bridge/SKILL.md), and the [CLI and fixture tests](../skills/multica-bridge/scripts) |
| Image, Compose, or release CI | [Local build](deployment.md) and [contributor verification](../CONTRIBUTING.md) |

The [file protocol](protocol.md) is the reference for field semantics,
scope and retries. [Contributor verification](../CONTRIBUTING.md) distinguishes
the offline CLI tests, the installed-container smoke, the disposable real
Multica smoke, and the image publication gate.
