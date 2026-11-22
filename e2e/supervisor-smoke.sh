#!/bin/sh
# Exercise the production Compose entrypoint and control socket without Multica.
set -eu
cd "$(dirname "$0")/.."
repo=$(pwd)
command -v docker >/dev/null 2>&1 || {
  echo 'Docker unavailable: supervisor container smoke NOT RUN.' >&2
  exit 2
}
docker compose version >/dev/null
BRIDGE_UID=$(id -u)
BRIDGE_GID=$(id -g)
[ "$BRIDGE_UID" -ne 0 ] || {
  echo 'Run the supervisor smoke as a non-root Docker user.' >&2
  exit 2
}

umask 077
project="mab-supervisor-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
fixture=$(mktemp -d "${TMPDIR:-/tmp}/mab-supervisor.XXXXXXXX")
fixture=$(cd "$fixture" && pwd)
BRIDGE_IMAGE="$project:fixture"
BRIDGE_CONFIG="$fixture/connections.json"
BRIDGE_SECRETS_DIR="$fixture/secrets"
BRIDGE_DATA_DIR="$fixture/data"
export BRIDGE_UID BRIDGE_GID BRIDGE_IMAGE BRIDGE_CONFIG BRIDGE_SECRETS_DIR BRIDGE_DATA_DIR

compose() {
  docker compose --env-file "$fixture/env" -p "$project" \
    -f "$repo/deploy/compose.yaml" -f "$fixture/isolation.yaml" "$@"
}
ctl() { compose exec -T bridge multica-bridge connection "$@"; }
cleanup() {
  result=$?
  trap - EXIT
  if [ "$result" -ne 0 ] && [ -n "${container:-}" ]; then
    compose logs --tail=40 bridge >&2 || :
  fi
  if [ "${started:-0}" -eq 1 ]; then
    compose down --remove-orphans || result=1
  fi
  if [ "${built:-0}" -eq 1 ]; then
    docker image rm "$BRIDGE_IMAGE" >/dev/null || result=1
  fi
  rm -rf -- "$fixture" || result=1
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$BRIDGE_SECRETS_DIR" "$BRIDGE_DATA_DIR/runtime" "$BRIDGE_DATA_DIR/inbox" \
  "$BRIDGE_DATA_DIR/outbox" "$BRIDGE_DATA_DIR/state"
: > "$fixture/env"
cat > "$fixture/isolation.yaml" <<'YAML'
services:
  bridge:
    network_mode: none
    pull_policy: never
YAML
cat > "$BRIDGE_CONFIG" <<'JSON'
{
  "connections": [
    {"id": "alpha", "server_url": "https://alpha.invalid", "token_file": "/run/secrets/alpha.pat"},
    {"id": "beta", "server_url": "https://beta.invalid", "token_file": "/run/secrets/beta.pat"}
  ]
}
JSON

# No PAT files exist. A started connection retries locally before any login.
# Use expected enabled/state pairs and require exactly the two fixture rows.
wait_status() {
  attempt=0
  while [ "$attempt" -lt 30 ]; do
    if ctl status > "$fixture/status.json" 2>"$fixture/status.err" &&
      python3 - "$fixture/status.json" "$@" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as status:
        rows = json.load(status)["connections"]
    expected = dict(value.split("=", 1) for value in sys.argv[2:])
    matches = len(rows) == len(expected) and {
        row["id"]: f'{row["enabled"]}:{row["state"]}' for row in rows
    } == expected
except (OSError, KeyError, TypeError, ValueError):
    matches = False
raise SystemExit(not matches)
PY
    then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  printf 'Supervisor status did not reach: %s\n' "$*" >&2
  cat "$fixture/status.json" "$fixture/status.err" >&2
  return 1
}

docker build --tag "$BRIDGE_IMAGE" .
built=1
started=1
compose up -d bridge
container=$(compose ps -q bridge)
[ -n "$container" ] || { echo 'Supervisor container did not start.' >&2; exit 1; }
wait_status alpha=True:retrying beta=True:retrying
# Both mounts must accept their per-connection binding before the missing PAT.
for mount in runtime state; do
  for id in alpha beta; do
    if [ "$(cat "$BRIDGE_DATA_DIR/$mount/$id/.server-url")" != "https://$id.invalid" ]; then
      echo "Missing or wrong $mount endpoint binding for $id." >&2
      exit 1
    fi
  done
done

ctl stop alpha
wait_status alpha=False:stopped beta=True:retrying
ctl stop --all
wait_status alpha=False:stopped beta=False:stopped
[ "$(compose ps -q bridge)" = "$container" ] || {
  echo 'Stopping all connections must leave the same container running.' >&2
  exit 1
}

compose restart bridge
wait_status alpha=False:stopped beta=False:stopped
ctl start beta
wait_status alpha=False:stopped beta=True:retrying
echo 'SUPERVISOR CONTAINER SMOKE PASS: independent connection control and restart persistence (no real daemon login).'
