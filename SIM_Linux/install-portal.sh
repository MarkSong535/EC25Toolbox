#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run this installer as root.' >&2
    exit 1
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m venv /opt/ec25toolbox/venv
/opt/ec25toolbox/venv/bin/pip install -r "$SCRIPT_DIR/requirements-portal.txt"
# Main installer preserves central config and includes the portal assets.
sh "$SCRIPT_DIR/install.sh"
install -d -m 0755 /etc/systemd/system/ec25toolbox-linux.service.d
install -m 0644 "$SCRIPT_DIR/portal-runtime.conf" /etc/systemd/system/ec25toolbox-linux.service.d/portal-runtime.conf
systemctl daemon-reload
printf '%s\n' 'Portal installed, not enabled. Configure [portal] in /etc/ec25toolbox/config.toml.'
printf '%s\n' 'Validate with /opt/ec25toolbox/venv/bin/python /opt/ec25toolbox/ec25toolbox-linux --check-config'
printf '%s\n' 'Then restart ec25toolbox-linux. See PORTAL.md for Access and Tunnel setup.'
