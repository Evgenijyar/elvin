#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${ELVIN_APP_DIR:-/opt/lead-voice/app}"
CONFIG_DIR="${ELVIN_CONFIG_DIR:-/opt/lead-voice/config}"
DATA_DIR="${ELVIN_DATA_DIR_HOST:-/opt/lead-voice/data}"
LOG_DIR="${ELVIN_LOG_DIR_HOST:-/opt/lead-voice/logs}"
RECORDINGS_DIR="${ELVIN_RECORDINGS_DIR_HOST:-/opt/lead-voice/recordings}"
ASTERISK_CONFIG_DIR="${ELVIN_ASTERISK_CONFIG_DIR:-/etc/asterisk}"
BRANCH="${ELVIN_GIT_BRANCH:-main}"
NAME="${ELVIN_CONTAINER_NAME:-elvin-backend}"
CANDIDATE="${NAME}-candidate"
BACKUP="${NAME}-previous"

log() { printf '[elvin-deploy] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

wait_ready() {
  local url="$1"
  local attempts="${2:-90}"
  local i
  for i in $(seq 1 "$attempts"); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

usage() {
  cat <<USAGE
Usage:
  elvin-deploy                 Deploy origin/${BRANCH}
  elvin-deploy logs [LINES]    Follow backend logs (default: 200 lines)
  elvin-deploy status          Show Git, container and health status
  elvin-deploy health          Check health and readiness
  elvin-deploy asterisk-logs   Follow /var/log/asterisk/full
USAGE
}

command="${1:-deploy}"
case "$command" in
  logs)
    lines="${2:-200}"
    docker inspect "$NAME" >/dev/null 2>&1 || die "container ${NAME} does not exist"
    exec docker logs --follow --timestamps --tail "$lines" "$NAME"
    ;;
  asterisk-logs)
    [[ -f /var/log/asterisk/full ]] || die "/var/log/asterisk/full does not exist"
    exec tail -n "${2:-200}" -F /var/log/asterisk/full
    ;;
  health)
    curl -fsS http://127.0.0.1:8000/api/health && printf '\n'
    curl -fsS http://127.0.0.1:8000/api/readiness && printf '\n'
    exit 0
    ;;
  status)
    if [[ -d "$APP_DIR/.git" ]]; then
      printf 'Git: '
      git -C "$APP_DIR" log -1 --oneline || true
    fi
    docker ps -a --filter "name=^/${NAME}$" \
      --format 'Container: {{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}'
    curl -fsS http://127.0.0.1:8000/api/health 2>/dev/null || true
    printf '\n'
    exit 0
    ;;
  deploy) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage; die "unknown command: ${command}" ;;
esac

[[ $EUID -eq 0 ]] || die "run deployment as root"
[[ -d "$APP_DIR/.git" ]] || die "Git repository not found: ${APP_DIR}"

# Keep the global command permanently attached to the current repository script.
chmod +x "$APP_DIR/deploy/server/elvin-deploy.sh" 2>/dev/null || true
ln -sfn "$APP_DIR/deploy/server/elvin-deploy.sh" /usr/local/bin/elvin-deploy
ln -sfn "$APP_DIR/deploy/server/elvin-deploy.sh" /usr/local/sbin/elvin-deploy

cd "$APP_DIR"
log "Fetching origin/${BRANCH}..."
git fetch --prune origin "$BRANCH"
git reset --hard "origin/${BRANCH}"

# The fetched revision can contain a newer copy of this file; refresh the command link.
chmod +x deploy/server/elvin-deploy.sh
ln -sfn "$APP_DIR/deploy/server/elvin-deploy.sh" /usr/local/bin/elvin-deploy
ln -sfn "$APP_DIR/deploy/server/elvin-deploy.sh" /usr/local/sbin/elvin-deploy

# Disable unbounded Asterisk/syslog and call-artifact retention before starting
# the new backend. This is idempotent and also cleans files accumulated earlier.
chmod +x deploy/server/elvin-disable-persistence.sh
deploy/server/elvin-disable-persistence.sh

# Asterisk AMI intentionally listens on host loopback only. Expose it solely
# on the Docker bridge through a systemd socket proxy, never on a public
# interface. The backend uses this real-time control path for the optional
# interruption-tail voice fade, explicit robot-requested hangup, and the
# isolated direct-SIP telephony test.
install -m 0644 deploy/server/elvin-ami-proxy.socket \
  /etc/systemd/system/elvin-ami-proxy.socket
install -m 0644 deploy/server/elvin-ami-proxy.service \
  /etc/systemd/system/elvin-ami-proxy.service
systemctl daemon-reload
systemctl enable --now elvin-ami-proxy.socket >/dev/null
systemctl is-active --quiet elvin-ami-proxy.socket \
  || die "Asterisk AMI proxy socket failed to start"

required=(
  pyproject.toml uv.lock media-requirements.txt Dockerfile.deps Dockerfile
  deploy/server/elvin-ami-proxy.socket
  deploy/server/elvin-ami-proxy.service
  deploy/server/asterisk-logger.conf
  deploy/server/elvin-asterisk-no-output.conf
  deploy/server/elvin-rsyslog-drop-asterisk.conf
  deploy/server/elvin-disable-persistence.sh
  deploy/server/elvin-telephony-test.conf
)
for file in "${required[@]}"; do
  [[ -f "$file" ]] || die "required file is missing: ${file}"
done

# Install one isolated direct-SIP playback context.  The production
# from-lptracker -> elvin-ai -> chan_websocket route is left untouched.
install -d -m 0755 "$ASTERISK_CONFIG_DIR/extensions.d"
install -m 0644 deploy/server/elvin-telephony-test.conf \
  "$ASTERISK_CONFIG_DIR/extensions.d/elvin-telephony-test.conf"
extensions_file="$ASTERISK_CONFIG_DIR/extensions.conf"
include_line='#include extensions.d/elvin-telephony-test.conf'
grep -Fqx "$include_line" "$extensions_file" \
  || printf '\n%s\n' "$include_line" >> "$extensions_file"
asterisk -rx "dialplan reload" >/dev/null
dialplan_loaded=false
for _attempt in $(seq 1 10); do
  dialplan_output="$(asterisk -rx "dialplan show elvin-telephony-test")"
  if grep -Fq "Elvin isolated telephony test" <<<"$dialplan_output"; then
    dialplan_loaded=true
    break
  fi
  sleep 0.2
done
[[ "$dialplan_loaded" == true ]] \
  || die "isolated telephony-test dialplan was not loaded"

if grep -Eqi 'applied-caas|internal\.api\.openai' uv.lock; then
  die "uv.lock contains an internal package registry"
fi

REVISION="$(git rev-parse --short=12 HEAD)"
DEPS_HASH="$(sha256sum pyproject.toml uv.lock media-requirements.txt Dockerfile.deps | sha256sum | cut -c1-16)"
DEPS_IMAGE="elvin-backend-deps:${DEPS_HASH}"
APP_IMAGE="elvin-backend:${REVISION}"

if ! docker image inspect "$DEPS_IMAGE" >/dev/null 2>&1; then
  log "Building dependency image ${DEPS_IMAGE}..."
  DOCKER_BUILDKIT=0 docker build --pull -f Dockerfile.deps -t "$DEPS_IMAGE" .
else
  log "Reusing dependency image ${DEPS_IMAGE}."
fi

log "Building application image ${APP_IMAGE}..."
DOCKER_BUILDKIT=0 docker build \
  --build-arg "ELVIN_DEPS_IMAGE=${DEPS_IMAGE}" \
  --label "org.opencontainers.image.revision=${REVISION}" \
  -t "$APP_IMAGE" .

ENV_ARGS=()
for file in database.env application.env asterisk-secrets.env; do
  [[ -f "$CONFIG_DIR/$file" ]] && ENV_ARGS+=(--env-file "$CONFIG_DIR/$file")
done

mkdir -p "$DATA_DIR" "$LOG_DIR" "$RECORDINGS_DIR"
chown -R 994:986 "$DATA_DIR" "$LOG_DIR" "$RECORDINGS_DIR" 2>/dev/null || true

BASE_ARGS=(
  --log-driver local
  --log-opt max-size=50m
  --log-opt max-file=5
  --add-host host.docker.internal:host-gateway
  -e ELVIN_BIND_HOST=0.0.0.0
  -e ELVIN_BIND_PORT=8000
  -e ASTERISK_AMI_HOST=host.docker.internal
  -e ASTERISK_AMI_PORT=5039
  # Production must not retain call audio or per-call diagnostic artifacts.
  -e ELVIN_RECORDINGS_ENABLED=false
  -e ELVIN_FRAME_TRACE_ENABLED=false
  -v "$DATA_DIR:/opt/lead-voice/data"
  -v "$LOG_DIR:/opt/lead-voice/logs"
  -v "$RECORDINGS_DIR:/opt/lead-voice/recordings"
)

# Verify the image without touching the working production container.
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
log "Starting isolated candidate on 127.0.0.1:18000..."
docker run -d --name "$CANDIDATE" \
  -p 127.0.0.1:18000:8000 \
  "${ENV_ARGS[@]}" "${BASE_ARGS[@]}" "$APP_IMAGE" >/dev/null

if ! wait_ready http://127.0.0.1:18000/api/readiness 90; then
  log "Candidate failed readiness. Last logs:"
  docker logs --tail 250 "$CANDIDATE" || true
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  die "production container was not changed"
fi
log "Candidate is ready."
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true

# Preserve the previous production container until the new one passes readiness.
docker rm -f "$BACKUP" >/dev/null 2>&1 || true
had_previous=false
if docker inspect "$NAME" >/dev/null 2>&1; then
  had_previous=true
  log "Stopping previous production container..."
  docker stop --time 20 "$NAME" >/dev/null
  docker rename "$NAME" "$BACKUP"
fi

rollback() {
  log "New production container failed. Restoring previous container..."
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  if [[ "$had_previous" == true ]] && docker inspect "$BACKUP" >/dev/null 2>&1; then
    docker rename "$BACKUP" "$NAME"
    docker start "$NAME" >/dev/null
  fi
}

log "Starting production container ${NAME}..."
if ! docker run -d --name "$NAME" \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  "${ENV_ARGS[@]}" "${BASE_ARGS[@]}" "$APP_IMAGE" >/dev/null; then
  rollback
  die "docker run failed"
fi

if ! wait_ready http://127.0.0.1:8000/api/readiness 90; then
  log "New production container failed readiness. Last logs:"
  docker logs --tail 250 "$NAME" || true
  rollback
  die "rollback completed"
fi

if [[ "$had_previous" == true ]]; then
  docker rm -f "$BACKUP" >/dev/null 2>&1 || true
fi

health="$(curl -fsS http://127.0.0.1:8000/api/health)"
readiness="$(curl -fsS http://127.0.0.1:8000/api/readiness)"
log "Health: ${health}"
log "Readiness: ${readiness}"
log "Deployment completed. Revision: ${REVISION}"

docker image prune -f >/dev/null 2>&1 || true
