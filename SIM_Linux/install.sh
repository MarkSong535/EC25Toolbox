#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run this installer as root." >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=/opt/ec25toolbox
CONFIG_DIR=/etc/ec25toolbox
STATE_DIR=/var/lib/ec25toolbox
SERVICE_FILE=/etc/systemd/system/ec25toolbox-linux.service
VOICE_SERVICE_FILE=/etc/systemd/system/ec25toolbox-voice.service
ACME_SERVICE_FILE=/etc/systemd/system/ec25toolbox-acme-renew.service
ACME_TIMER_FILE=/etc/systemd/system/ec25toolbox-acme-renew.timer
UDEV_RULE=/etc/udev/rules.d/99-ec25toolbox.rules

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "Python 3.11 or later is required." >&2
    exit 1
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    printf '%s\n' "Python 3.11 or later is required." >&2
    exit 1
}

if ! getent group dialout >/dev/null 2>&1; then
    groupadd --system dialout
fi
if ! getent group ec25toolbox >/dev/null 2>&1; then
    groupadd --system ec25toolbox
fi
if ! id ec25toolbox >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin \
        --gid ec25toolbox --groups dialout ec25toolbox
fi
usermod -a -G dialout ec25toolbox
if getent group audio >/dev/null 2>&1; then
    usermod -a -G audio ec25toolbox
fi

install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -o ec25toolbox -g ec25toolbox -m 0750 "$STATE_DIR"
install -d -o ec25toolbox -g ec25toolbox -m 0750 \
    "$STATE_DIR/asterisk" "$STATE_DIR/asterisk/db" "$STATE_DIR/asterisk/keys" \
    "$STATE_DIR/asterisk/log" "$STATE_DIR/asterisk/run" "$STATE_DIR/asterisk/spool"
rm -rf "$INSTALL_DIR/ec25toolbox_linux"
cp -R "$SCRIPT_DIR/ec25toolbox_linux" "$INSTALL_DIR/ec25toolbox_linux"
install -m 0755 "$SCRIPT_DIR/ec25toolbox-linux" "$INSTALL_DIR/ec25toolbox-linux"
install -m 0755 "$SCRIPT_DIR/ec25toolbox-acme" "$INSTALL_DIR/ec25toolbox-acme"
install -d -m 0755 "$INSTALL_DIR/agi-bin"
install -m 0755 "$SCRIPT_DIR/ec25toolbox-agi" "$INSTALL_DIR/agi-bin/ec25toolbox-agi"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    install -o root -g ec25toolbox -m 0640 "$SCRIPT_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
    printf '%s\n' "Created $CONFIG_DIR/config.toml; configure SMTP before starting the service."
else
    printf '%s\n' "Preserved existing $CONFIG_DIR/config.toml."
fi
chown root:ec25toolbox "$CONFIG_DIR/config.toml"
chmod 0640 "$CONFIG_DIR/config.toml"

install -m 0644 "$SCRIPT_DIR/ec25toolbox-linux.service" "$SERVICE_FILE"
install -m 0644 "$SCRIPT_DIR/ec25toolbox-voice.service" "$VOICE_SERVICE_FILE"
install -m 0644 "$SCRIPT_DIR/ec25toolbox-acme-renew.service" "$ACME_SERVICE_FILE"
install -m 0644 "$SCRIPT_DIR/ec25toolbox-acme-renew.timer" "$ACME_TIMER_FILE"
install -m 0644 "$SCRIPT_DIR/ec25toolbox-archive.service" /etc/systemd/system/ec25toolbox-archive.service
install -m 0644 "$SCRIPT_DIR/ec25toolbox-archive.timer" /etc/systemd/system/ec25toolbox-archive.timer
install -m 0644 "$SCRIPT_DIR/99-ec25toolbox.rules" "$UDEV_RULE"
systemctl daemon-reload
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
fi

printf '%s\n' "Installed EC25 Toolbox Linux."
printf '%s\n' "Validate: $INSTALL_DIR/ec25toolbox-linux --config $CONFIG_DIR/config.toml --check-config"
printf '%s\n' "Start:    systemctl enable --now ec25toolbox-linux"
printf '%s\n' "Voice:    systemctl enable --now ec25toolbox-voice"
printf '%s\n' "ACME:     ./install-acme-cloudflare.sh (after configuring [acme] and the API token)"
