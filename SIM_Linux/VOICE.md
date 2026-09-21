# Optional cellular voice bridge

This is the optional Asterisk/chan_quectel design, not the default SMS portal deployment. Hardware and carrier compatibility must be established independently. The portal requires `voice.enabled=false`; do not enable both owners. See [README.md](README.md) for the primary setup.

# EC25 Toolbox Linux

For the Google Voice-style SMS inbox, sending, immutable history and Cloudflare
Access/Tunnel deployment on HTTP port 9561, see [PORTAL.md](PORTAL.md).

This directory is a self-contained Linux gateway for a DJI/Quectel EC25 dongle.
It can receive and place ordinary cellular calls through a SIP phone app, receive
SMS through the same modem owner, and forward SMS or call events through SMTP.
It does not change or require a particular USB vendor/product ID.

For a complete start-to-finish Raspberry Pi installation, follow
[`RPI_DEPLOYMENT.md`](RPI_DEPLOYMENT.md). The remainder of this file documents
the design and individual components.

## Architecture

`sn4f/asterisk-chan-quectel` is the sole owner of the EC25 call-control, audio,
and SMS interfaces. A dedicated Asterisk instance routes cellular audio to a
registered SIP app. No SIP trunk or external VoIP provider is used. The Python
service never opens the modem while voice bridging is enabled; it only delivers
durably queued SMTP events produced by the Asterisk AGI hook.

The app-facing connection has no plaintext mode: Asterisk accepts SIP over TLS
on TCP 5061 and requires SDES-SRTP for audio. SIP authentication uses SHA-256
Digest. Linphone must verify a publicly trusted certificate whose subject
alternative name matches `voice.server_name`; do not disable certificate
verification in the app.

The selected driver is pinned to commit
`d6e9dba8319e9f78eed99b56e8fc4440caf9c4be`. The installer applies the included
SMS safety patch, which defers SMS retrieval during an active call and resumes
it after the last call ends. This addresses the upstream driver's documented
SMS-during-call command-queue crash risk.

## Important cellular limitation

The app-side link is local SIP, but the modem side is an ordinary carrier phone
call. The carrier must still provide a voice bearer supported by this exact EC25
variant and firmware: either legacy circuit-switched 2G/3G voice or VoLTE. If a
network has retired 2G/3G and does not authorize this modem for VoLTE, software
cannot create a working call path. One EC25 supports one active cellular call.

Serial audio mode does not itself require VoLTE. It transports whatever voice
call the modem established. This dongle's confirmed persistent paths are
`/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0` for AT/call control and
`/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0` for serial audio. These paths
remain stable without rewriting its USB identity.

## One central configuration file

All editable settings are stored in:

```text
/etc/ec25toolbox/config.toml
```

This includes modem ports, serial/UAC audio mode, SIP credentials and ACLs, TLS
certificate paths, ACME DNS settings, public addressing, RTP ports, call routing,
SMS deletion behavior, SMTP credentials, recipients, retry timing, and database location. Asterisk files under
`/run/ec25toolbox-asterisk` are generated on every start and must not be edited.
They contain no independent configuration source.

When an SMS is forwarded, its email subject is exactly the sender's phone
number and its email body is only the original SMS content. Multipart SMS is
reassembled by `chan_quectel` before the AGI hook queues the email. SMTP failures
remain in SQLite with bounded retry backoff.

The supplied SMTP example uses Proton's `smtp.protonmail.ch` submission service
with certificate-verified STARTTLS on port 587. It requires a dedicated Proton
SMTP token; the regular Proton account password is not accepted.

## Install

On the Linux gateway:

```sh
cd SIM_Linux
sudo ./install-voice.sh
sudo ./install.sh
sudoedit /etc/ec25toolbox/config.toml
```

Set these essential values:

```toml
[calls]
mode = "bridge"

[voice]
enabled = true
data_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0"
audio_mode = "serial"
audio_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0"
sip_username = "ec25phone"
sip_password = "replace-with-at-least-16-random-characters"
server_name = "phone.example.com"
sip_bind = "0.0.0.0:5061"
tls_cert_file = "/etc/ec25toolbox/tls/fullchain.pem"
tls_private_key_file = "/etc/ec25toolbox/tls/privkey.pem"
# Set the public IP if this host is behind NAT; otherwise leave it empty.
external_address = "203.0.113.10"

[acme]
enabled = true
email = "admin@example.com"
credentials_file = "/etc/ec25toolbox/cloudflare.ini"
propagation_seconds = 30
staging = false
```

Use a real DNS name and a certificate issued by a CA Linphone already trusts,
such as Let's Encrypt. An IP address or self-signed certificate does not meet
the verification requirement.

### Automatic certificate with Cloudflare DNS

Use an ACME DNS-01 challenge. The Raspberry Pi only makes outbound HTTPS
requests to Cloudflare and Let's Encrypt; it does **not** need inbound ports 80
or 443. In Cloudflare, create an API token with only `Zone / DNS / Edit` for the
single DNS zone containing `voice.server_name`. Do not use the Global API Key.

Create the root-only credentials file without putting the token in the central
TOML or shell history:

```sh
sudo ./install-acme-cloudflare.sh
sudoedit /etc/ec25toolbox/cloudflare.ini
```

Put exactly this setting in that file and rerun the installer:

```ini
dns_cloudflare_api_token = YOUR_ZONE_SCOPED_TOKEN
```

```sh
sudo chmod 0600 /etc/ec25toolbox/cloudflare.ini
sudo ./install-acme-cloudflare.sh
```

The installer obtains the initial certificate and enables an automatic renewal
timer. After every successful issuance, the deploy hook validates the hostname,
expiry, and key match; atomically copies the certificate and key to the paths in
`[voice]`; and restarts `ec25toolbox-voice` only if it is already running.

Create an A or AAAA record for `voice.server_name` pointing to the public address
that forwards SIP and SRTP to the Pi. It must be **DNS only** (gray cloud), not
Cloudflare-proxied: the normal Cloudflare HTTP proxy and Cloudflare Tunnel do not
carry this SIP TCP connection plus the SRTP UDP range. Cloudflare Spectrum can
proxy arbitrary TCP/UDP only as a separately configured paid Layer-4 product.

Then validate and start both services:

```sh
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --check-config
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --voice-check
sudo systemctl enable --now ec25toolbox-linux ec25toolbox-voice
```

Test future renewal without consuming a production certificate:

```sh
sudo certbot renew --cert-name phone.example.com --dry-run
systemctl list-timers '*certbot*' 'ec25toolbox-acme-renew.timer'
```

The distribution's normal `asterisk.service` must not run simultaneously. The
voice dependency installer stops it if it is active; EC25 Toolbox uses an
isolated Asterisk instance with generated configuration and state.

## Phone app

Use Linphone and register it directly to the Linux gateway:

- Server/domain: the exact DNS name in `voice.server_name`
- Username: `voice.sip_username`
- Password: `voice.sip_password`
- Transport: TLS
- Port: the port in `voice.sip_bind`, normally `5061`
- Media encryption: SRTP, mandatory (not ZRTP)
- Server certificate verification: enabled

Incoming cellular calls ring every registered contact, and the first app to
answer owns the call. Calls dialed in the app are sent through the EC25 and use
the SIM's cellular line. The generated endpoint accepts only the networks in
`voice.permitted_networks`. A mobile client has changing source addresses, so a
public deployment will normally need `permitted_networks = ["0.0.0.0/0"]` and
must rely on TLS, SRTP, the long SIP password, and firewall abuse controls.

Forward the configured SIP **TCP** port and RTP **UDP** range through the router
to this host. Never forward TCP/UDP 5060: no plaintext listener is generated.
Rate-limit TCP 5061 and add log-based blocking for repeated authentication
failures. Certificate verification encrypts and authenticates the Linphone-to-
Asterisk link; it does not make an Internet-facing PBX immune to password attacks.

On iOS, arbitrary self-hosted Asterisk does not provide Linphone's push service.
The operating system may suspend Linphone and prevent reliable background rings;
TLS/SRTP does not change that mobile-platform limitation.

## USB identity and driver binding

Normal operation probes no VID/PID and does not alter the dongle. If Linux does
not expose its serial interfaces, place the dongle's current `vvvv:pppp` value
from `lsusb` in `modem.option_driver_ids`. The supplied pre-start helper binds
only the configured interfaces to Linux's `option` driver. That changes host
driver association, not the dongle identity or firmware.

## Diagnostics

```sh
journalctl -u ec25toolbox-voice -u ec25toolbox-linux -f
sudo /usr/sbin/asterisk -C /run/ec25toolbox-asterisk/asterisk.conf \
  -rx 'quectel show devices'
```

`--voice-check` verifies files, ports, Asterisk, the channel module, and the
certificate/key pair. Only a real incoming and outgoing call can verify carrier
voice support, Linphone interoperability, NAT traversal, and two-way encrypted
audio. Physical modem, carrier, SIP-client, and SMTP delivery cannot be verified
on the development Mac.

## Tests

```sh
cd SIM_Linux
python3 -m unittest discover -s tests -v
```
