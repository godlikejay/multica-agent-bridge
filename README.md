# Multica Agent Bridge

A file mailbox between a [Multica](https://github.com/multica-ai/multica) issue run
and a local operator. The bridge uses Multica's Hermes ACP interface without
running a model. It does not launch or control an agent, open a checkout, or
execute instructions from issue text.

This repository is an early, single-writer prototype. Do not connect it to a
team or production workspace until the disposable integration suite passes and
the operating permissions have been reviewed.

## Quick start

- Go 1.24+ on Linux or macOS: `make check` runs synthetic tests, and
  `make build` builds `bin/multica-bridge`.
- Docker Compose v2: see [the integration runbook](e2e/README.md) for the
  pinned Multica stack, one-time synthetic workspace setup, and test command.
  No model account or Codex installation is needed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for where to make changes and what counts as
evidence, and [the protocol](docs/protocol.md) for file formats and receipts.

## Scope

The standard Multica daemon owns the remote Run. An issue and observed comment
versions appear in the outbox; only explicit local input can post a reply or
request normal completion. Finishing a Run does not change its Issue status.
One Run is processed at a time. A new Run for an issue increments a local epoch;
old requests are not carried over.

The mailbox and private Bridge state are JSON files. The test setup treats the
operator as the only Inbox writer. This is not an independent approval gate for
an agent sharing the same user account or filesystem privileges.

Licensed under [Apache-2.0](LICENSE).
