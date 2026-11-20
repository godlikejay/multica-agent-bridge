# Run Bridge with Docker

This stack contains **one container** with the Multica v0.11.1 Daemon and the
Bridge binary. It connects to an existing Multica instance; it does not run
PostgreSQL or another Multica server. No Go, Codex, Hermes installation or
model API key is required on the Docker host.

The image definition is shared with the [isolated acceptance stack](../e2e/README.md).
This project does not yet publish a prebuilt image. Build from this checkout
with Docker Compose v2:

```sh
cp .env.example .env
# Edit .env to set the Multica API and web HTTPS URLs.
# Run as a regular host user, not root.
export BRIDGE_UID="$(id -u)" BRIDGE_GID="$(id -g)"
(umask 077; mkdir -p .bridge/home .bridge/workspaces .bridge/inbox .bridge/outbox .bridge/state)
docker compose build bridge
docker compose run --rm --no-deps bridge --profile bridge version
docker compose run --rm --no-deps --entrypoint multica-bridge bridge acp --version
```

On a Docker network, the API URL must be reachable **from the container**.
The host's loopback address is not the container's loopback. Use a trusted
HTTPS endpoint. The HTTP fixture exception is confined to the isolated test stack.
If you independently build, verify, and publish an image, set `BRIDGE_IMAGE`
to its digest, then use `docker compose pull bridge` and `docker compose up -d
--no-build bridge` instead of building locally.

## First login and registration

Use an authorized Multica test workspace first. A dedicated runtime owner
account with access only to the intended workspace is preferable to a personal
account. In the existing Multica web UI, generate a token for that account.
Pass the token on stdin, not in arguments, Compose config, chat, or logs.
The following block uses Bash and the login token is hidden from terminal echo:

```bash
(
  IFS= read -rsp 'Multica token: ' pat
  printf '\n'
  printf '%s\n' "$pat" | docker compose run --rm -T --no-deps bridge --profile bridge login --token
)
docker compose run --rm --no-deps bridge --profile bridge config set workspace_id WORKSPACE_UUID
docker compose up -d bridge
docker compose exec bridge multica --profile bridge daemon status
```

The login persists in `.bridge/home`. Treat that directory like a credential
store. Keep it and the mailbox out of Git, do not share the container's entire
mount tree with a coding agent, and never mount the host home, project checkout,
credentials, or Docker socket in this container. A successful daemon status is
not proof that Multica can use the runtime: in the Multica UI, confirm the
intended bridge runtime is online, bind a test agent to that exact runtime and
select the bridge model. Keep the runtime private, and don't configure custom
arguments, extra environments, repositories or real coding agents for it.
A workspace owner/admin is needed if the team chooses a Custom Runtime Profile.

The runtime has no published ports. Its root filesystem is read-only and it
runs without root or added capabilities. Only the listed directories are
mounted. An **operator** writes `.bridge/inbox` on the host; the container
cannot write it. The Bridge writes `.bridge/outbox` and `.bridge/state`.
A Skill or approval prompt is not a permission boundary when another process
runs with the same host user privileges.

For a first test, assign an invented issue, check its task event in the outbox,
reply explicitly through the [mailbox tool](../skills/multica-bridge/SKILL.md),
and verify completion in Multica. The full [E2E runbook](../e2e/README.md) also
covers restarting and stale requests.

## Stop and upgrade

`docker compose down` stops the runtime, not the existing Multica service.
Keep `.bridge/home` (daemon identity) and `.bridge/state` (replay protection);
do not copy credentials into an image. Verify no active Run before upgrading,
then update the checkout and rebuild, or change to a reviewed image digest.
The upstream idle watchdog and task-token limits still apply.

The image definition and Compose configuration are not evidence of a tested
image until built on Docker and exercised against a compatible Multica.
