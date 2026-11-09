#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

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

# The systemd timer and a manual wizard run must never overlap. The lock is
# opened read-only (enough for flock) so it still works when the file was
# created by another user, e.g. root running the systemd timer.
LOCK_FILE=".update.lock"
if command -v flock >/dev/null 2>&1; then
  [ -e "$LOCK_FILE" ] || : > "$LOCK_FILE"
  exec 9<"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another update is already running; skipping this run."
    exit 0
  fi
fi

# Refresh versions-<environment>.env from the versions API. On any API problem
# the last good file is kept and nothing is regenerated or restarted.
# `timeout` bounds the whole call, on top of the per-request timeout.
FETCH_STATUS=0
if command -v timeout >/dev/null 2>&1; then
  timeout 300 ./wizard.py fetch-versions || FETCH_STATUS=$?
else
  ./wizard.py fetch-versions || FETCH_STATUS=$?
fi
if [ "$FETCH_STATUS" -ne 0 ]; then
  echo "Image versions not updated; compose.yaml and the running stack were left untouched." >&2
  exit "$FETCH_STATUS"
fi

# Render into a candidate file: compose.yaml is only replaced once the new
# images have been pulled successfully.
CANDIDATE="compose.yaml.new"
trap 'rm -f "$CANDIDATE"' EXIT
./wizard.py generate --output "$CANDIDATE"

if [ -f compose.yaml ] && cmp -s "$CANDIDATE" compose.yaml; then
  echo "No image updates detected for environment '$DOME_ENVIRONMENT'; nothing to do."
  exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  mv "$CANDIDATE" compose.yaml
  echo "Dry run enabled; compose.yaml regenerated, skipping docker compose pull/up."
  log_action "update.sh) regenerated compose.yaml for '$DOME_ENVIRONMENT' (dry run)"
  exit 0
fi

# "<service> <image>" for every active service of a compose file, sorted.
service_images() {
  docker compose -f "$1" config --format json | python3 -c '
import json, sys
for name, service in json.load(sys.stdin).get("services", {}).items():
    print(name, service.get("image", ""))
' | sort
}

docker compose -f "$CANDIDATE" config -q

# Pull only the services whose image changed (or that are new): an image that is
# unchanged but no longer downloadable (e.g. a discontinued infrastructure image)
# must not block the updates of the others. Pull everything if the current
# compose.yaml can't be read.
PULL_ALL=1
if [ -f compose.yaml ] && CURRENT_IMAGES="$(service_images compose.yaml)"; then
  PULL_ALL=0
  CANDIDATE_IMAGES="$(service_images "$CANDIDATE")"
  CHANGED_SERVICES=()
  while read -r service _; do
    [ -n "$service" ] && CHANGED_SERVICES+=("$service")
  done < <(comm -13 <(echo "$CURRENT_IMAGES") <(echo "$CANDIDATE_IMAGES"))
fi

if [ "$PULL_ALL" = "1" ]; then
  echo "Image definitions changed; pulling all images and updating the stack..."
  docker compose -f "$CANDIDATE" pull
elif [ "${#CHANGED_SERVICES[@]}" -gt 0 ]; then
  echo "Image definitions changed for: ${CHANGED_SERVICES[*]}; pulling them and updating the stack..."
  docker compose -f "$CANDIDATE" pull "${CHANGED_SERVICES[@]}"
else
  echo "compose.yaml changed without image changes; updating the stack..."
fi
mv "$CANDIDATE" compose.yaml
docker compose -f compose.yaml up -d --remove-orphans
log_action "update.sh) updated versions and restarted the stack (environment $DOME_ENVIRONMENT)"
