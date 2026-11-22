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
Regression CI runs on branch pushes and pull requests that change code,
tests, or runtime/build inputs, including direct pushes to `main`. It skips
standalone documentation edits outside the test paths. It does not publish
images or block direct pushes; a failed check is reported on the commit.

- `make check build` runs offline Go tests (with race detection), vet,
  Python test discovery, shell syntax checks, and the Go build.
- `make container-smoke` checks connection control through the installed
  Compose entry point with synthetic connections. It does not use a live
  Multica service.
- `make e2e-auto` starts a disposable Multica to verify claim, reply and
  completion. Record upstream version, command and outcome. Fixture success
  alone is not proof of a real Run.

PRs and feature branch pushes validate only. On `main`, the same three
checks must pass for the exact commit before CI calls the image publisher.
Automatic publication is limited to image inputs; test-only changes run the
checks without rebuilding. To request a rebuild, run the Regression workflow
manually on `main`; it still requires all three checks. This is a release
gate, not a change to branch protection.

When upgrading Multica, update the pinned upstream images in `Dockerfile` and
`e2e/compose.yaml` together. Check the task API and ACP contract against the new
source, adjust offline fixtures only for real contract changes, then run the
same Docker smoke against the new pin before merging.

Examples and fixtures must use invented data. Keep test state, authentication,
and local path mappings out of Git. Avoid parallel instruction sets or changes
to the security model to make an integration test pass.
