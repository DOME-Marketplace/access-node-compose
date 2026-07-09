#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [ -t 1 ]; then
  YELLOW='\033[93m'
  RESET='\033[0m'
else
  YELLOW=''
  RESET=''
fi

if [ ! -f .env.runtime ]; then
  if [ -f .env.runtime.example ]; then
    cp .env.runtime.example .env.runtime
    echo "Created: .env.runtime"
  else
    echo "Missing .env.runtime. Copy .env.runtime.example to .env.runtime and set DOME_ENVIRONMENT." >&2
    exit 1
  fi
fi

set -a
source .env.runtime
set +a

log_action() {
  if [ "${LOGGING:-false}" = "true" ]; then
    echo "[$(date '+%Y.%m.%d %H.%M.%S')] $1" >> "${LOG_FILE:-access-node-compose.log}"
  fi
}

case "${DOME_ENVIRONMENT:-}" in
  sbx|dev|dev2|prod)
    ;;
  *)
    echo "Unsupported DOME_ENVIRONMENT='${DOME_ENVIRONMENT:-}'. Use one of: sbx, dev, dev2, prod." >&2
    exit 1
    ;;
esac

VERSIONS_FILE="versions-${DOME_ENVIRONMENT}.env"
VERSIONS_REMOTE="${VERSIONS_REMOTE:-origin}"
STATE_FILE=".compose-update.state"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -z "${VERSIONS_BRANCH:-}" ]; then
    VERSIONS_BRANCH="$(git branch --show-current)"
    VERSIONS_BRANCH="${VERSIONS_BRANCH:-main}"
  fi

  if [ "$VERSIONS_BRANCH" != "main" ]; then
    echo -e "${YELLOW}Warning: fetching versions from '${VERSIONS_REMOTE}/${VERSIONS_BRANCH}', not 'main'.${RESET}"
  else
    echo "Fetching remote versions from ${VERSIONS_REMOTE}/${VERSIONS_BRANCH}..."
  fi
  if git fetch "$VERSIONS_REMOTE" "$VERSIONS_BRANCH" >/dev/null 2>&1; then
    REMOTE_FILE="refs/remotes/${VERSIONS_REMOTE}/${VERSIONS_BRANCH}:$VERSIONS_FILE"
    if git cat-file -e "$REMOTE_FILE" 2>/dev/null; then
      git show "$REMOTE_FILE" > "$VERSIONS_FILE"
      echo "Updated $VERSIONS_FILE from ${VERSIONS_REMOTE}/${VERSIONS_BRANCH}."
    else
      echo "Remote file $VERSIONS_FILE not found on ${VERSIONS_REMOTE}/${VERSIONS_BRANCH}; using local copy."
    fi
  else
    echo "Unable to fetch ${VERSIONS_REMOTE}/${VERSIONS_BRANCH}; using local copy."
  fi
else
  echo "Not a git repository; using local versions file."
fi

if [ ! -f "$VERSIONS_FILE" ]; then
  echo "Missing $VERSIONS_FILE for environment '$DOME_ENVIRONMENT'." >&2
  exit 1
fi

CURRENT_HASH="$(sha256sum "$VERSIONS_FILE" | awk '{print $1}')"
PREVIOUS_HASH=""
if [ -f "$STATE_FILE" ]; then
  PREVIOUS_HASH="$(cat "$STATE_FILE")"
fi

if [ "$CURRENT_HASH" = "$PREVIOUS_HASH" ]; then
  echo "No image updates detected for environment '$DOME_ENVIRONMENT'; nothing to do."
  exit 0
fi

echo "Image definitions changed; regenerating compose and updating stack..."
./wizard.py generate

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "Dry run enabled; skipping docker compose pull/up."
  log_action "update.sh) regenerated compose.yaml for '$DOME_ENVIRONMENT' (dry run)"
else
  docker compose -f compose.yaml pull
  docker compose -f compose.yaml up -d --remove-orphans
  log_action "update.sh) updated versions and restarted the stack (environment $DOME_ENVIRONMENT)"
fi

echo "$CURRENT_HASH" > "$STATE_FILE"
