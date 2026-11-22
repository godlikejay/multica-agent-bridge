# Run Bridge with Docker

English | [简体中文](zh-CN/deployment.md)

This stack contains **one container** with the Multica v0.11.1 Daemon and the
Bridge binary. It connects to an existing Multica instance; it does not run
PostgreSQL or another Multica server. No Go, Codex, Hermes installation or
model API key is required on the Docker host.

Clone this repository and prepare the configuration and private state
directories before pulling the image or building it locally:

```sh
git clone https://github.com/godlikejay/multica-agent-bridge.git
cd multica-agent-bridge
[ -f .env ] || cp .env.example .env
# Run as a regular host user, not root.
id -u
id -g
(umask 077; mkdir -p .bridge/home .bridge/workspaces .bridge/inbox .bridge/outbox .bridge/state)
```

Save the numeric outputs of `id -u` and `id -g` as `BRIDGE_UID` and
`BRIDGE_GID` in the ignored root `.env`. Do not put a token there. Compose
reads the file in fresh terminals; exported variables take precedence.

Set `MULTICA_SERVER_URL=https://api.multica.ai` for Multica Cloud. For
self-hosting, use the HTTPS API base URL supplied by your administrator. This
is where Docker signs in, receives Runs, and sends updates. Include
`https://` and any required port, but not an individual route like
`/api/me`. The URL must be reachable from inside Docker.

The Multica website (Cloud: [multica.ai](https://multica.ai)) is for you to
create a token, select a workspace, and assign Issues. You do not need to put
that address in the production configuration. An existing
`MULTICA_APP_URL` remains supported as an optional upstream browser-flow
override; it is not used for token login or mailbox traffic.

### Choose an image

The [main build workflow](../.github/workflows/container.yml) publishes the
Linux amd64 and arm64 image to GHCR with a moving `latest` tag and a
`sha-<full-git-commit>` tag. For reproducible deployments, use the image
digest shown in the Actions run summary.

In `.env`, set `BRIDGE_IMAGE=ghcr.io/godlikejay/multica-agent-bridge:latest`
or use the reviewed digest. Check the resolved image and pull it:

```sh
docker compose config --images
docker compose pull bridge
```

If the GHCR package is private, pulling requires an authorized GitHub login.
An administrator can [make the package public](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
to enable anonymous pulls. GitHub makes the first publication private by default,
even if the source repository is public.

To build instead, leave `BRIDGE_IMAGE` empty in `.env`, clear any matching
shell export, and run `docker compose build bridge`.
With either option, check the binaries before logging in:

```sh
docker compose run --rm --no-deps bridge --profile bridge version
docker compose run --rm --no-deps --entrypoint multica-bridge bridge acp --version
```

## First login and registration

Use an authorized Multica test workspace first. A dedicated runtime owner
account with access only to the intended workspace is preferable to a personal
account. Sign in to the same Multica site in your browser. Go to **Settings → API Token**
([Multica Cloud shortcut](https://multica.ai/settings?tab=tokens)), create a
named personal access token, and copy it when shown. Multica only shows the
full token once. This is a Multica token, not a GitHub token.
Pass the token on stdin, not in arguments, Compose config, chat, or logs.
The following block uses Bash and the login token is hidden from terminal echo:

```bash
(
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" | docker compose run --rm -T --no-deps bridge --profile bridge login --token
)
docker compose run --rm --no-deps bridge --profile bridge workspace list --full-id
```

Find your target workspace in the list. Replace `WORKSPACE_UUID` with its
full ID, not its name, then start the daemon:

```sh
docker compose run --rm --no-deps bridge --profile bridge config set workspace_id WORKSPACE_UUID
docker compose up -d --no-build bridge
docker compose exec bridge multica --profile bridge daemon status
```

The login persists in `.bridge/home`. Treat that directory like a credential
store. Keep it and the mailbox out of Git, do not share the container's entire
mount tree with a coding agent, and never mount the host home, project checkout,
credentials, or Docker socket in this container. A successful daemon status is
not proof that Multica can use the runtime: in the Multica UI, confirm the
runtime named `multica-agent-bridge` (or your `BRIDGE_DEVICE_NAME`) is
online. Create or edit a test Agent, bind it to that exact runtime and select
**Hermes**. The runtime uses the Bridge behind that label, not a model API.
Keep it private; do not configure custom arguments, extra environments,
repositories or real coding agents for it.
A workspace owner/admin is needed if the team chooses a Custom Runtime Profile.

## Concurrent Issues

The Compose runtime defaults to **4 active Runs across the daemon**. Set
`BRIDGE_MAX_ACTIVE_RUNS` in `.env` to a positive integer and recreate the
container to change that limit (use `1` to run serially).
The Agent's own **Max concurrent tasks** and any shared Runtime limit in
Multica also apply: configure both to permit the desired concurrency.
Multica v0.11.1 defaults to 6 per Agent and no extra Runtime cap. To change
the shared cap, an authorized operator can use
`multica --profile bridge runtime concurrency RUNTIME_ID 4` in the
container; `none` clears that cap. These settings do not allow two Runs of the
same Agent on the same Issue to execute together.

The runtime has no published ports. Its root filesystem is read-only and it
runs without root or added capabilities. Only the listed directories are
mounted. An **operator** writes `.bridge/inbox` on the host; the container
cannot write it. The Bridge writes `.bridge/outbox` and `.bridge/state`.
Place each request in `.bridge/inbox/<issue_id>/<request_id>.json`; create the
Issue directory on the host (the mailbox tool does this). Keep requests
scoped to the Issue, Run and epoch from the current task event. Finishing
one Issue does not finish or stop a concurrent Issue.
A Skill or approval prompt is not a permission boundary when another process
runs with the same host user privileges.

## Receive and reply to your first Issue

Put a small test Issue in **Todo** and assign it to the Agent you just connected.
If Multica prompts for a local directory, choose an empty directory under
`/workspaces` in the container, not a real project checkout. In the Bridge
checkout, run the following. Replace `EVENT_ID` with the task event ID from
`list`, not the Issue number.

```sh
export BRIDGE_ROOT="$PWD/.bridge"
tool=skills/multica-bridge/scripts/mailbox.py
python3 "$tool" list
python3 "$tool" read EVENT_ID
draft_dir=$(mktemp -d)
printf 'Bridge received this test Issue.\n' > "$draft_dir/reply.txt"
python3 "$tool" draft EVENT_ID --kind reply --body-file "$draft_dir/reply.txt" > "$draft_dir/reply.json"
python3 "$tool" submit "$draft_dir/reply.json"
```

Check the destination and text in the terminal before confirming. The first
success signal is the task in the local `outbox`; the second is the new
comment on the original Multica Issue. If the container is running but
`list` stays empty, check that the runtime is online in Multica and that the
Issue was assigned to its Agent. You do not need a coding agent for this test.

Once that works, use the [Agent Skill](agent-integration.md) to read and draft
from Codex, agy, or OMP. Keep the final submission as an explicit operator action.

## Stop and upgrade

`docker compose down` stops the runtime, not the existing Multica service.
Keep `.bridge/home` (daemon identity) and `.bridge/state` (replay protection);
do not copy credentials into an image. Verify no active Run before upgrading,
then update the checkout and rebuild, or change to a reviewed image digest.
The upstream idle watchdog and task-token limits still apply.
On upgrade from the single-Run layout, flat inbox JSON files are no longer
consumed. Keep the old files for inspection; do not bulk move/replay them.
Inspect receipts and the current Run before submitting a new request.

The image definition and Compose configuration are not evidence of a tested
image until built on Docker and exercised against a compatible Multica.
