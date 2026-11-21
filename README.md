# Multica Agent Bridge

A file mailbox between a [Multica](https://github.com/multica-ai/multica) issue run
and a local operator. The bridge uses Multica's Hermes ACP interface without
running a model. It does not launch or control an agent, open a checkout, or
execute instructions from issue text.

This repository is an early prototype with one writer per Issue. Do not connect it to a
team or production workspace until the disposable integration suite passes and
the operating permissions have been reviewed.

## Quick start

- Go 1.24+ on Linux or macOS: `make check` runs synthetic tests, and
  `make build` builds `bin/multica-bridge`.
- Docker Compose v2: see [the integration runbook](e2e/README.md) for the
  fresh-machine setup, local image build, and one- and two-Issue acceptance
  tests against pinned Multica. No model account or coding agent is needed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for where to make changes and what counts as
evidence, and [the protocol](docs/protocol.md) for file formats and receipts.

## Use with an existing Multica

Build one runtime image containing Multica Daemon and Bridge, then start it
with Compose. See [Docker setup](docs/deployment.md) for configuration and
one-time login, and the [agent installation and mailbox tool](docs/agent-integration.md)
for Codex, Antigravity CLI and Oh My Pi.

To connect to multiple Multica instances from **one runtime container**, see
[Multi-connection Docker setup](docs/multi-connection.md) and the
[two-service acceptance runbook](e2e/multi/README.md).

## Scope

The standard Multica daemon owns the remote Run. An issue and observed comment
versions appear in the outbox; only explicit local input can post a reply or
request normal completion. Finishing a Run does not change its Issue status.
Multiple Issues can be active. Runs for one Issue are serialized. A new Run
for that Issue increments its epoch; old requests are not carried over.

The mailbox and private Bridge state are JSON files. The operator writes the Issue-specific Inbox; the runtime reads it. This is not an independent approval gate for
an agent sharing the same user account or filesystem privileges.

Keep each Issue's local pickup state (reviewed messages, checkout and unsent
draft reference) separate from its live Run and from other Issues. The pickup
tool does not start agents, change Multica, or turn a draft into an approved
reply. See [the operator workflow](docs/agent-integration.md).

Licensed under [Apache-2.0](LICENSE).
