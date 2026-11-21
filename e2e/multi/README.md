# One container, two synthetic Multica services

This is a separate, operator-driven end-to-end test. It uses two actual,
independently initialized Multica v0.11.1 installations and **one Bridge runtime
container**. Each installation has its own database, uploads and private
network. Both Daemons run inside that one container; Codex, real repositories,
SSH keys and company credentials are never mounted. Web/API ports are published
only on host loopback by the same fixed-destination proxy used by the
[single-service fixture](../README.md).

## Clean checkout and local prerequisites

Use a regular host user with Python 3 and a working Docker Engine with Compose
v2. Run on the Docker host (or Docker Desktop), from a fresh Git checkout in a
directory writable by the host user. A remote Docker context or a read-only
VM mount is not sufficient for the bind mounts. Host ports 13100, 13200, 18180
and 18280 must be free. Image pulls/builds require network access; the running
Multica backends and runtime are only on internal networks.

```sh
git clone https://github.com/godlikejay/multica-agent-bridge.git
cd multica-agent-bridge
./e2e/multi/run.sh init
./e2e/multi/run.sh up
./e2e/multi/run.sh versions
```

Record the tested Git revision, Docker/Compose versions, and image digests:

```sh
git rev-parse HEAD
docker version
docker compose version
docker image inspect --format '{{json .RepoDigests}}' \
  ghcr.io/multica-ai/multica-backend:v0.11.1 ghcr.io/multica-ai/multica-web:v0.11.1
```

`up` brings up two databases, servers, web frontends and loopback proxies,
then runs readiness checks. It does not yet start the shared runtime or
prove registration. If readiness fails, run `./e2e/multi/run.sh logs`.

## One-time synthetic bootstrap (human)

1. In separate browser profiles (or one normal and one private window), open **http://localhost:13100**
   (A) and **http://localhost:13200** (B). Cookies can be shared between
   localhost ports within one profile. Create one invented account per
   server, using different reserved test emails such as
   `bridge-a@example.invalid` / `bridge-b@example.invalid` and test code
   `888888`. Create one empty Workspace on each using the same name, e.g.
   `BRIDGE_E2E_ONLY`. Never use an existing team workspace.
2. Generate a temporary PAT in each synthetic account's settings. Enter it
   only into the matching hidden terminal prompt:

   ```sh
   ./e2e/multi/run.sh token a
   ./e2e/multi/run.sh token b
   ./e2e/multi/run.sh runtime
   ./e2e/multi/run.sh status a
   ./e2e/multi/run.sh status b
   ```

   The files are local, owner-only and read-only inside the runtime. The
   launcher uses the appropriate one to sign in; the verifier reads the same
   temporary credentials to verify each server independently without re-entry.
   Never paste either token in commands, source, chat, or evidence. Each
   `status` should show `running` and its own PID.
3. Confirm in **both** local UIs that the intended runtime is online. Bind
   one synthetic Agent on each site to its own runtime with Hermes selected
   (the Bridge implementation), without model keys, external MCP servers or
   real repositories. If you use the CLI to set up a project, select the
   actual synthetic workspace UUID first:

   ```sh
   ./e2e/multi/run.sh workspace a WORKSPACE_A_UUID
   ./e2e/multi/run.sh workspace b WORKSPACE_B_UUID
   ```

   The CLI default workspace does not restrict Daemon registration. If a
   repo-less assignment needs a local project resource, the `workspace`
   commands above also create `/bridge-runtime/a/workspaces/demo` and
   `/bridge-runtime/b/workspaces/demo` after the launcher binds both profiles. Create a synthetic project and use the actual local
   Daemon ID from the matching site's runtime record, not the display label:

   ```sh
   ./e2e/multi/run.sh cli a project resource add PROJECT_A_UUID \
     --type local_directory --daemon-id DAEMON_A_UUID \
     --execution-mode in_place --local-path /bridge-runtime/a/workspaces/demo
   ./e2e/multi/run.sh cli b project resource add PROJECT_B_UUID \
     --type local_directory --daemon-id DAEMON_B_UUID \
     --execution-mode in_place --local-path /bridge-runtime/b/workspaces/demo
   ```

   Approve a directory prompt only for these synthetic paths. No browser
   sign-up, token creation, permission prompt or real assignment is automated.

## Run and verify

1. In each workspace create one new unassigned **Todo** Issue. Put
   `E2E_SYNTHETIC_ONLY: return a fixed acknowledgement; do not execute tools`
   in both title and description. The two displayed keys must match
   (e.g. the first issue with the same prefix and number in each clean
   workspace), but server Issue and Run UUIDs must differ. Assign each issue
   to its corresponding synthetic Agent through the normal UI. Leave both
   Runs active.
2. Identify each initial `kind: task` event by scope, comparing its Issue
   UUID and exact Run UUID with the right site's UI. The following operator
   CLI commands show emitted events without changing state:

   ```sh
   tool=skills/multica-bridge/scripts/mailbox.py
   root="$PWD/e2e/multi/.local/bridge"
   python3 "$tool" --root "$root" --connection a --workspace WORKSPACE_A_UUID list
   python3 "$tool" --root "$root" --connection b --workspace WORKSPACE_B_UUID list
   ```

3. Invoke the synthetic acceptance runner in an interactive terminal. It writes deterministic replies and
   completion requests to the disposable inbox only:

   ```sh
   ./e2e/multi/run.sh accept \
     e2e/multi/.local/bridge/outbox/a/WORKSPACE_A_UUID/TASK_A_EVENT_ID.json \
     e2e/multi/.local/bridge/outbox/b/WORKSPACE_B_UUID/TASK_B_EVENT_ID.json
   ```

   The runner asserts exactly one runtime container and two different Daemon
   PIDs, checks both Runs are `running` on their own servers, and verifies
   both replies against exact server Issue UUID, source Run UUID and comment
   identity. It completes A without ending B, stops A's synthetic backend
   and A's Daemon, then posts and verifies a reply on B **while A's backend
   remains down**. It restores A's backend and checks A restarted while B's PID
   and the container ID stayed unchanged.

4. Wait for the **“A is back”** prompt before creating another Issue. In A's
   browser, create and assign a fresh marked **Todo** Issue to the same
   synthetic Agent in the same Workspace. Leave B unchanged. Check the new
   Issue and Run UUIDs, then paste the new task event path at the prompt
   (absolute or repository-relative, with no shell quotes). An event already
   present when the prompt appeared is refused.

   The runner verifies the fresh reply and completion on A while B is still
   running, then completes B. All three Runs must reach `completed` on their
   respective servers, each reply must appear on its intended Issue only, and
   neither B nor the shared container may restart. Only the final
   `TWO-SERVICE E2E PASS` is a pass. An API failure, missing scope, timer
   expiration, local receipt alone, or non-running status is not a pass.

   Preserve sanitized output (Git SHA, versions, image digests, Issue/Run
   IDs, receipt statuses, server result). Do not capture Docker env/config,
   PATs or browser cookies. On any failure, inspect the server Issues and the
   receipts before retrying. Do not resubmit a request just because delivery
   is unclear. The original A/B pair is reserved at the start of an attempt,
   so a rerun requires fresh paired Issues with matching display keys.

### Optional multi-workspace check

On one synthetic server only, add a second empty workspace to the same
synthetic account. With no workspace allowlist, the same Daemon should register
in both workspaces. Use the actual second workspace UUID to scope its task
and verify it never appears in the first workspace mailbox. This is distinct
from the mandatory test above, which proves two servers in one container.

## Stop and keep evidence

Revoke both disposable tokens in their respective local browser sessions
before stopping the fixture.

```sh
./e2e/multi/run.sh down
```

Only this Compose project is stopped; local fixture files and volumes are
kept. If you no longer need them, remove only this fixture's ignored
`e2e/multi/.local` and its volumes manually. Do not run a system-wide prune.
The existing `e2e/run.sh` remains the older single-service compatibility test.

For offline checks, run `python3 -B e2e/multi/test_fixture.py` and
`sh -n e2e/multi/run.sh`. They do not start Docker or establish a real
Multica E2E pass.
