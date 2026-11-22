#!/bin/sh
# No production resources, real coding agents, cloud models, or host home mounts.
set -eu
cd "$(dirname "$0")"
compose() { docker compose --env-file .local/compose.env -f compose.yaml "$@"; }
need_docker() { command -v docker >/dev/null 2>&1 || { echo 'Docker unavailable: container E2E NOT RUN.' >&2; exit 2; }; docker compose version >/dev/null; }
case "${1:-help}" in
  init)
    umask 077
    mkdir -p .local/home .local/workspaces/a .local/workspaces/b .local/bridge/inbox .local/bridge/outbox .local/bridge/state
    if [ ! -f .local/compose.env ]; then
      python3 -c 'import secrets,os; print("E2E_DB_PASSWORD="+secrets.token_hex(24)); print("E2E_JWT_SECRET="+secrets.token_hex(32)); print("LOCAL_UID="+str(os.getuid())); print("LOCAL_GID="+str(os.getgid()))' > .local/compose.env
    fi
    echo 'Isolated synthetic directories ready. Do not put real credentials/data here.' ;;
  up)
    need_docker
    compose config --quiet
    compose pull postgres backend frontend
    compose --profile runtime build proxy runtime
    compose up -d postgres backend frontend proxy
    python3 fixture.py health
    python3 -B fixture.py urls
    echo 'Human setup required (see e2e/README.md).' ;;
  urls) python3 -B fixture.py urls ;;
  login)
    need_docker
    compose --profile runtime run --rm runtime --profile e2e --server-url http://backend:8080 login --token ;;
  workspaces)
    need_docker
    compose --profile runtime run --rm --no-deps runtime --profile e2e workspace list --full-id ;;
  workspace)
    need_docker; test "$#" -eq 2
    compose --profile runtime run --rm runtime --profile e2e config set workspace_id "$2" ;;
  runtime)
    need_docker
    compose --profile runtime up -d runtime
    echo 'Check actual runtime registration in UI; a started container is not readiness.' ;;
  status)
    need_docker
    compose --profile runtime exec runtime multica --profile e2e daemon status --output json ;;
  versions)
    need_docker
    compose --profile runtime run --rm runtime version
    compose --profile runtime run --rm --entrypoint multica-bridge runtime version
    compose --profile runtime run --rm --entrypoint multica-bridge runtime acp --version ;;
  restart) need_docker; compose --profile runtime restart runtime ;;
  health) python3 fixture.py health ;;
  follow-up)
    shift
    python3 -B fixture.py follow-up "$@" ;;
  smoke)
    event=${2:-${E2E_TASK:-}}
    [ -n "$event" ] || { echo 'Set E2E_TASK to the synthetic Run task event in e2e/.local/bridge/outbox. Complete manual bootstrap/assignment first.' >&2; exit 2; }
    # run.sh executes in e2e; accept the repository-relative example path too.
    case "$event" in e2e/*) event=${event#e2e/} ;; esac
    python3 fixture.py smoke "$event" ;;
  parallel)
    [ "$#" -ge 3 ] || { echo 'Provide two synthetic task events: ./e2e/run.sh parallel TASK_A_JSON TASK_B_JSON [--finish-a].' >&2; exit 2; }
    a=$2 b=$3; shift 3
    case "$a" in e2e/*) a=${a#e2e/} ;; esac
    case "$b" in e2e/*) b=${b#e2e/} ;; esac
    python3 fixture.py parallel "$a" "$b" "$@" ;;
  fixture|verify)
    cmd=$1; shift
    if [ "$cmd" = fixture ] && [ "$#" -gt 0 ]; then
      event=$1; shift; case "$event" in e2e/*) event=${event#e2e/} ;; esac
      python3 fixture.py "$cmd" "$event" "$@"
    else python3 fixture.py "$cmd" "$@"; fi ;;
  logs) need_docker; compose --profile runtime logs --tail 100 runtime backend frontend proxy postgres ;;
  down)
    need_docker; compose --profile runtime down
    echo 'Stopped; test state retained. No volumes/files deleted.' ;;
  *) echo 'Usage: ./e2e/run.sh init|up|urls|versions|login|workspaces|workspace UUID|runtime|status|restart|health|smoke TASK_JSON|parallel TASK_A_JSON TASK_B_JSON [--finish-a]|fixture TASK_JSON|verify RUN_ID|logs|down'
     echo '       ./e2e/run.sh follow-up (queue|restart) WORKSPACE_UUID OLD_TASK NEW_TASK OLD_HISTORY [--observed-trigger OLD_COMMENT]' ;;
esac
