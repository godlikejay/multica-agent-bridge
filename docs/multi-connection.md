# Multiple Multica connections in one container

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
export BRIDGE_UID="$(id -u)" BRIDGE_GID="$(id -g)"
(umask 077; mkdir -p .bridge-multi/secrets .bridge-multi/runtime \
    .bridge-multi/inbox .bridge-multi/outbox .bridge-multi/state)
cp -n connections.example.json .bridge-multi/connections.json
```

Edit the copy and replace the example API and web URLs. Every connection has
a stable local `id` and a separate `token_file` mounted read-only. Set the paths
to files under `/run/secrets/`. In the matching Multica site, use an authorized
account and create a PAT. Enter each token on the Docker host, not in a chat,
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
docker compose -f compose.multi.yaml up -d --build
docker compose -f compose.multi.yaml logs --tail 100 bridge
```

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

Keep the runtime and mailbox directories to preserve daemon identity and replay
protection. The login might be persisted in the runtime directory; treat it as
credential storage. Don't repoint an existing connection ID to a new service:
use a new ID and archive the old data instead of copying unprocessed requests.
Stop the container before changing its configuration or image. Use
`docker compose -f compose.multi.yaml down` to stop; it doesn't delete host
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
