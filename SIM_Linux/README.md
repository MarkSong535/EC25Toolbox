# Raspberry Pi SMS gateway and private inbox

Self-contained Linux implementation: modem SMS reception, SMTP forwarding,
incoming-call rejection, responsive SMS portal, and upload-only Proton Drive
archives. All Linux code, templates, tests and instructions live here. The
original macOS/Swift application remains separate and unchanged.

## Documentation index

| Task | File |
| --- | --- |
| Install the SMS gateway and portal | [RPI_DEPLOYMENT.md](RPI_DEPLOYMENT.md) |
| Complete runtime settings, including optional voice | [config.example.toml](config.example.toml) |
| Minimal SMS/portal template | [config.portal.example.toml](config.portal.example.toml) |
| Portal authentication, API security and UI | [PORTAL.md](PORTAL.md) |
| Proton CLI, login, scheduling and recovery | [ARCHIVES.md](ARCHIVES.md) |
| File map, settings, updates and troubleshooting | [OPERATIONS.md](OPERATIONS.md) |
| Optional voice design / deployment | [VOICE.md](VOICE.md), [VOICE_DEPLOYMENT.md](VOICE_DEPLOYMENT.md) |
| Tests and manual commit/push checklist | [DEVELOPMENT.md](DEVELOPMENT.md) |

## Data flow

```text
SIM modem <-> single Python serial owner <-> SQLite state
                  |                           |
                  +-> reject incoming calls  +-> SMTP queue -> Proton SMTP -> recipient
                                              +-> loopback portal :9561
                                                   ^
                                      browser -> Cloudflare Access + Tunnel

SQLite snapshot -> tar.gz -> Proton Drive upload -> download/hash verification
                                                 -> verified SQLite receipt
```

Application settings have one source: `/etc/ec25toolbox/config.toml`. Templates
are alternatives, not layered configs. External credentials (Proton/pass/GPG,
Tunnel token and optional ACME token) remain in their own protected stores.
Never commit those stores, real configs, messages, archives or logs.

## Capabilities and limits

- No USB ID or firmware change required. BAIWANG AT path:
  `/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0`.
- SMS email subject is the sender number; body is original SMS text. The sender
  display name (`RPI SMS`) is separate from the authorized email address.
- Calls are rejected on the first observed ring by default. An active serial
  operation, particularly outbound SMS, may delay rejection.
- Outbound portal SMS: one UCS-2 segment, up to 70 supported BMP characters.
  No emoji, multipart sending or attachments. `sent` means modem acceptance,
  not delivery. Unknown outcomes are not automatically retried.
- Conversations always remain vertical. Mobile uses a menu drawer; the fixed
  SIM dot opens access logs. It indicates initialized modem connectivity, not
  independent proof of carrier registration or SMS delivery.
- No client edit/delete API. Host administrators remain trusted; SQLite triggers
  are not tamper-proof/WORM storage.
- Archives are ordinary tar.gz containing SQLite/JSONL/EML. Proton applies its
  own end-to-end encryption; GPG protects credentials, not these archive files.
  Temporary export copies are removed, but live history is never auto-pruned.
- Archive filenames open Proton Drive, not direct per-file download URLs.
- One modem/active SIM subscription is supported. There is no SIM cloning,
  concurrent multi-SIM use, eSIM conversion, iMessage registration or Apple relay.
- Optional Asterisk voice code is retained, not guaranteed for every dongle.
  Earlier deployment work identified QDC507 audio compatibility as a blocker;
  BAIWANG naming alone does not prove EC25 voice support. Without VoLTE or a
  supported carrier 2G/3G voice bearer, software cannot supply cellular audio.

## Verification boundary

Tests emulate modem, SMTP and Drive operations. Browser tests use intercepted
requests and a temporary database; they send no real SMS. These checks do not
prove real carrier delivery, Proton upload, account authorization or call audio.
Follow the deployment acceptance checks. Documentation is not a live status report.

Repository: `git@github.com:MarkSong535/EC25Toolbox.git`. Preserve the repository
license, upstream notices and channel-driver patch attribution.
