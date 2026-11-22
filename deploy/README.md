# Install from the published image

English | [简体中文](README.zh-CN.md)

Use Docker Compose. You do not need to clone the repository, build the
image, or install Multica Daemon separately. Use your existing Multica
account and start in a test workspace. The image supports Linux x86-64
and ARM64, including Apple Silicon with Docker Desktop.

## 1. Download the configuration

Run as your regular user, not root:

```sh
mkdir -p multica-bridge
cd multica-bridge
base=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main
curl -fsSLo compose.yaml "$base/deploy/compose.yaml"
curl -fsSLo connections.json "$base/deploy/connections.example.json"
(umask 077; mkdir -p secrets data/runtime data/inbox data/outbox data/state)
printf 'BRIDGE_UID=%s\nBRIDGE_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

This is for a new directory. Keep existing configuration and data on upgrade.
The example already points to Multica Cloud. For self-hosted Multica, edit
`connections.json`: set `server_url` to the HTTPS API base URL reachable from
Docker. Do not add an API route or put the token in the JSON. For multiple
accounts or services, add entries with unique stable IDs and one token file
per entry. Only use a token issued by the service configured for that entry.
See [connection settings](#connection-settings) for the full field list.

## 2. Save a token and start

Sign in to that Multica site, go to **Settings → API Token** ([Cloud link](https://multica.ai/settings?tab=tokens)),
and create a personal access token. The full value is shown only once.
In Bash, save it to the file named in the example:

```bash
(
  umask 077
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" > secrets/cloud.pat
)
```

If the GHCR package is private, authenticate Docker to GHCR with an account
that can read the package. This is separate from your Multica token.

```sh
docker compose up -d
docker compose logs --tail 80 bridge
```

Compose pulls the image for your platform if needed. The container signs in
using the mounted token and runs the daemon. In Multica, confirm
`multica-bridge-cloud` is online (the suffix is the connection ID). Bind a
test Agent to that runtime, choose **Hermes**, and assign a test Issue. If
Multica asks you to select a local working directory, use an empty test
directory under `/bridge-runtime/cloud/workspaces` in the container.
A new task event under `data/outbox/cloud/` confirms the first delivery.
See the [mailbox guide](../docs/agent-integration.md) for handling the event.

## Reply to the first Issue

Run from the install directory. This first-reply example uses a one-off test
command with Python 3; for day-to-day work, [save your project routes](../docs/agent-integration.md#configure-once-per-project).
If you do not know the workspace UUID yet, use `scopes` to discover it after
the first task arrives. The event ID from `list` is not the Issue number.
```sh
base=https://raw.githubusercontent.com/godlikejay/multica-agent-bridge/main
curl -fsSLo mailbox.py "$base/skills/multica-bridge/scripts/mailbox.py"
python3 mailbox.py --root "$PWD/data" scopes
```

```sh
mailbox() { python3 mailbox.py --root "$PWD/data" --connection cloud --workspace WORKSPACE_UUID "$@"; }
mailbox list
mailbox read EVENT_ID
draft_dir=$(mktemp -d)
printf 'Bridge received this test Issue.\n' > "$draft_dir/reply.txt"
mailbox draft EVENT_ID --kind reply --body-file "$draft_dir/reply.txt" > "$draft_dir/reply.json"
mailbox submit "$draft_dir/reply.json"
```

Confirm the destination and text in the terminal. Verify that the reply
appears on the original Multica Issue.

## Connection settings

Use the same `connections.json` and Compose for one or more connections.
Each entry defines one daemon for a service and account. Multiple projects
within the same workspace do not need separate daemons.

| Setting | Meaning |
| --- | --- |
| `id` | Unique, stable local ID. It identifies the runtime and the mailbox folders. |
| `server_url` | HTTPS API base URL reachable from Docker. |
| `token_file` | Path under `/run/secrets/` in the container, such as `/run/secrets/cloud.pat`. With the default mounts, the matching host file is `secrets/cloud.pat`. |
| `workspace_ids` | Optional workspace allowlist for tasks accepted by Bridge. It does **not** limit daemon registration. Use an account with access only to the intended workspaces for that. |
| `max_active_runs` | Optional positive per-connection concurrency cap, subject to limits in Multica. |

Save a separate token file for each entry. To apply changes to
`connections.json`, wait for active Runs to finish, then run
`docker compose up -d --force-recreate bridge`. Verify each configured runtime in
its own Multica site before assigning a test Issue. If a connection does not
come online, check the Compose logs for its ID. For daemon diagnostics in
addition to the connection status below, substitute its ID in both places:

```sh
docker compose exec -T -e HOME=/bridge-runtime/cloud/home bridge \
  multica --profile cloud daemon status --output json
```

## Start or stop connections

From the Compose install directory, leave the Bridge container running and
use the connection ID from `connections.json` (here, `company`):

```sh
docker compose exec bridge multica-bridge connection status
docker compose exec bridge multica-bridge connection status company
docker compose exec bridge multica-bridge connection stop company
docker compose exec bridge multica-bridge connection start company
```

Use `connection stop --all` or `connection start --all` with the same prefix
to control all configured connections. Stopping a connection can interrupt
its active Runs, so let them finish first when practical. It does not finish
Runs on your behalf or delete credentials, runtime identity, or mailbox
data. If all connections are stopped, the container stays up and idle.
Stopped settings are saved under the runtime mount and survive container
restarts. Use `connection start` to resume a stopped connection.

`connection status` reports local daemon state, not registration in Multica.
After starting a connection, confirm its runtime shows as online in the
corresponding Multica site before assigning work. These commands use a local
Unix socket inside the container; no control port or Docker socket mount is
needed. To run from another directory, use the installation's Compose and
environment files:

```sh
docker compose --project-directory /path/to/install \
  --env-file /path/to/install/.env -f /path/to/install/compose.yaml \
  exec bridge multica-bridge connection status
```

### Set up the `mab` shortcut

For a shortcut that works from any directory, save this as a user-owned
file named `mab` in a directory already on your `PATH`, then run
`chmod u+x /path/to/mab`. Use the absolute path to your existing install:

```sh
#!/bin/sh
install_dir='/absolute/path/to/bridge-install'
exec docker compose --project-directory "$install_dir" \
  --env-file "$install_dir/.env" -f "$install_dir/compose.yaml" \
  exec -T bridge multica-bridge connection "$@"
```

Use the same options as the existing deployment, including any custom file
names, additional Compose files, or project name (`-p`). Use different
shortcut names for different installations.

To have an agent do it, say: “Follow the MAB README and set up the `mab`
shortcut for my existing Compose install at `/path/to/install`. Verify with
`mab status` only.” On the Docker host, use the confirmed install and
project, don't silently overwrite an existing command, and report the
path written. Setup needs paths, not token values. Do not change Docker
permissions, start or stop connections, or modify the deployment. The
shortcut uses the operator's existing Docker access and is separate from
the mailbox Skill.

## Files, upgrades and isolation

| File or directory | Purpose | Optional path setting in `.env` |
| --- | --- | --- |
| `connections.json` | API address and token file for each connection | `BRIDGE_CONFIG` |
| `secrets/` | Token files mounted read-only as `/run/secrets` | `BRIDGE_SECRETS_DIR` |
| `data/` | Local inbox/outbox, private daemon identity and replay state | `BRIDGE_DATA_DIR` |

Create custom paths before starting Compose. Protect `secrets/` and
`data/runtime/`; give a coding agent only the mailbox access it needs.
An agent needs Inbox write access to send replies or finish Runs on request.
For setup and approval rules, see the [coding agent guide](../docs/agent-integration.md).
Use separate containers, mounts and credentials for mutually untrusted environments.

When no Runs are active, `docker compose pull && docker compose up -d` upgrades
the image without deleting data. Set `BRIDGE_IMAGE` to a reviewed
`ghcr.io/godlikejay/multica-agent-bridge@sha256:…` digest to pin it.
Keep `data/runtime` and `data/state` together. To rotate a token for the same
account, update its secret file and run `docker compose restart bridge` once
active Runs finish. For a different account or service, use a new connection ID;
do not copy unprocessed requests across connections.
