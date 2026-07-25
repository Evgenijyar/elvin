#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[elvin-storage] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -f deploy/server/asterisk-logger.conf ]] || die "run from the application repository"

install -d -m 0755 /etc/asterisk /etc/systemd/system/asterisk.service.d /etc/rsyslog.d \
  /etc/systemd/journald.conf.d

if [[ ! -e /etc/asterisk/logger.conf.elvin-before-disable ]]; then
  cp -p /etc/asterisk/logger.conf /etc/asterisk/logger.conf.elvin-before-disable
fi
install -m 0640 -o asterisk -g asterisk \
  deploy/server/asterisk-logger.conf /etc/asterisk/logger.conf
install -m 0644 deploy/server/elvin-asterisk-no-output.conf \
  /etc/systemd/system/asterisk.service.d/10-elvin-no-output.conf
install -m 0644 deploy/server/elvin-rsyslog-drop-asterisk.conf \
  /etc/rsyslog.d/10-elvin-drop-asterisk.conf

cat >/etc/systemd/journald.conf.d/99-elvin-retention.conf <<'EOF'
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=50M
MaxRetentionSec=7day
EOF

if command -v rsyslogd >/dev/null 2>&1 && ! rsyslogd -N1 >/dev/null 2>&1; then
  rm -f /etc/rsyslog.d/10-elvin-drop-asterisk.conf
  die "rsyslog configuration validation failed"
fi

systemctl daemon-reload
systemctl restart systemd-journald
systemctl restart rsyslog
systemctl restart asterisk
systemctl is-active --quiet asterisk || die "Asterisk failed to restart"

# Remove accumulated call artifacts and only the known oversized log files.
find /opt/lead-voice/recordings -mindepth 1 -delete 2>/dev/null || true
find /opt/lead-voice/data/outbound_diagnostics -mindepth 1 -delete 2>/dev/null || true
find /var/log/asterisk -type f -exec truncate -s 0 {} \; 2>/dev/null || true
for file in /var/log/syslog /var/log/syslog.1; do
  [[ -f "$file" ]] && truncate -s 0 "$file"
done
rm -f /var/log/syslog.2.gz

journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-size=100M >/dev/null 2>&1 || true

asterisk -rx 'logger show channels' 2>/dev/null || true
df -h /
log "Persistent Asterisk logging and call-artifact recording are disabled."
