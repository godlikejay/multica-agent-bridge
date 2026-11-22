# Multica Agent Bridge

English | [简体中文](README.zh-CN.md)

A file mailbox between a [Multica](https://github.com/multica-ai/multica) issue run
and a local operator. The bridge uses Multica's Hermes ACP interface without
running a model. It does not launch or control an agent, open a checkout, or
execute instructions from issue text.

This repository is an early prototype with one writer per Issue. Do not connect it to a
team or production workspace until the disposable integration suite passes and
the operating permissions have been reviewed.

## Install with Docker Compose

You need Docker Compose v2 and access to an existing Multica service. The
runtime image includes Multica Daemon and Bridge. You do not need Go, a model
API key, or a separate Daemon installation. The host mailbox tool uses
Python 3 (standard library only).

Start with the [Docker install guide](docs/deployment.md). It takes you from
creating your Multica token to receiving a test Issue and sending a reply.
Start with one connection; use the [multi-connection guide](docs/multi-connection.md)
only if you need one container to connect to several servers or accounts.

After the mailbox works, install the [coding agent skill](docs/agent-integration.md)
for Codex, Antigravity CLI, or Oh My Pi. The skill drafts replies; you decide
what to submit. See the [English documentation index](docs/README.md) for more.

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

## Licenses

Bridge source is licensed under [Apache-2.0](LICENSE). The runtime image is
built on [Multica](https://github.com/multica-ai/multica) and bundles its
v0.11.1 CLI/Daemon under the [Multica License](https://github.com/multica-ai/multica/blob/v0.11.1/LICENSE).
The image includes both components' license files and the upstream
[Multica NOTICE](https://github.com/multica-ai/multica/blob/v0.11.1/NOTICE)
under `/usr/share/licenses/`.
