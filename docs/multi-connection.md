# Multiple Multica connections in one container

English | [简体中文](zh-CN/multi-connection.md)

Use this mode to connect a single runtime container to multiple Multica
services, or to make distinct connections using separate accounts on one
service. One configuration starts one Multica Daemon per connection. These
Daemons share the Bridge executable, not credentials, working directories or
mailbox state. They own the remote connections and invoke the Bridge on demand
for Runs. Nothing launches a local coding agent.

For the existing single-connection layout, keep using [the original Compose
file](deployment.md). It does not require a configuration or mailbox migration.

## Set up

At the repository root, as a regular non-root user:

```sh
[ -f .env ] || cp .env.example .env
id -u
id -g
(umask 077; mkdir -p .bridge-multi/secrets .bridge-multi/runtime \
    .bridge-multi/inbox .bridge-multi/outbox .bridge-multi/state)
cp -n connections.example.json .bridge-multi/connections.json
```

Save the numeric `id -u` and `id -g` outputs as `BRIDGE_UID` and `BRIDGE_GID`
in the ignored root `.env`. Compose reads these again in fresh terminals. The
URL fields in `.env` are only for single-connection mode; do not put tokens there.

Edit `.bridge-multi/connections.json` and set each connection's HTTPS
`server_url` (API), stable local `id`, and separate read-only `token_file`.
An existing `app_url` remains supported as an optional upstream browser
setting; the production token flow does not need it. Set the paths
to files under `/run/secrets/`. In the matching Multica site, use an authorized
account and [create a PAT](deployment.md#first-login-and-registration). Enter each token on the Docker host, not in a chat,
environment variable, or command argument. For example, in Bash:

```bash
(
  umask 077
  IFS= read -rsp 'Cloud Multica PAT: ' pat; printf '\n'
  printf '%s\n' "$pat" > .bridge-multi/secrets/cloud.pat
)
(
  umask 077
  IFS= read -rsp 'Work Multica PAT: ' pat; printf '\n'
  printf '%s\n' "$pat" > .bridge-multi/secrets/work.pat
)
```

In the root `.env`, set `BRIDGE_IMAGE=ghcr.io/godlikejay/multica-agent-bridge:latest`
or a reviewed digest as in the [single-connection setup](deployment.md#choose-an-image).

```sh
docker compose -f compose.multi.yaml config --images
docker compose -f compose.multi.yaml pull bridge
docker compose -f compose.multi.yaml up -d --no-build bridge
docker compose -f compose.multi.yaml logs --tail 100 bridge
```

To build instead, leave `BRIDGE_IMAGE` empty in `.env`, clear any matching
shell export, and run
`docker compose -f compose.multi.yaml up -d --build`.

Check that each intended runtime is online in its own Multica site. Bind the
test Agent to that runtime and choose the Bridge model as in [single-connection
setup](deployment.md#first-login-and-registration). A container being up is
not evidence both Daemons registered. If a service is offline, the other can
continue; read the per-connection status in the logs. To query the actual
Daemon status for one connection, use its configured ID (here `work`):

```sh
docker compose -f compose.multi.yaml exec -T \
    -e HOME=/bridge-runtime/work/home bridge \
    multica --profile work daemon status --output json
```

Optional `workspace_ids` restricts which workspaces Bridge accepts for that
connection. It **does not** restrict Multica runtime registration: the upstream
Daemon may register with other workspaces visible to its login. For registration
scope, use an account that has access only to the intended workspaces. Multiple
projects on one workspace do not need more Daemons. `max_active_runs` is a
per-connection cap, also subject to upstream limits.

Keep the runtime and mailbox directories together to preserve daemon identity
and replay protection. The runtime directory contains the saved login; treat
it as credential storage. The mounted PAT file must still be readable at
startup. To rotate a PAT for the **same account**, update its secret file and
restart the container. To switch servers **or accounts**, use a new connection
ID and keep the old mailbox separate; never move unprocessed requests.

Avoid planned stops or upgrades with active Runs. The Bridge supervisor gives
each Daemon up to 40 seconds to shut down, and Compose gives the container 45
seconds so it does not cut that short. Shutdown is not a delivery guarantee;
if the final receipt is uncertain, check the original server before retrying.
Use `docker compose -f compose.multi.yaml down` to stop; it doesn't delete host
files.

## Use the mailbox

```sh
tool=skills/multica-bridge/scripts/mailbox.py
root="$PWD/.bridge-multi"
python3 "$tool" --root "$root" scopes
python3 "$tool" --root "$root" --connection work --workspace WORKSPACE_UUID list
python3 "$tool" --root "$root" --connection work --workspace WORKSPACE_UUID status ISSUE_UUID
```

Select the same connection and workspace for read, pickup, draft, submit and
receipt. Submission previews show both; a draft from another connection or
workspace is rejected, even when display keys or IDs coincide. Issue pickup
still records your chosen local worktree; project names do not determine
delivery routing. Do not give a local coding agent the runtime or secrets
directory.

One container prevents accidental routing between connections; it is not a
security boundary between tenants. If your operating policy requires true
separation, deploy separate containers with separate mounts/credentials.

## Verify

Run the normal offline tests with `make check build`. For a fresh Docker
host, use the [two-service acceptance runbook](../e2e/multi/README.md). A
single-server parallel test or a static Compose check is not proof of
multi-connection behavior.
