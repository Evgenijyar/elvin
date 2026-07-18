#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${ELVIN_APP_DIR:-/opt/lead-voice/app}"
CONFIG_DIR="${ELVIN_CONFIG_DIR:-/opt/lead-voice/config}"
DATA_DIR="${ELVIN_DATA_DIR_HOST:-/opt/lead-voice/data}"
LOG_DIR="${ELVIN_LOG_DIR_HOST:-/opt/lead-voice/logs}"
RECORDINGS_DIR="${ELVIN_RECORDINGS_DIR_HOST:-/opt/lead-voice/recordings}"
REPO="${ELVIN_GIT_REPOSITORY:-https://github.com/Evgenijyar/elvin.git}"
BRANCH="${ELVIN_GIT_BRANCH:-main}"
NAME="elvin-backend"

log() { printf '[elvin-deploy] %s\n' "$*"; }
cd "$APP_DIR"
log "Fetching origin/${BRANCH}..."
git fetch origin "$BRANCH"
git reset --hard "origin/${BRANCH}"
REVISION="$(git rev-parse --short=12 HEAD)"
DEPS_HASH="$(sha256sum pyproject.toml uv.lock media-requirements.txt Dockerfile.deps | sha256sum | cut -c1-16)"
DEPS_IMAGE="elvin-backend-deps:${DEPS_HASH}"
APP_IMAGE="elvin-backend:${REVISION}"

if ! docker image inspect "$DEPS_IMAGE" >/dev/null 2>&1; then
  log "Building dependency image ${DEPS_IMAGE}..."
  docker build -f Dockerfile.deps -t "$DEPS_IMAGE" .
else
  log "Reusing dependency image ${DEPS_IMAGE}."
fi

log "Building ${APP_IMAGE}..."
docker build --build-arg "ELVIN_DEPS_IMAGE=${DEPS_IMAGE}" -t "$APP_IMAGE" .

ENV_ARGS=()
for file in database.env application.env asterisk-secrets.env; do
  [[ -f "$CONFIG_DIR/$file" ]] && ENV_ARGS+=(--env-file "$CONFIG_DIR/$file")
done
COMMON=(
  --restart unless-stopped
  --log-driver local
  --log-opt max-size=50m
  --log-opt max-file=5
  -e ELVIN_BIND_HOST=0.0.0.0
  -e ELVIN_BIND_PORT=8000
  -v "$DATA_DIR:/opt/lead-voice/data"
  -v "$LOG_DIR:/opt/lead-voice/logs"
  -v "$RECORDINGS_DIR:/opt/lead-voice/recordings"
)

mkdir -p "$DATA_DIR" "$LOG_DIR" "$RECORDINGS_DIR"
CANDIDATE="${NAME}-candidate"
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
log "Starting isolated candidate on 127.0.0.1:18000..."
docker run -d --name "$CANDIDATE" \
  -p 127.0.0.1:18000:8000 \
  "${ENV_ARGS[@]}" "${COMMON[@]}" "$APP_IMAGE" >/dev/null

ready=false
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:18000/api/readiness >/dev/null; then ready=true; break; fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  docker logs --tail 200 "$CANDIDATE" || true
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  log "ERROR: candidate failed readiness. Production was not changed."
  exit 1
fi
log "Candidate is ready."

docker rm -f "$CANDIDATE" >/dev/null
log "Switching production container..."
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  -p 127.0.0.1:8000:8000 \
  "${ENV_ARGS[@]}" "${COMMON[@]}" "$APP_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8000/api/readiness >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/health
printf '\n'
log "Deployment completed. Revision: ${REVISION}"

docker image prune -f >/dev/null || true
