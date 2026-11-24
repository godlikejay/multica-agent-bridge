# Contributing

Start with the [architecture map](docs/architecture.md) to trace a Run from
Compose to the mailbox and find the relevant source and tests. For installed
Bridge operation, use the [operator guide](deploy/README.md), not a Compose
file at the repository root.

## Choose the smallest verification that proves the change

| Command | What it proves |
| --- | --- |
| `make check build` | Offline Go tests with race detection, vet, Python mailbox CLI tests, shell syntax checks and a Go build, all with synthetic fixtures. |
| `make container-smoke` | Connection control through the installed Compose entry point with synthetic connections, without a live Multica service. |
| `make e2e-auto` | The real pinned Multica server and daemon work with the Go worker. See the [disposable Docker smoke](e2e/README.md) for exactly what it checks. |

Run `make check build` before submitting code. Keep scheduling, delivery, and
isolation regressions offline, including independent Issues and endpoints,
stale requests, and recovery. For the Docker smoke, record the upstream
version, command and result separately. That smoke uses the file protocol
directly; it does not run the installed Python Skill, launch a coding agent,
drive a browser, or test Multica Cloud. Python subprocess tests exercise the
actual CLI against local fixtures, not a live Run. Avoid parallel instruction
sets or changing security boundaries to make a test pass.

Examples and tests must use invented data. Do not discover installed coding
agents, read real credentials, or connect to a team workspace. Keep test state,
authentication and local path mappings out of Git.

## CI and image publication

[Regression](.github/workflows/regression.yml) runs the three checks above
on feature branch pushes, pull requests and main pushes that match its paths.
Markdown-only pushes do not trigger it. Feature pushes and PR runs never
publish an image. On `main`, all three checks must pass for the exact commit
before publication; only changes to [image inputs](.github/scripts/image-inputs.sh)
automatically rebuild the image. Changes limited to `e2e/` or the Python
Skill scripts run checks without publishing. Go tests under `cmd/` or
`internal/` count as image inputs because those trees are included in the
build context. For an intentional
rebuild, run Regression manually on `main`; the same three checks still gate
publication. This is an image release gate, not branch protection.

When upgrading Multica, update the pinned upstream images in [Dockerfile](Dockerfile)
and [e2e/compose.yaml](e2e/compose.yaml) together. Check the task API and ACP
contract against the new source, adjust offline fixtures only for real contract
changes, then run the Docker smoke against the new pin before merging.
