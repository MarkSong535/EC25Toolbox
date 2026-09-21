#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run this installer as root." >&2
    exit 1
fi

ACME_COMMAND=/opt/ec25toolbox/ec25toolbox-acme
CREDENTIALS=/etc/ec25toolbox/cloudflare.ini

if [ ! -x "$ACME_COMMAND" ]; then
    printf '%s\n' "Run ./install.sh before installing ACME support." >&2
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y certbot python3-certbot-dns-cloudflare
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y certbot python3-certbot-dns-cloudflare
else
    printf '%s\n' "Unsupported package manager. Install Certbot and its Cloudflare DNS plugin first." >&2
    exit 1
fi

if [ ! -f "$CREDENTIALS" ]; then
    install -o root -g root -m 0600 /dev/null "$CREDENTIALS"
    printf '%s\n' "Created $CREDENTIALS. Add this line with a zone-scoped token:" >&2
    printf '%s\n' "dns_cloudflare_api_token = YOUR_TOKEN" >&2
    printf '%s\n' "Then rerun this installer." >&2
    exit 2
fi

chown root:root "$CREDENTIALS"
chmod 0600 "$CREDENTIALS"
"$ACME_COMMAND" issue
if systemctl cat certbot.timer >/dev/null 2>&1; then
    systemctl enable --now certbot.timer
    renewal_timer=certbot.timer
else
    systemctl enable --now ec25toolbox-acme-renew.timer
    renewal_timer=ec25toolbox-acme-renew.timer
fi
printf '%s\n' "Issued the verified SIP certificate and enabled automatic DNS-01 renewal."
printf '%s\n' "Renewal timer: $renewal_timer"
