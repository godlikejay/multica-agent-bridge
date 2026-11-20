# Isolated Multica container trial

**Status:** shell/Python/YAML checked; Docker is absent in the development
container. Image pulls, startup, human login, runtime registration and real
Multica delivery have **not** been executed. This is a runnable checklist,
not evidence of an end-to-end pass. Go fixture tests are a separate layer.

## Pinned contract

- Upstream source reviewed: `multica-ai/multica` at
  `220791df038b0a507556d91d7be899c4ba8a463d`.
- Release tag `v0.11.1` resolves to `e1fe10d6694848e35dd1aa00e08cd11d93e545ac`,
  not the review commit above. Its Dockerfile CLI copy path, daemon flags,
  `/api/daemon/tasks/{taskId}/status` route and Hermes ACP methods were also
  checked. Full adapter compatibility still requires the container trial.
- Backend/web: official `ghcr.io/multica-ai/multica-{backend,web}:v0.11.1`.
  No `latest` fallback. Record image RepoDigests after pull; release tags alone
  are not immutable digests. The pinned upstream Dockerfile installs the CLI
  at `/app/multica`; our runtime image copies it and builds this repository's
  `./cmd/multica-bridge`. Verify both versions below before running a task.
- Upstream `docker-compose.selfhost.yml`, `SELF_HOSTING.md`,
  `server/cmd/multica/{cmd_login,cmd_daemon,cmd_config}.go`, and
  `server/internal/handler/daemon.go` define this setup.
- If GHCR tag/image layout is unavailable, **stop**. Do not switch to latest or
  silently build main. A reviewed local build of that exact source using its
  Dockerfile/Dockerfile.web is a possible fallback, but is not automated here.

## Isolation

Dedicated Compose project, database, uploads and `.local/` directories. Only
localhost ports 13000/18080 are published. The Compose network is `internal`,
so runtime/backend cannot call model providers or team servers. No Docker
socket, host home, Codex, SSH keys, real repository or team credentials are
mounted. The runtime's only agent executable is the bridge's Hermes adapter.
Inbox is host-written, container-read-only; outbox and state are writable.
Runtime home contains **only synthetic test** authentication. Do not attach
this profile to a real workspace. Do not expose these ports through a tunnel.

`APP_ENV=development` + fixed code `888888` is intentionally local-test-only.
Telemetry is disabled; no email/LLM/OAuth/integration keys are supplied.
The internal hostname `backend` needs the explicit `BRIDGE_ALLOW_TEST_HTTP=1`
test exception; normal production HTTPS validation must stay strict.

## Human bootstrap (not automated)

Prerequisites: Docker Engine/Desktop with Compose v2, Python 3, network access
for **image pulls/build only**. From repository root:

```sh
./e2e/run.sh init
./e2e/run.sh up
```

1. Open **http://localhost:13000**, register a synthetic account such as
   `bridge-e2e@example.invalid` with code `888888`; create a dedicated empty
   workspace named `BRIDGE_E2E_ONLY`. Do not import a real database.
2. In that account's settings create a temporary local personal access token.
   Run `./e2e/run.sh login` and enter it at the prompt. It is stored solely in
   `.local/home`; never paste it into source, issue text, terminal arguments,
   test output or a conversation. The container talks to `http://backend:8080`.
3. Copy the workspace UUID from the local UI; set the CLI profile:
   `./e2e/run.sh workspace WORKSPACE_UUID`.
4. Verify exact binaries before starting the daemon:

```sh
cd e2e
# Each line uses only the isolated local test profile.
docker compose --env-file .local/compose.env --profile runtime run --rm runtime version
docker compose --env-file .local/compose.env --profile runtime run --rm --entrypoint multica-bridge runtime version
docker compose --env-file .local/compose.env --profile runtime run --rm --entrypoint multica-bridge runtime acp --version
cd ..
./e2e/run.sh runtime
./e2e/run.sh status
```

5. Confirm **in the local Multica UI** that `synthetic-bridge-e2e` is online and
   exposes Hermes (our bridge), not Codex/another CLI. `daemon status` is daemon
   readiness, not proof of a registered usable agent. If detection fails,
   inspect local logs and stop. Create a synthetic agent bound to that runtime
   with Hermes selected; no model key, real repo or external MCP servers.
   Use `/workspaces` only. A repo-less/local-directory task may require a
   human confirmation in the runtime UI; do not claim bootstrap bypasses it.

## Single command after manual assignment

After the bootstrap **and** manually assigning the synthetic Issue below:

```sh
E2E_TASK=e2e/.local/bridge/outbox/TASK_EVENT_ID.json ./e2e/run.sh smoke
# Root Makefile may expose the identical invocation as `make e2e`.
```

This runs real reply + completion receipts, then authenticated server status
verification. It does not create a workspace/agent/issue or exercise the
separate comment/restart cases. Missing E2E_TASK fails rather than printing
help and pretending to test. Automated Issue creation/assignment is pending
upstream API integration verification; no guessed endpoint is used here.

## End-to-end acceptance

1. Create/assign a synthetic issue to this agent. Put
   `E2E_SYNTHETIC_ONLY: return a fixed acknowledgement; do not execute tools`
   in its title/body. Note exact Issue ID and **Run ID** from the UI.
2. Wait for an immutable `kind:task` JSON in `e2e/.local/bridge/outbox/`.
   Match its issue/run IDs to the UI; capture epoch. A stdout banner or
   container “running” alone does not prove ACP execution.
3. Perform a deterministic reply (explicit operator action):

```sh
./e2e/run.sh fixture e2e/.local/bridge/outbox/TASK_EVENT_ID.json
```

   It only copies scope IDs from the event, never executes its body. Require
   `reply_posted`, then inspect the reply on that **same issue/thread** in UI.
4. While that Run is active, add a UI comment containing `E2E_SYNTHETIC_ONLY`.
   Wait for `kind:comment` with its `source_id`. Use that event with `fixture`
   to reply. Verify the reply's thread parent is that comment, not another run.
5. Complete explicitly, preferably using the latest received comment event:

```sh
./e2e/run.sh fixture e2e/.local/bridge/outbox/COMMENT_EVENT_ID.json --complete
./e2e/run.sh verify RUN_UUID
```

   The first command waits for `completion_requested`: only a durable local
   decision. The second prompts the **same synthetic runtime owner's** PAT,
   then reads `/api/daemon/tasks/{runId}/status` until the server says
   `completed`. A receipt alone is NOT an E2E pass. A 401/403/404 is a failed
   verification, not completion. Check final response is visible in UI too.
6. Record image digests, Multica/bridge versions, Issue/Run IDs, receipt statuses
   and server completion. Exclude tokens/cookies/config files from evidence.

### Restart / dedupe / failures (separate synthetic Runs)

- After a successful reply, restart runtime (`docker compose ... restart runtime`).
  Leave the same immutable request file in inbox. Check it is not posted twice.
  Do not change an existing request's bytes or generate a new ID to “retry”.
- Cancel in UI mid-run. Expect adapter termination and no later issue writes.
- Start a new Run of the same issue. Check epoch/Run identity change; submit a
  **new request ID with old scope** only in this synthetic fixture. Require
  `stale`, not a post to the new run. This case is manually constructed; the
  ordinary runner intentionally doesn't mutate scope.
- Test HTTP loss separately: a `delivery_uncertain` receipt stops the runner.
  Inspect the remote issue before any further action; never auto-repeat POST.
- Check container inbox read-only by attempting a harmless temporary test file
  from runtime; it must fail, while bridge state/outbox still update normally.

## Stop and evidence

`./e2e/run.sh down` stops only this Compose project and preserves test data.
It never deletes volumes or `.local/`. If desired, remove these synthetic
resources manually after reviewing them. There is no automatic cleanup of
credentials/data and no automated publish/deploy step.

`python3 e2e/test_fixture.py` and `sh -n e2e/run.sh` are offline harness checks.
`docker compose --env-file e2e/.local/compose.env -f e2e/compose.yaml config --quiet`
is an additional Compose validation when Docker is available. Never label
static validation or mocked HTTP as a real Multica run.
