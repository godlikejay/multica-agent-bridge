#!/bin/sh
# Only the isolated synthetic Multica stack; never use a production PAT.
set -eu
cd "$(dirname "$0")"
compose() { docker compose --env-file .local/compose.env -f compose.yaml "$@"; }
need_docker() {
  command -v docker >/dev/null 2>&1 || { echo 'Docker unavailable: container E2E NOT RUN.' >&2; exit 2; }
  docker compose version >/dev/null
}
case "${1:-help}" in
  verify)
    need_docker
    umask 077
    project="mab-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
    if ! mkdir .local; then
      echo 'verify needs a fresh checkout with no e2e/.local; existing data was not touched.' >&2
      exit 2
    fi
    export COMPOSE_PROJECT_NAME="$project"
    cleanup() {
      result=$?
      trap - EXIT
      if [ -f .local/compose.env ]; then
        if [ "$result" -ne 0 ]; then
          compose --profile runtime ps -a || :
        fi
        compose --profile runtime down --volumes --remove-orphans || result=1
      fi
      rm -rf -- .local || result=1
      exit "$result"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    sh ./run.sh up
    python3 -B seed.py auth
    sh ./run.sh bootstrap --token-file .local/pat
    python3 -B seed.py dispatch
    sh ./run.sh smoke --token-file .local/pat
    python3 -B seed.py invocation
    ;;
  up)
    need_docker
    umask 077
    mkdir -p .local/home .local/workspaces .local/bridge/inbox .local/bridge/outbox .local/bridge/state
    if [ ! -f .local/compose.env ]; then
      python3 -c 'import secrets,os; print("E2E_DB_PASSWORD="+secrets.token_hex(24)); print("E2E_JWT_SECRET="+secrets.token_hex(32)); print("LOCAL_UID="+str(os.getuid())); print("LOCAL_GID="+str(os.getgid()))' > .local/compose.env
    fi
    compose config --quiet
    compose pull postgres backend frontend
    compose --profile runtime build proxy runtime
    compose up -d postgres backend frontend proxy
    python3 -B fixture.py health
    echo 'Local browser setup: http://localhost:13000 (see e2e/README.md).'
    ;;
  bootstrap|smoke)
    cmd=$1; shift
    if [ "$cmd" = bootstrap ]; then need_docker; fi
    python3 -B fixture.py "$cmd" "$@" ;;
  logs) need_docker; compose --profile runtime logs --tail 80 runtime backend frontend proxy postgres ;;
  down)
    need_docker; compose --profile runtime down
    echo 'Stopped; synthetic volumes and local files retained.' ;;
  *) echo 'Usage: ./e2e/run.sh verify|up|bootstrap [--token-file PATH]|smoke [--token-file PATH]|logs|down' ;;
esac
