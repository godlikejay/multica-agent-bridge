# Isolated Multica container trial

**Status:** Offline checks passed in development. This runbook does not
claim a Docker build, successful daemon registration, or a real Multica
Run. Record those outcomes separately when you run it. Here “real Multica”
means actual v0.11.1 Server/Web/Daemon containers with invented test data,
not a live team workspace or a mock API.

## Start from a clean clone

Use a regular host user on a machine with Git, Python 3, a working Docker
Engine or Docker Desktop with Compose v2, and a local browser. Install and
start Docker using the normal instructions for that machine first. Allow
network access for image pulls and building; the running stack is isolated.
The examples assume loopback ports 13000 and 18080 are free.

```sh
git clone https://github.com/godlikejay/multica-agent-bridge.git
cd multica-agent-bridge
git rev-parse HEAD
git status --short
docker info --format '{{.ServerVersion}}'
docker compose version
python3 --version
```

Do not copy an old `.local`, Docker volume, daemon profile or mailbox into
the new checkout. A fresh clone does not remove a Compose project of the
same name; use a fresh Docker context or review and stop any existing
`multica-bridge-e2e` project before starting this one. With Go 1.24+ and
Make installed locally, `make check`
runs offline checks and `make build` builds a host binary. Neither command
starts Docker or tests a real Multica Run. Go on the host is optional for
the steps below; Docker builds the Bridge from the checkout.

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
  at `/app/multica`; the shared root runtime Dockerfile copies it and builds this repository's
  `./cmd/multica-bridge`. Verify both versions below before running a task.
- Upstream `docker-compose.selfhost.yml`, `SELF_HOSTING.md`,
  `server/cmd/multica/{cmd_login,cmd_daemon,cmd_config}.go`, and
  `server/internal/handler/daemon.go` define this setup.
- If GHCR tag/image layout is unavailable, **stop**. Do not switch to latest or
  silently build main. A reviewed local build of that exact source using its
  Dockerfile/Dockerfile.web is a possible fallback, but is not automated here.

## Isolation

Dedicated Compose project, database, uploads and `.local/` directories. Only
localhost ports 13000/18080 are published via a minimal read-only loopback TCP
`proxy` container (`e2e/proxy`). `postgres`, `backend`, `frontend`, and `runtime`
remain attached solely to the `internal` Compose network, so runtime and backend
cannot call model providers or team servers. No Docker
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

From the repository root:

```sh
./e2e/run.sh init
./e2e/run.sh up
```

The second command validates Compose, pulls PostgreSQL and the pinned
Multica Server/Web images, builds the runtime with the pinned Multica CLI
and local Bridge, and starts the database, Server and Web. Wait for
`Backend /readyz ready` and the frontend loopback check. This does not
start the Bridge Daemon or test any assignments. If pull, build, or
readiness fails, stop and inspect the failing command's output and
`./e2e/run.sh logs`; do not switch to `latest` or a team deployment.

1. Open **http://localhost:13000**, register a synthetic account such as
   `bridge-e2e@example.invalid` with code `888888`; create a dedicated empty
   workspace named `BRIDGE_E2E_ONLY`. Do not import a real database.
2. In that account's settings create a temporary local personal access token.
   The daemon connects to `http://backend:8080` on the private Compose
   network. The commands below use Bash to pass the token without
   terminal echo; the upstream prompt in `./e2e/run.sh login` can echo.
   Never paste the token into source, issue text, terminal arguments,
   test output or a conversation. The synthetic profile is persisted in
   `e2e/.local/home` so later containers reuse it.

```bash
(
  IFS= read -rsp 'Synthetic Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" | docker compose --env-file e2e/.local/compose.env \
    -f e2e/compose.yaml --profile runtime run --rm -T runtime \
    --profile e2e --server-url http://backend:8080 login --token
)
```

3. Copy the workspace UUID from the local UI; set the CLI profile:
   `./e2e/run.sh workspace WORKSPACE_UUID`.
4. Verify the images and binaries before starting the daemon:

```sh
docker image inspect --format '{{json .RepoDigests}}' \
  ghcr.io/multica-ai/multica-backend:v0.11.1 ghcr.io/multica-ai/multica-web:v0.11.1
./e2e/run.sh versions
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
   For parallel acceptance, set that Agent's max concurrent tasks to at least
   2 and keep the Runtime cap unset or at least 2. The daemon in this Compose
   project defaults to 2 via `BRIDGE_MAX_ACTIVE_RUNS`.

## Single command after manual assignment

After the bootstrap **and** manually assigning the synthetic Issue below:

```sh
E2E_TASK=e2e/.local/bridge/outbox/TASK_EVENT_ID.json ./e2e/run.sh smoke
# Equivalent: E2E_TASK=e2e/.local/bridge/outbox/TASK_EVENT_ID.json make e2e
```

This runs real reply + completion receipts, then authenticated server status
verification. It does not create a workspace/agent/issue or exercise the
separate comment/restart cases. Use a **new** Issue/Run for the detailed
checks below if the smoke command already completed the first Run.
Missing E2E_TASK fails rather than printing
help and pretending to test. Automated Issue creation/assignment is pending
upstream API integration verification; no guessed endpoint is used here.

## End-to-end acceptance

1. Create an unassigned **Todo** issue in the synthetic workspace; do not
   use Backlog or Triage, which do not enqueue normal assignment Runs. Put
   `E2E_SYNTHETIC_ONLY: return a fixed acknowledgement; do not execute tools`
   in its title/body. Assign it to the synthetic Agent using the ordinary
   Issue assignee control, without any “don't start” option. This step tests
   the same assignment path as a teammate, not a direct Issue API lookup.
   Note exact Issue ID and **Run ID** from the UI. If repo-less execution is
   not offered, attach the synthetic Local Directory described below first.
2. Wait for an immutable `kind:task` JSON in `e2e/.local/bridge/outbox/`.
   Match its issue/run IDs to the UI; capture epoch. A stdout banner or
   container “running” alone does not prove ACP execution.

```sh
python3 -B skills/multica-bridge/scripts/mailbox.py \
  --root "$PWD/e2e/.local/bridge" list --limit 20
```

   The displayed task `id` identifies `outbox/<id>.json`.
3. Perform a deterministic reply (explicit operator action):

```sh
./e2e/run.sh fixture e2e/.local/bridge/outbox/TASK_EVENT_ID.json
```

   It copies scope IDs from the event, creates the host-side
   `inbox/<issue_id>/` directory, and never executes the event body. Require
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

### Two Issues in parallel (same synthetic Agent)

Use two distinct real directories. With Direct (`in_place`) mode, Multica
v0.11.1 holds a directory lock for the full Run, so two Issues directed to
the same directory can remain serialized even when all three concurrency
settings allow two. Do not map both Issues to `/workspaces` itself or use
symlinks/aliases for the same directory.

`./e2e/run.sh init` creates these separate fixtures. In the local UI,
create two synthetic projects. Attach one Local Directory
resource to each, with Direct execution, on the **same** test Daemon:
`/workspaces/a` for A and `/workspaces/b` for B. Enter paths as seen inside
the container, not paths on the host. If the UI cannot attach a container
path, use the pinned CLI with real project and Daemon IDs from the test
instance (the Daemon ID is not the display name):

```sh
# Set PROJECT_A, PROJECT_B and E2E_DAEMON_ID to the IDs from the local stack.
mc() {
  docker compose --env-file e2e/.local/compose.env -f e2e/compose.yaml \
    --profile runtime exec runtime multica --profile e2e "$@"
}
mc project resource add "$PROJECT_A" --type local_directory \
  --daemon-id "$E2E_DAEMON_ID" --execution-mode in_place --local-path /workspaces/a
mc project resource add "$PROJECT_B" --type local_directory \
  --daemon-id "$E2E_DAEMON_ID" --execution-mode in_place --local-path /workspaces/b
```

Create a new marked Todo Issue in each project and assign both to the
**same Agent** on that runtime. Approve any needed Local Directory prompt
only for those synthetic paths. Leave both active. Match the two initial
task event IDs to the correct Issues and exact Runs in the UI, then run:

```sh
./e2e/run.sh parallel \
  e2e/.local/bridge/outbox/TASK_A_EVENT_ID.json \
  e2e/.local/bridge/outbox/TASK_B_EVENT_ID.json
```

The runner requires both exact Runs to already be reported `running` by the
server before writing any inbox request. It replies to A, submits a separate
completion for B, verifies B reaches `completed`, then checks that A still
reports `running`. Review the UI to confirm each comment landed on only its
intended Issue. It leaves A running by default; pass `--finish-a` only if you
also want the runner to explicitly complete A **after** isolation is verified.
To verify picking A back up, leave it running, add a new marked comment on A,
then exercise the **actual operator CLI** rather than the fixture writer:

```sh
export BRIDGE_ROOT="$PWD/e2e/.local/bridge"
tool=skills/multica-bridge/scripts/mailbox.py
# Replace these values with Issue A's UUID and its new comment event ID.
python3 "$tool" status ISSUE_A_ID
python3 "$tool" list --issue-id ISSUE_A_ID --unread
python3 "$tool" read COMMENT_A_EVENT_ID
python3 "$tool" ack COMMENT_A_EVENT_ID
printf '%s\n' 'E2E_SYNTHETIC_ONLY: picked up A after B completed.' > e2e/.local/reply.txt
python3 "$tool" draft COMMENT_A_EVENT_ID --kind reply \
  --body-file e2e/.local/reply.txt > e2e/.local/reply.draft.json
python3 "$tool" submit e2e/.local/reply.draft.json
# Use the request_id printed by submit, after the runtime has polled.
python3 "$tool" receipt REQUEST_ID --issue-id ISSUE_A_ID
```

In a real terminal, verify the preview names A and the exact selected parent,
then enter the displayed request ID. Confirm `reply_posted` and inspect A's
comment thread in the UI. If the receipt hasn't appeared, poll `receipt`
again, not `submit`. Verify the reviewed event no longer appears in the
unread list; other unreviewed events can still be there. Complete A explicitly
when finished, or cancel it in the local UI if you do not want a final test reply.

No Run suggests a Backlog/Triage Issue, a suppressed assignment or a
blocked Agent. For a queued Run check all three concurrency caps (Agent,
Runtime and Daemon) and runtime online status. If it is
`waiting_local_directory`, check that the resources resolve to different
real directories and look for any pending approval before adjusting caps.
None of these states is a parallel pass.

### Restart / dedupe / failures (separate synthetic Runs)

- After a successful reply, restart it with `./e2e/run.sh restart`.
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
It never deletes volumes or `e2e/.local/`. If preserving the stack,
revoke the temporary PAT in the local UI after testing and before stopping
the server. After reviewing or saving sanitized evidence, you may remove
only this Compose project's disposable volumes and this checkout's
`e2e/.local/` manually. Do not run a system-wide Docker prune. There is no
automatic credential cleanup, publishing or deployment.

`python3 e2e/test_fixture.py` and `sh -n e2e/run.sh` are offline harness checks.
`docker compose --env-file e2e/.local/compose.env -f e2e/compose.yaml config --quiet`
is an additional Compose validation when Docker is available. Never label
static validation or mocked HTTP as a real Multica run.
