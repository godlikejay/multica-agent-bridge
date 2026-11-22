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
| Real Multica acceptance | [One Docker smoke test](e2e/README.md) |
| Daemon supervision and connection settings | `internal/supervisor`, [connection configuration](deploy/README.md#connection-settings) |
| Public mailbox tool and user Skill | [skills/multica-bridge](skills/multica-bridge/SKILL.md) |

Keep scheduling, delivery, and isolation regressions offline: the bridge tests
cover independent Issues and endpoints, follow-up delivery and stale requests;
the client and supervisor tests cover the API contract, routing and recovery.
Run `make e2e-auto` on a Docker host to prove real Multica claim, reply and
completion. CI runs it separately from offline checks and image publication. Record upstream version, command, and outcome;
fixture success is not proof of a real Run.

When upgrading Multica, update the pinned upstream images in `Dockerfile` and
`e2e/compose.yaml` together. Check the task API and ACP contract against the new
source, adjust offline fixtures only for real contract changes, then run the
same Docker smoke against the new pin before merging.

Examples and fixtures must use invented data. Keep test state, authentication,
and local path mappings out of Git. Avoid parallel instruction sets or changes
to the security model to make an integration test pass.
