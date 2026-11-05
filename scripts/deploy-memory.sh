#!/usr/bin/env bash
# One-command memory deployment; normal provider credentials and .env are preserved.
set -Eeuo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$TASK_ROOT/.tools/bin:$PATH"
cd "$TASK_ROOT"
ACTION="${1:-up}"
if [[ $# -gt 0 ]]; then shift; fi
SETUP_OPTIONS=()
INSTALL_OPTIONS=()
CHECK_OPTIONS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local-auth|--external-auth) SETUP_OPTIONS+=("$1"); shift ;;
    --origin) [[ $# -ge 2 ]] || { echo '--origin needs an HTTPS URL'; exit 2; }; SETUP_OPTIONS+=(--origin "$2"); shift 2 ;;
    --skip-system-deps|--all-providers) INSTALL_OPTIONS+=("$1"); shift ;;
    --online) CHECK_OPTIONS+=(--online); shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$ACTION" in
  help|--help|-h)
    echo 'Usage: bash scripts/deploy-memory.sh [up|install|init|migrate|start|restart|stop|status|check|credentials] [--local-auth|--external-auth] [--origin https://host] [--skip-system-deps] [--all-providers] [check --online]'
    exit 0 ;;
  up|install)
    if [[ " ${SETUP_OPTIONS[*]} " == *" --local-auth "* ]]; then
      command -v docker >/dev/null && docker compose version >/dev/null || { echo 'Install Docker Engine and Compose for --local-auth, or configure an external OIDC provider.' >&2; exit 1; }
    fi
    bash scripts/deploy.sh install "${INSTALL_OPTIONS[@]}"
    backend/.venv/bin/python scripts/memory-deploy.py init "${SETUP_OPTIONS[@]}"
    backend/.venv/bin/python scripts/memory-deploy.py migrate
    backend/.venv/bin/python scripts/memory-deploy.py check
    [[ "$ACTION" == install ]] && exit 0
    exec backend/.venv/bin/python scripts/memory-deploy.py restart ;;
  init) exec backend/.venv/bin/python scripts/memory-deploy.py init "${SETUP_OPTIONS[@]}" ;;
  check) exec backend/.venv/bin/python scripts/memory-deploy.py check "${CHECK_OPTIONS[@]}" ;;
  migrate|start|restart|stop|status|credentials) exec backend/.venv/bin/python scripts/memory-deploy.py "$ACTION" ;;
  *) echo 'Unknown action. Run: bash scripts/deploy-memory.sh help' >&2; exit 2 ;;
esac
