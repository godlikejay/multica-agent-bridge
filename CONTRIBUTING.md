# Contributing

Use a clean checkout. Run `make check` and `make build` before submitting a
change. The first command uses fixtures only. It must not discover an installed
coding agent, read real credentials, or connect to an outside workspace.

| Change | Start here |
| --- | --- |
| ACP handshake and wait/cancel | `internal/acp` |
| Task identity, comments, API transport | `internal/multica` |
| JSON files, replay and run epochs | `internal/mailbox`, `internal/bridge` and [protocol](docs/protocol.md) |
| Runtime image and day-to-day deployment | [Dockerfile](Dockerfile), [deploy/compose.yaml](deploy/compose.yaml), [local build](docs/deployment.md) and [operator install](deploy/README.md) |
| Real Multica acceptance | [Single service](e2e/README.md), [two services](e2e/multi/README.md) |
| Daemon supervision and connection settings | `internal/supervisor`, [connection configuration](deploy/README.md#connection-settings) |
| Public mailbox tool and user Skill | [skills/multica-bridge](skills/multica-bridge/SKILL.md) |

Add a deterministic regression to the relevant package. Run real integration
checks separately on a disposable Docker stack. Record upstream version, test
command, outcome, and which layer was actually exercised. Fixture success is not
proof that a real Daemon registered or the server completed a Run.

Examples and fixtures must use invented data. Keep test state, authentication,
and local path mappings out of Git. Avoid parallel instruction sets or changes
to the security model to make an integration test pass.
