# Optional Raspberry Pi voice-bridge deployment

Use this guide only with a confirmed compatible modem/audio path and carrier voice service. It is not needed for SMS, call rejection, the web portal or Proton archives. Use [RPI_DEPLOYMENT.md](RPI_DEPLOYMENT.md) for those features. Disable the portal before enabling voice mode.

# Raspberry Pi deployment guide

This guide deploys the complete EC25 Toolbox gateway on a Raspberry Pi. The
finished system uses one DJI/Quectel EC25 dongle for cellular calls and SMS,
bridges calls to Linphone over TLS and SRTP, and sends SMS and call events by
SMTP. All runtime settings live in `/etc/ec25toolbox/config.toml`; the only
separate value is the root-only Cloudflare API token used for certificate
renewal.

## 1. Check the non-software requirements

You need:

- A 64-bit Raspberry Pi OS or Debian installation using `systemd`, `apt`, and
  Python 3.11 or later.
- A Raspberry Pi with reliable power and enough free storage to compile
  Asterisk. A powered USB hub is recommended if the EC25 is not stable when
  powered directly by the Pi.
- A voice-and-SMS SIM and an EC25 hardware/firmware variant that the carrier
  permits to make voice calls. Software cannot substitute for a missing
  circuit-switched or carrier-authorized VoLTE voice bearer.
- A domain whose DNS is hosted by Cloudflare.
- SMTP account details for the mailbox that will forward notifications.
- For Linphone use outside the home network, a publicly reachable IPv4 address
  and control of the router's port-forwarding rules.

Before continuing, check the WAN address shown by the router. If the router's
WAN address is private (`10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`) or
carrier-grade NAT (`100.64.0.0/10`), this direct, no-VPN deployment cannot
receive Internet SIP/RTP traffic. Ask the ISP for a public address. Cloudflare
DNS and ACME certificates do not bypass CGNAT.

Do not expose ports 80, 443, or 5060 for this application.

## 2. Prepare the Pi

Update the operating system and install basic tools:

```sh
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y ca-certificates git openssl
sudo reboot
```

After reconnecting, confirm that the OS is 64-bit and Python is new enough:

```sh
uname -m
python3 --version
```

`uname -m` should report `aarch64`, and Python must be 3.11 or later.

## 3. Copy the project to the Pi

The safest choice while this port is still local work is to copy the complete
`SIM_Linux` directory from the development computer. Run this on the
development computer, replacing the host name or address:

```sh
rsync -av SIM_Linux/ pi@raspberrypi.local:~/SIM_Linux/
```

If the current changes have been committed and pushed to GitHub instead, run
this on the Pi:

```sh
git clone https://github.com/MarkSong535/EC25Toolbox.git
cd EC25Toolbox/SIM_Linux
```

For the copied-directory method, use:

```sh
cd ~/SIM_Linux
```

## 4. Connect and identify the EC25

Connect the dongle, wait several seconds, and inspect the interfaces:

```sh
lsusb
ls -l /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null
```

The persistent device mapping confirmed for this dongle is:

- `/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0`: AT commands, calls, and SMS
- `/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0`: serial voice audio

Verify that both links exist on the Pi before installation. If they do, use
them exactly as shown and leave `modem.option_driver_ids = []`. If the links do
not appear, inspect `lsusb` and the `/dev/ttyUSB*` devices before changing the
configuration. Nothing in this deployment rewrites the USB identity.

## 5. Build and install the gateway

Run the voice installer first. It installs or builds Asterisk, then builds the
pinned `asterisk-chan-quectel` driver with the included SMS-during-call safety
patch. The pinned Asterisk source comes from Asterisk's official archived
release directory so it remains available after a newer point release becomes
current. Compilation can take a while on a Pi. If the initial parallel build
fails, the installer automatically retries with one job and full compiler
output; this both reduces memory pressure and preserves the real error instead
of reporting only a generic PJSIP `Error 2`.

```sh
cd ~/SIM_Linux
sudo ./install-voice.sh
sudo ./install.sh
```

The second installer creates the service account, systemd units, udev rule,
state directories, and the central configuration file. It preserves an
existing `/etc/ec25toolbox/config.toml` during later upgrades.

## 6. Configure the gateway

Generate a SIP password using only characters accepted by the generated
Asterisk configuration:

```sh
openssl rand -hex 24
```

Open the central configuration:

```sh
sudoedit /etc/ec25toolbox/config.toml
```

Review the whole file. At minimum, replace the following example values:

```toml
[modem]
port = "auto"
bind_option_driver = true
option_driver_ids = []

[sms]
enabled = true
forward_existing_on_first_start = false
delete_after_forward = true

[calls]
mode = "bridge"
send_email = true

[voice]
enabled = true
data_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0"
audio_mode = "serial"
audio_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0"
sip_username = "ec25phone"
sip_password = "PASTE_THE_RANDOM_PASSWORD_HERE"
server_name = "phone.example.com"
sip_bind = "0.0.0.0:5061"
tls_cert_file = "/etc/ec25toolbox/tls/fullchain.pem"
tls_private_key_file = "/etc/ec25toolbox/tls/privkey.pem"
external_address = "203.0.113.10"
rtp_start = 10000
rtp_end = 10100
max_contacts = 5
codecs = ["ulaw", "alaw"]
permitted_networks = ["0.0.0.0/0"]

[acme]
enabled = true
email = "admin@example.com"
credentials_file = "/etc/ec25toolbox/cloudflare.ini"
propagation_seconds = 30
staging = false

[smtp]
enabled = true
host = "smtp.protonmail.ch"
port = 587
security = "starttls"
username = "sender@your-domain.example"
password = "PASTE_THE_PROTON_SMTP_TOKEN"
from_address = "sender@your-domain.example"
to_addresses = ["recipient@example.com"]
subject_prefix = "[EC25]"
```

Use the real public IPv4 address for `external_address`. Keep the confirmed
BAIWANG persistent serial paths above. If those links do not appear, stop and
diagnose the USB interfaces rather than substituting an assumed port or USB ID.

With `forward_existing_on_first_start = false`, old messages already stored on
the SIM are recorded as the initial baseline rather than emailed as new SMS.

For Proton, sign in through a browser and open **Settings > All settings >
IMAP/SMTP > SMTP tokens**. Generate a token specifically for this Pi and paste
the generated SMTP token into `smtp.password`. Use the paired email address for
both `smtp.username` and `smtp.from_address`; the normal Proton account password
will not work. Proton's public submission endpoint requires STARTTLS on port
587. The program validates the server certificate before it sends the token or
message.

Check the configuration before requesting a certificate:

```sh
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --check-config
```

Do not continue until this command succeeds.

## 7. Create DNS and issue the TLS certificate

In Cloudflare DNS, create an `A` record:

- Name: the host portion used by `voice.server_name`, such as `phone`
- IPv4 address: the router's public IPv4 address
- Proxy status: **DNS only** (gray cloud)

The normal orange-cloud proxy cannot carry this SIP TCP and RTP UDP traffic.

In the Cloudflare dashboard, create an API token with:

- Permission: `Zone` / `DNS` / `Edit`
- Zone resource: only the zone containing `voice.server_name`

Do not use the Global API Key. Then run the ACME installer once:

```sh
cd ~/SIM_Linux
sudo ./install-acme-cloudflare.sh
```

The first run creates an empty, root-only token file and stops. Open it:

```sh
sudoedit /etc/ec25toolbox/cloudflare.ini
```

Add exactly one credential line:

```ini
dns_cloudflare_api_token = PASTE_THE_ZONE_SCOPED_TOKEN_HERE
```

Issue the certificate and enable automatic renewal:

```sh
sudo chmod 0600 /etc/ec25toolbox/cloudflare.ini
sudo ./install-acme-cloudflare.sh
```

DNS-01 uses temporary DNS TXT records, so the Pi needs outbound HTTPS but does
not need inbound ports 80 or 443.

## 8. Configure the router and Pi firewall

Give the Pi a fixed LAN address using a router DHCP reservation. Forward these
ports from the router's public address to that Pi address:

| Public protocol/port | Pi protocol/port | Purpose |
| --- | --- | --- |
| TCP 5061 | TCP 5061 | SIP over TLS |
| UDP 10000-10100 | UDP 10000-10100 | SRTP audio |

The forwarded RTP range must exactly match `voice.rtp_start` and
`voice.rtp_end`. Do not forward TCP or UDP 5060.

If UFW is already used, permit SSH before enabling it and then add only the
gateway ports. Replace `22` if SSH uses a different port:

```sh
sudo apt install -y ufw
sudo ufw allow 22/tcp
sudo ufw allow 5061/tcp
sudo ufw allow 10000:10100/udp
sudo ufw enable
sudo ufw status verbose
```

If another firewall manager is already active, add equivalent rules there
instead of installing UFW.

## 9. Validate and start the services

Run the static voice check after the certificate has been installed:

```sh
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --voice-check
```

It should report that static prerequisites are present. Then enable and start
both services:

```sh
sudo systemctl disable --now asterisk.service 2>/dev/null || true
sudo systemctl enable --now ec25toolbox-linux.service
sudo systemctl enable --now ec25toolbox-voice.service
sudo systemctl --no-pager --full status \
  ec25toolbox-linux.service ec25toolbox-voice.service
```

Confirm that the modem registered with Asterisk:

```sh
sudo /usr/sbin/asterisk \
  -C /run/ec25toolbox-asterisk/asterisk.conf \
  -rx 'quectel show devices'
```

If it is not ready, inspect the live logs:

```sh
journalctl -u ec25toolbox-linux -u ec25toolbox-voice -f
```

## 10. Configure Linphone

Create a SIP account in Linphone with:

- SIP identity/username: the `voice.sip_username` value
- Password: the `voice.sip_password` value
- Domain or server: the exact `voice.server_name` DNS name
- Port: `5061`
- Transport: TLS
- Media encryption: mandatory SRTP using SDES, not ZRTP
- Server certificate verification: enabled

Do not configure an outbound VoIP provider. The Asterisk instance on the Pi is
the SIP server, and outgoing app calls use the physical SIM through the EC25.

Test Linphone first while connected to a different network, such as cellular
data. Testing through the same router can fail on routers without NAT loopback
even when external access is configured correctly.

## 11. End-to-end acceptance test

Perform all of these checks; a successful service start alone does not prove
that the carrier supports voice or that NAT traversal works:

1. Send an SMS to the EC25 SIM. Confirm that the selected recipient receives an
   email whose subject is exactly the sender's phone number and whose body is
   exactly the SMS text.
2. Call the EC25 number. Confirm that Linphone rings, audio works in both
   directions after answering, and a call-event email is delivered.
3. From Linphone, call a normal telephone number. Confirm that the call uses the
   EC25 SIM and has two-way audio.
4. Stop and start both services and repeat an SMS test to confirm persistence.
5. Check renewal without obtaining another production certificate:

   ```sh
   sudo certbot renew --cert-name phone.example.com --dry-run
   systemctl list-timers '*certbot*' 'ec25toolbox-acme-renew.timer'
   ```

Replace `phone.example.com` with the exact `voice.server_name` value.

## 12. Updating the installation

Copy or pull the updated `SIM_Linux` directory, then rerun the installers:

```sh
cd ~/SIM_Linux
sudo ./install-voice.sh
sudo ./install.sh
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --check-config
sudo /opt/ec25toolbox/ec25toolbox-linux \
  --config /etc/ec25toolbox/config.toml --voice-check
sudo systemctl restart ec25toolbox-linux ec25toolbox-voice
```

The central configuration and event database are preserved. Rerunning
`install-voice.sh` rebuilds the pinned Asterisk channel driver, so allow time
for compilation.

## Troubleshooting commands

```sh
systemctl --no-pager --full status ec25toolbox-linux ec25toolbox-voice
journalctl -u ec25toolbox-linux -u ec25toolbox-voice --since today
ls -l /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null
sudo /usr/sbin/asterisk \
  -C /run/ec25toolbox-asterisk/asterisk.conf \
  -rx 'pjsip show contacts'
sudo /usr/sbin/asterisk \
  -C /run/ec25toolbox-asterisk/asterisk.conf \
  -rx 'quectel show devices'
```

Interpret common failures as follows:

- No `/dev/ttyUSB*`: USB power, cable, kernel driver binding, or the configured
  `option_driver_ids` is wrong.
- Quectel device is not ready: wrong AT/audio ports, SIM PIN, poor signal,
  missing network registration, or unsupported carrier voice service.
- Linphone cannot register: DNS, port forwarding, firewall, certificate name,
  credentials, or CGNAT is wrong.
- Calls connect with no audio: the RTP UDP forwarding/range, NAT address, EC25
  audio port/mode, or carrier voice path is wrong.
- SMS is received but no email arrives: inspect SMTP errors in
  `ec25toolbox-linux` logs and verify the SMTP host, credentials, encryption
  mode, sender, and recipient in the central TOML.
