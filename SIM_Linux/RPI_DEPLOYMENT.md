# Raspberry Pi: SMS, email, call rejection and portal

Primary setup: no Asterisk, SIP, ACME certificate on the Pi, public Pi IP or
inbound router port is needed. The portal listens on **127.0.0.1:9561 HTTP**;
Cloudflare Access and Tunnel provide browser HTTPS/authentication. Optional
call bridging is separate: [VOICE_DEPLOYMENT.md](VOICE_DEPLOYMENT.md).

## 1. Prepare and obtain the code

Use 64-bit Raspberry Pi OS/Debian with Python 3.11+, systemd, reliable USB power,
an active SMS SIM, a portal domain, and Proton SMTP credentials. The archive
binary instructions assume ARM64 (`aarch64`). Do not run another serial terminal
or modem manager against the gateway's AT port.

```sh
sudo apt update
sudo apt install -y python3 python3-venv git ca-certificates curl gnupg pass sqlite3
uname -m
python3 --version
git clone git@github.com:MarkSong535/EC25Toolbox.git
cd EC25Toolbox/SIM_Linux
ls -l /dev/serial/by-id/
```

Clone after these changes are pushed. SSH cloning needs an authorized GitHub
key; HTTPS is an alternative for a public repository. For unpublished code,
copy from the development checkout with `rsync -av SIM_Linux/ rpi:~/SIM_Linux/`,
then work in `~/SIM_Linux`. Do not copy production secrets into the checkout.

AT path: `/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0`. The if01 audio path
is unused here. If serial paths are absent, check `lsusb`, power and kernel logs.
Only then consider binding the existing ID via `modem.option_driver_ids`; this
changes the Linux driver association, never the dongle identity.

## 2. Install and configure

```sh
sudo sh ./install-portal.sh
sudoedit /etc/ec25toolbox/config.toml
```

The installer creates the service account, copies code/assets to `/opt/ec25toolbox`,
installs units and creates a config only if absent. It preserves existing config
and does not restart the gateway. Use [config.portal.example.toml](config.portal.example.toml)
as the minimal reference; edit existing tables rather than appending duplicates.

Essential settings:

- `modem.port`: the persistent AT path above, or `auto` for probing.
- `voice.enabled=false`, `acme.enabled=false`, `calls.mode="reject"`,
  `calls.reject=true`, `calls.reject_delay_ms=0`.
- SMTP: `smtp.protonmail.ch`, port `587`, `security="starttls"`. Use a dedicated
  SMTP token, not an account password. `username` and `from_address` must match
  its authorized address. This installation uses `noreply@anonymunication.ch`
  with `from_name="RPI SMS"`; choose your own recipient list.
- Portal: enabled, `origin="https://phone.markso.ng"`, port `9561`.
  Choose `auth_mode="tunnel"` only with step 3's Access boundary. Independent
  JWT mode is documented in [PORTAL.md](PORTAL.md).
- Leave archives disabled until service-account authentication is verified.

`forward_existing_on_first_start=false` baselines old modem SMS without emailing
it. `delete_after_forward=true` removes successfully forwarded modem copies,
not database history. Keep SMTP enabled for this workflow.

```sh
sudo chown root:ec25toolbox /etc/ec25toolbox/config.toml
sudo chmod 0640 /etc/ec25toolbox/config.toml
sudo /opt/ec25toolbox/venv/bin/python /opt/ec25toolbox/ec25toolbox-linux --check-config
```

## 3. Protect and publish the portal

Reuse the same-Pi `cloudflared` connector and preserve other routes. For a new
connector follow [Cloudflare's official setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/).
Keep its token outside Git and shared logs.

Create an Access self-hosted application for the **whole** `phone.markso.ng`
hostname with only intended identities allowed. No Bypass/Everyone policy.
Publish the hostname to **http://127.0.0.1:9561**, not HTTPS. A container's
loopback is not the Pi's unless using host networking. Do not expose port 9561
publicly or through FRP to solve connector reachability.

Tunnel routing and Access authorization are separate. `auth_mode="tunnel"`
trusts this boundary and does not independently verify JWTs. [PORTAL.md](PORTAL.md)
documents the stronger local JWT option and security checks.

## 4. Start and verify

Stop/disable an old `ec25toolbox-voice.service` if it owns this modem. Do not
stop unrelated telephony services. Then:

```sh
sudo systemctl enable --now ec25toolbox-linux
# Also restart if it was already active before the config/code change:
sudo systemctl restart ec25toolbox-linux
sudo systemctl status ec25toolbox-linux --no-pager
sudo journalctl -u ec25toolbox-linux -n 50 --no-pager
ss -ltn 'sport = :9561'
curl --fail http://127.0.0.1:9561/api/session
```

Local curl succeeds in tunnel mode. JWT mode should reject tokenless requests
with 401. Do not share response tokens or private log content.

Acceptance checks:

1. Unauthenticated browser is redirected to Access; disallowed identity is blocked;
   allowed identity sees the inbox. LAN/public origin port is unreachable.
2. SMS from a controlled phone appears in the inbox and email, with sender-number
   subject and original-text body.
3. Incoming call is rejected and its email event queued.
4. One portal SMS reaches a controlled recipient (charges may apply). Check the
   recipient before retrying any `unknown` outcome.
5. Mobile menu opens a vertical conversation list with New message and Archives.
   Heading sits beside the menu; Send has a plane icon and no helper text.
   The SIM dot opens first-observed-session logs, not Cloudflare auth history.
6. Restart once and confirm history remains. Avoid restarting during a send.

Opening `index.html` through `file://` is not a functional preview: root-relative
assets, APIs and origin checks require the running application.

## 5. Archives and maintenance

Follow [ARCHIVES.md](ARCHIVES.md) for CLI installation, GPG/pass setup, Proton
login, upload/download verification and timer activation. Use [OPERATIONS.md](OPERATIONS.md)
for backups, updates, rollback and failure diagnosis. Never overwrite live
history as part of an ordinary code update.
