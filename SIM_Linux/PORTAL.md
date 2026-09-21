# RPI SMS portal

The viewport layout follows marksong.tech's blue/light and slate/dark palette.
The fixed top-right green/red status dot opens access logs; hover shows SIM status.
There is no branding header/footer. Lists scroll inside their panes.
Conversations stay vertical at every width. At 600px and below, the supplied
three-line SVG toggles a sidebar drawer containing New message, search,
conversations, older-message pagination and Archives. Selecting an item closes
the drawer; Escape/outside-click also close it. Closed drawers are inert and an
open drawer prevents focus entering the workspace. The title/subtitle align with
the menu icon. The send control contains only `Send` and the supplied plane SVG;
the removed character helper does not change the 70-character validation.
There is no separate Activity log button; the server audit records/API remain.
Archive names use the site link colors and open Proton Drive in a new tab, not
a direct file download. Folder-path text is not shown in the archive view.
Access logs record each observed session's first portal visit, server time and
browser/device classification from its User-Agent. That header is spoofable.
In tunnel mode the identity is explicitly edge-reported, not independently
verified. Failed Cloudflare logins and logins before this feature was enabled
are not available locally. Reissued Access tokens can create a new session row.
No bearer tokens are stored in access logs. Without an Access token, a signed
browser-session cookie deduplicates visits (renewed after 24 hours/restart).

For upload-only Proton Drive archives and recovery, see [ARCHIVES.md](ARCHIVES.md).

## iOS Home Screen

In Safari, sign in at the HTTPS portal, then Share > Add to Home Screen
(enable Open as Web App if offered). Open the saved icon to run without Safari's
address/toolbar; iOS still controls its status bar and system indicators. Remove
and re-add an older shortcut if its icon or launch mode is cached. Access login
may be requested again; no authentication rule is bypassed.

The page declares Apple's standalone metadata and a same-origin PNG copied
from `https://img.markso.ng/apple-touch-icon.png` (the original 192×192 PNG,
copied without resizing). No UI/CSS changes or offline/service-worker
caching are added. Real iOS installation must be verified on a device; desktop
tests verify metadata and the icon response only.

Reference: [Apple web app configuration](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html).

A private, responsive conversation inbox at `https://phone.markso.ng`. Origin:
`http://127.0.0.1:9561` on the Pi. All runtime settings stay in
`/etc/ec25toolbox/config.toml`. No separate frontend configuration or cloud message store.

## Features and boundaries

- Browse/search received and sent messages, grouped by number. Load older pages
  to browse the entire retained history. Search covers the pages already loaded.
- Existing messages in the gateway SQLite database remain visible, even after
  modem copies are removed following email forwarding. Deleted messages that
  were never recorded by the gateway cannot be recovered by this portal.
- Compose/reply using a full international number, e.g. `+15025550123`.
  Version 1 sends one UCS-2 segment: 1–70 BMP characters, including Chinese;
  emoji, multipart SMS, short-code destinations and attachments are unsupported.
- `sent` means the modem returned `+CMGS` and `OK`, not recipient delivery.
  An uncertain response or process crash becomes `unknown` and is never retried
  automatically. Confirm on the recipient phone before manually sending again.
- Existing incoming SMS email forwarding and call rejection continue. Portal
  sending requires `voice.enabled=false`; it shares the daemon's single AT owner.
  An SMS submission can occupy that connection for up to 60 seconds, delaying
  call rejection during submission. Immediate rejection cannot be guaranteed then.
- No client edit/delete, SQL, file, shell, AT command, model-prompt or settings API.
  Outbound requests and audit entries are append-only, enforced by SQLite triggers.
  Received message bodies are also protected by triggers. Only server-controlled
  delivery metadata changes. Audit records begin when the portal is first enabled;
  they include SMS records, SMTP outcomes and outbound submission transitions.
- Historical `processed` email status includes startup-baselined messages that
  were intentionally not emailed. New SMTP acceptance has its own audit entry.

## Install on Raspberry Pi

Keep `SIM_Linux` together on the Pi. First back up the central config, installed
application, and SQLite database using SQLite's backup API (not a live plain copy).

```sh
sudo apt-get install python3-venv
sudo sh ./install-portal.sh
```

This installs pinned dependencies in `/opt/ec25toolbox/venv`, application assets
and a systemd interpreter override. It preserves the central config and does not
restart the running gateway or change any Cloudflare resources.

Add this table to the **existing** central config exactly once:

```toml
[portal]
enabled = true
auth_mode = "tunnel"
origin = "https://phone.markso.ng"
port = 9561
sends_per_hour = 20
sends_per_day = 100
```

`tunnel` mode was requested for this deployment: authentication is delegated to
the operator's Cloudflare Access setup. No JWT/AUD values are needed locally.
The listener is always loopback-only; there is deliberately no configurable
`0.0.0.0` listener. Do not forward this port with FRP, a public reverse proxy,
or a Tunnel route lacking Access. Local processes on the Pi are trusted in this
mode and can read messages. The audit actor is explicitly `Tunnel client
(identity managed at edge)`; unsigned email headers are not treated as verified
individual identities.

```sh
sudo /opt/ec25toolbox/venv/bin/python /opt/ec25toolbox/ec25toolbox-linux --check-config
sudo systemctl restart ec25toolbox-linux
sudo systemctl status ec25toolbox-linux --no-pager
curl -I http://127.0.0.1:9561/
```

## Cloudflare Tunnel and Access

On the existing same-Pi `cloudflared` tunnel, configure the published application
route `phone.markso.ng` to **HTTP** `127.0.0.1:9561`. Preserve other tunnel routes.
If cloudflared runs inside a container, its loopback is not the Pi's; use host
networking or move the connector onto the Pi host rather than making the origin public.

Before publishing, create/verify an Access self-hosted application covering the
**whole hostname**, including `/api/*` and `/assets/*`. Allow only the intended
identities, require MFA at your identity provider, and do not add Bypass or Everyone
policies. Ensure no more-specific path application weakens protection. Do not
cache HTML/API responses; the origin returns `Cache-Control: no-store` throughout.

Use a private browser window to verify unauthenticated requests are redirected
to Access, a disallowed identity is blocked, and your permitted identity can
read the inbox. Confirm no LAN/public address can reach port 9561 directly.
The app cannot verify the edge policy in `tunnel` mode. An active cloudflared
service alone does not prove that the route or Access policy exists.

For defense in depth, switch the same table to:

```toml
auth_mode = "access_jwt"
team_domain = "YOUR-TEAM.cloudflareaccess.com"
audience = "YOUR-APPLICATION-AUD"
allowed_emails = ["YOUR-ALLOWED-EMAIL"]
```

Keep the existing `enabled`, `origin`, `port` and rate limits in that table. This
mode independently verifies RS256 signatures, issuer, audience, expiry, issuance
time, human identity and the exact allowed email on every route. Missing/invalid
tokens fail closed. Signing keys come only from the configured Cloudflare team,
with bounded HTTPS fetches and a cache. Direct loopback requests then receive 401.

Official references:
[Access JWT verification](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/),
[Tunnel origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/).

## Security and operation

All browser message rendering uses `textContent`, never HTML/Markdown. A strict
Content Security Policy blocks inline scripts, external assets and framing. There
is no AI integration, so incoming text cannot act as a system prompt. If a future
AI feature is added, SMS must remain untrusted data with no tool authority.

Sending requires JSON, exact HTTPS Origin, a server-generated CSRF token, an
allowed field set, a UUID idempotency key, strict recipient/text checks and
persisted global hourly/daily limits. Duplicate submissions reuse their original
request. SQL values are parameterized. The modem receives only encoded hex PDU
data; user content cannot terminate its input or inject another AT command.
Waitress limits HTTP headers, bodies and concurrent connections. No CORS access
is granted. Use Access/WAF rate limiting at the edge as well for network floods.

The Pi's root and gateway service account remain trusted: SQLite triggers are
not tamper-proof against a host administrator, disk access or server compromise.
If logs must survive a compromised Pi, export them to separately controlled
append-only/WORM storage. Browser-side changes in developer tools only alter
that browser's display, never the authoritative stored records.

The portal shares the gateway process, so it has the gateway's privileges.
It does not expose credentials or configuration through its API. Keep packages
updated, restrict Pi login, and back up the database securely (it contains SMS,
including verification codes). SMS itself is not end-to-end encrypted.

## Verification and rollback

```sh
/opt/ec25toolbox/venv/bin/python -m unittest discover -s ./tests -v
```

Tests use generated signing keys and an emulated serial modem. They exercise
authentication failures, CSRF, malicious inputs, immutable tables, pagination,
rate limits, crash recovery, PDU encoding and prompt/submission/restore framing.
They do not send carrier SMS. Use a number you control for a live test, verify
the received content, and inspect the portal activity status.

Disable `[portal].enabled` and restart `ec25toolbox-linux` to close the listener
while retaining SMS-to-email. History/audit protection remains in the database.
To restore old code, disable the portal first, restore the application backup and
remove only the portal interpreter override if that old version requires it.
Do not restore an old database over newer messages as a routine rollback.
