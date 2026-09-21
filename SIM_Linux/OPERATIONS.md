# Operations and implementation map

Paths below refer to the Pi unless stated otherwise. Run installation commands
from the checkout's `SIM_Linux` directory. Application settings belong only in
`/etc/ec25toolbox/config.toml`; generated Asterisk files are not an edit target.

## Source map

| Source | Responsibility |
| --- | --- |
| `ec25toolbox-linux` / `ec25toolbox_linux/daemon.py` | CLI, lifecycle, serial ownership, SMS/call processing, portal startup |
| `config.py`, `portal_config.py`, `archive_config.py` | Central TOML parsing, constraints, mode compatibility |
| `transport.py` | Serial locking, AT transactions, prompt detection, PDU submission |
| `sms.py`, `outbound.py` | Incoming parsing/dedup IDs; outgoing UCS-2 PDU validation/encoding |
| `storage.py`, `mailer.py` | Durable event queue, retry state, SMTP/TLS and exact SMS email format |
| `portal.py` | Flask/Waitress, authentication, CSRF, API routes, headers, session audit |
| `portal_store.py` | Message queries, immutable tables/triggers, rate/idempotency checks |
| `portal_static/index.html` | Accessible UI structure and supplied SVG icons |
| `portal_static/app.js` | Plain-text rendering, API calls, compose, drawer, archive/access-log views |
| `portal_static/style.css` | Base styling |
| `portal_static/layout.css` | Viewport panes, vertical list, mobile drawer and menu/header alignment |
| `portal_static/theme.css` | marksong.tech-derived light/dark colors and final control styling |
| `archive.py` | SQLite snapshot, EML/JSONL/tar.gz export, upload/hash verification, recovery |
| `voice.py`, `ec25toolbox-agi` | Optional Asterisk config generation and queued call/SMS events |
| `acme.py`, `ec25toolbox-acme` | Optional voice certificate DNS-01 issuance/renewal |
| `install.sh`, `install-portal.sh` | Base installation and pinned portal virtual environment |
| `install-voice.sh`, `patches/` | Optional pinned Asterisk/channel-driver build and SMS safety patch |
| `install-acme-cloudflare.sh` | Optional certbot/plugin setup and token-file wiring |
| `*.service`, `*.timer`, `portal-runtime.conf` | systemd service wiring and archive/ACME wakeups |
| `99-ec25toolbox.rules` | Targeted modem handling; no firmware/USB identity rewrite |
| `tests/` | Unit, emulated serial, archive round-trip and browser regressions |

## Central settings and scheduling

| TOML table | Controls | Apply changes |
| --- | --- | --- |
| `[service]` | DB location, logging, reconnect/reconcile timing | Restart gateway; move DB only deliberately |
| `[modem]` | AT path, baud, exclusive ownership, optional existing-ID binding | Restart gateway |
| `[sms]` | Storage polling, first-run baseline, deletion after forwarding | Restart gateway |
| `[calls]` | Reject/observe/bridge, delay, email notifications | Restart gateway |
| `[smtp]` | Token/address/recipients, TLS, retries | Restart gateway |
| `[portal]` | Origin, port, auth mode, JWT identity data, send limits | Restart gateway; coordinate Tunnel if hostname/port changes |
| `[archive]` | Enable, remote folder, binary, interval and size cap | Next archive service invocation reads it |
| `[voice]` | Optional SIP/TLS/SRTP/audio, call routing | Revalidate; restart relevant gateway/voice services |
| `[acme]` | Optional DNS-01 contact/token path/propagation | Next ACME invocation reads it |

The archive timer wakes five minutes after boot and hourly thereafter. The
application skips until `interval_hours` since the last verified receipt has
elapsed (allowed 1–168 hours). This is an interval, not an exact wall-clock time.
There is no catch-up burst of missed backups. `--force` bypasses due-time checking,
not `enabled=false`. The archive size cap accepts 1–1024 MB; defaults to 64 MB.

```sh
sudo systemctl is-enabled ec25toolbox-archive.timer
sudo systemctl list-timers ec25toolbox-archive.timer
sudo journalctl -u ec25toolbox-archive -n 30 --no-pager
```

## Installed data and secrets

| Path | Contents / handling |
| --- | --- |
| `/opt/ec25toolbox` | Installed code and portal venv; replace from source when updating |
| `/etc/ec25toolbox/config.toml` | Real settings and SMTP/SIP secrets; root:ec25toolbox, mode 0640 |
| `/var/lib/ec25toolbox/events.sqlite3` | Private messages, delivery state, outbox, audits and receipts |
| `/var/lib/ec25toolbox/.password-store` | GPG-encrypted Proton session; never display `pass show` output publicly |
| `/var/lib/ec25toolbox/.gnupg` | Service account key material; protect/backup outside this repository |
| `/var/lib/ec25toolbox/.cache`, `.local` | Possible Proton CLI cache/app data/logs; not the cloud archive mirror |
| `/etc/ec25toolbox/cloudflare.ini` | Optional root-only DNS API token for voice ACME |
| `/etc/ec25toolbox/tls` | Optional voice certificate/key; never commit private keys |
| `/run/ec25toolbox-asterisk` | Generated optional voice config, ephemeral and regenerated |
| `/var/backups/ec25toolbox-*` | Operator rollback backups; potentially contain secrets, protect accordingly |

System journal logs are not exported wholesale. Archives cover the gateway's
database audit/access records and reconstructed emails, not all OS logs or all
Proton Mail. The current archive receipt is inserted after its snapshot, so it
will appear only in a later snapshot. Keep recovery checksums separately too.

## Backup before updates

Do not plain-copy a running WAL-mode SQLite database. On the Pi, this creates a
protected, consistent database backup plus config/code backup:

```sh
sudo sh -c '
set -eu
backup_dir=$(mktemp -d /var/backups/ec25toolbox-update-XXXXXXXX)
chmod 0700 "$backup_dir"
cp -a /etc/ec25toolbox/config.toml "$backup_dir/config.toml"
cp -a /opt/ec25toolbox/ec25toolbox_linux "$backup_dir/code"
sqlite3 /var/lib/ec25toolbox/events.sqlite3 ".backup $backup_dir/events.sqlite3"
printf "Backup saved: %s\n" "$backup_dir"
'
```

Use the configured DB path if different. Back up credentials separately through
a protected procedure; a Drive archive intentionally does not include them.

## Update and rollback

Review local changes first; `git pull --ff-only` should stop on divergence.
Pause the archive timer and stop the gateway before replacing code, after
checking there is no active send. This avoids partially updated assets/modules.

```sh
git status --short
git pull --ff-only
# From SIM_Linux:
sudo systemctl stop ec25toolbox-archive.timer
sudo systemctl stop ec25toolbox-archive.service
sudo systemctl stop ec25toolbox-linux
sudo sh ./install-portal.sh
sudo /opt/ec25toolbox/venv/bin/python /opt/ec25toolbox/ec25toolbox-linux --check-config
sudo systemctl start ec25toolbox-linux
# Restart this timer only if it was previously enabled and credentials still work:
sudo systemctl start ec25toolbox-archive.timer
```

Stopping an active archive may interrupt verification; live data is retained,
and an unverified cloud copy can remain. The next run uses a new unique name.
Installers preserve config, but do not merge newly added example keys into it.
For rollback, reinstall a reviewed previous code version and its dependencies,
retain current database/config, then restart. Do not restore an old database for
a code-only rollback: newer messages and send deduplication state would be lost.

## Diagnostics without leaking secrets

| Symptom | Check / interpretation |
| --- | --- |
| Cloudflare 502 | Check gateway status and `curl -I http://127.0.0.1:9561/`; verify Tunnel service uses HTTP, correct port, same-host loopback |
| HTTP 401 | Expected for missing/invalid JWT in strict mode; verify team/AUD/allowlist and Access login, not weaker auth |
| HTTP 403 while sending | Origin or CSRF mismatch; reload the genuine HTTPS hostname, not file:// or a copied page |
| Red dot | Gateway modem initialization/connection is unavailable; inspect serial path, ownership, power and logs |
| Email queued/retrying | Check SMTP token/address match, STARTTLS 587, outbound network, logs; never paste the token |
| No older SMS email | First-run baseline may intentionally suppress old mail; `processed` alone is not proof of SMTP acceptance |
| Send `unknown` | Modem response uncertain or interrupted; verify recipient before any manual resend |
| Proton operation failed | Same Unix account/cwd, installed CLI, pass entry, GPG unlock, valid login and existing folder |
| Archive not due | Successful previous receipt still within configured interval |
| Oversize/space failure | Check DB and free space; no auto-pruning occurs. Raise cap only with enough temporary space |
| No Asterisk config/control socket | Voice not started or unsupported setup; irrelevant in SMS/reject mode |
| Asterisk download 404 | Retain pinned old-releases URL in installer, not a moving latest-release URL |
| PJSIP build `Error 2` | Generic wrapper; inspect first actual compiler error. Installer retries serially; use `sudo env EC25_BUILD_JOBS=1 sh ./install-voice.sh` for diagnosis |

For manual Proton troubleshooting use the service account and
`cd /var/lib/ec25toolbox`, not an inaccessible admin home directory. Do not run
live AT diagnostics while the gateway owns the port.

## Optional FRP/voice boundary

The portal does not need FRP. Do not publish port 9561 outside its Access-protected
Tunnel. The optional Asterisk TLS listener does not consume PROXY protocol;
do not set `transport.proxyProtocolVersion="v2"` in front of it. A TCP-only SIP
proxy does not carry RTP/SRTP UDP audio. A working relay requires both signaling
and media routing/NAT configuration and a real end-to-end call test. No FRP
deployment is configured by these installers. Use [VOICE_DEPLOYMENT.md](VOICE_DEPLOYMENT.md)
for the direct-network alternative and its hardware limitations.
