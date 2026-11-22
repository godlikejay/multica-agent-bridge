#!/bin/sh
# Explicit synthetic-only Compose fixture. Never source token values in the shell.
set -eu
cd "$(dirname "$0")"
compose() { docker compose --env-file .local/compose.env -f compose.yaml "$@"; }
need_docker() {
  command -v docker >/dev/null 2>&1 || { echo 'Docker unavailable: two-service E2E NOT RUN.' >&2; exit 2; }
  docker compose version >/dev/null
}
endpoint() {
  case "${1:-}" in
    a|b) ;;
    *) echo 'Use synthetic endpoint a or b.' >&2; exit 2 ;;
  esac
}
mc() {
  connection=$1; shift
  compose --profile runtime exec -T -e HOME="/bridge-runtime/$connection/home" \
    runtime multica --profile "$connection" "$@"
}
case "${1:-help}" in
  init)
    umask 077
    python3 -B fixture.py init ;;
  up)
    need_docker
    compose --profile runtime config --quiet
    compose pull postgres-a backend-a frontend-a postgres-b backend-b frontend-b
    compose --profile runtime build proxy-a runtime
    compose up -d --no-build proxy-a proxy-b
    python3 -B fixture.py health
    python3 -B fixture.py urls
    echo 'See README for signup and assignment.' ;;
  urls) python3 -B fixture.py urls ;;
  token)
    endpoint "${2:-}"
    python3 -B fixture.py token "$2" ;;
  runtime)
    need_docker
    python3 -B fixture.py credentials
    compose --profile runtime up -d runtime
    echo 'Confirm runtime registration on both sites; starting one container is not proof.' ;;
  versions)
    need_docker
    compose --profile runtime run --rm --no-deps --entrypoint multica runtime version
    compose --profile runtime run --rm --no-deps runtime acp --version ;;
  status)
    need_docker
    endpoint "${2:-}"
    mc "$2" daemon status --output json ;;
  workspace)
    need_docker
    endpoint "${2:-}"
    test "$#" -eq 3
    mc "$2" workspace switch "$3"
    (umask 077; mkdir -p ".local/runtime/$2/workspaces/demo") ;;
  cli)
    need_docker
    endpoint "${2:-}"
    connection=$2; shift 2
    mc "$connection" "$@" ;;
  accept)
    need_docker
    test "$#" -eq 3 || { echo 'Usage: ./e2e/multi/run.sh accept A_TASK_EVENT B_TASK_EVENT' >&2; exit 2; }
    a=$2 b=$3
    case "$a" in e2e/multi/*) a=${a#e2e/multi/} ;; esac
    case "$b" in e2e/multi/*) b=${b#e2e/multi/} ;; esac
    python3 -B fixture.py accept "$a" "$b" ;;
  logs)
    need_docker
    compose --profile runtime logs --tail 100 runtime backend-a backend-b proxy-a proxy-b ;;
  down)
    need_docker
    compose --profile runtime down
    echo 'Stopped only the two-service fixture. Volumes and synthetic local state retained.' ;;
  *)
    echo 'Usage: ./e2e/multi/run.sh init|up|urls|token a|b|runtime|versions|status a|b|workspace a|b UUID|cli a|b ...|accept A_TASK_EVENT B_TASK_EVENT|logs|down' >&2
    exit 2 ;;
esac
