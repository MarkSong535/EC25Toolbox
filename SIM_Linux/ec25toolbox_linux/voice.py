from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import ssl
import time

from .config import AppConfig
from .storage import EventStore


RUNTIME_CONFIG_DIR = Path("/run/ec25toolbox-asterisk")


@dataclass(frozen=True)
class VoiceCheck:
    ok: bool
    messages: tuple[str, ...]


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def render_asterisk(config: AppConfig, output_dir: Path = RUNTIME_CONFIG_DIR) -> tuple[Path, ...]:
    """Generate the complete Asterisk instance from the central TOML file."""
    if not config.voice.enabled:
        raise ValueError("voice.enabled is false")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o750)
    state_dir = config.service.database_path.parent / "asterisk"
    for child in ("db", "keys", "log", "run", "spool"):
        (state_dir / child).mkdir(parents=True, exist_ok=True)

    voice = config.voice
    sms_database = state_dir / "smsdb"
    allow = ",".join(voice.codecs)
    permit_lines = "\n".join(f"permit={network}" for network in voice.permitted_networks)
    external_lines = ""
    if voice.external_address:
        external_lines = (
            f"external_signaling_address={voice.external_address}\n"
            f"external_media_address={voice.external_address}\n"
        )
    if voice.audio_mode == "serial":
        audio_settings = f"audio={voice.audio_port}\n"
    else:
        audio_settings = f"quec_uac=1\nalsadev={voice.alsa_device}\n"

    files = {
        "asterisk.conf": f"""; Generated from {config.source}; do not edit.
[directories]
astetcdir => {output_dir}
astdbdir => {state_dir / 'db'}
astkeydir => {state_dir / 'keys'}
astspooldir => {state_dir / 'spool'}
astlogdir => {state_dir / 'log'}
astrundir => {state_dir / 'run'}
astagidir => /opt/ec25toolbox/agi-bin

[options]
documentation_language = en_US
""",
        "modules.conf": """; Generated; do not edit.
[modules]
autoload=yes
noload=chan_sip.so
""",
        "pjsip.conf": f"""; Generated; mandatory TLS signaling and SRTP media.
[transport-ec25-tls]
type=transport
protocol=tls
bind={voice.sip_bind}
cert_file={voice.tls_cert_file}
priv_key_file={voice.tls_private_key_file}
method=tlsv1_2
symmetric_transport=yes
{external_lines}

[ec25-allowed-networks]
type=acl
deny=0.0.0.0/0
{permit_lines}

[{voice.sip_username}]
type=endpoint
transport=transport-ec25-tls
context=from-softphone
disallow=all
allow={allow}
auth={voice.sip_username}-auth
aors={voice.sip_username}-aor
direct_media=no
force_rport=yes
rewrite_contact=yes
rtp_symmetric=yes
media_encryption=sdes
media_encryption_optimistic=no

[{voice.sip_username}-auth]
type=auth
auth_type=digest
username={voice.sip_username}
password={voice.sip_password}
supported_algorithms_uas=SHA-256

[{voice.sip_username}-aor]
type=aor
max_contacts={voice.max_contacts}
remove_existing=no
qualify_frequency=30
""",
        "extensions.conf": f"""; Generated; chan_quectel owns both calls and SMS.
[general]
static=yes
writeprotect=yes
clearglobalvars=no

[incoming-mobile]
exten => sms,1,NoOp(Incoming SMS from ${{CALLERID(num)}})
 same => n,AGI(ec25toolbox-agi,sms,${{SMS_BASE64}},${{BASE64_ENCODE(${{SMS_TS}})}})
 same => n,Hangup()
exten => s,1,NoOp(Incoming cellular call from ${{CALLERID(num)}})
 same => n,Dial(PJSIP/{voice.sip_username},{voice.ring_seconds})
 same => n,AGI(ec25toolbox-agi,call,${{DIALSTATUS}})
 same => n,Hangup()

[from-softphone]
exten => _X.,1,Dial(Quectel/{voice.modem_name}/${{EXTEN}},120)
 same => n,Hangup()
exten => _+X.,1,Dial(Quectel/{voice.modem_name}/${{EXTEN}},120)
 same => n,Hangup()
""",
        "quectel.conf": f"""; Generated for the pinned sn4f/asterisk-chan-quectel driver.
[general]
interval=15
smsdb={sms_database}
csmsttl=600

[defaults]
context=incoming-mobile
group=0
rxgain={voice.rx_gain}
txgain={voice.tx_gain}
autodeletesms={'yes' if config.sms.delete_after_forward else 'no'}
resetquectel=yes
usecallingpres=yes
callingpres=allowed_passed_screen
disablesms=no
ignoremms=yes
callwaiting=no
initstate=start
dtmf=relax

[{voice.modem_name}]
data={voice.data_port}
{audio_settings}""",
        "rtp.conf": f"""; Generated; do not edit.
[general]
rtpstart={voice.rtp_start}
rtpend={voice.rtp_end}
strictrtp=yes
""",
        "logger.conf": """; Generated; do not edit.
[general]
dateformat=%F %T
[logfiles]
console=notice,warning,error
messages=notice,warning,error
""",
    }
    paths: list[Path] = []
    for name, content in files.items():
        path = output_dir / name
        _write_private(path, content)
        paths.append(path)
    return tuple(paths)


def check_voice(config: AppConfig) -> VoiceCheck:
    messages: list[str] = []
    if not config.voice.enabled:
        return VoiceCheck(False, ("voice.enabled is false",))
    binary = Path(config.voice.asterisk_binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        messages.append(f"Asterisk executable is unavailable: {binary}")
    if not Path(config.voice.data_port).exists():
        messages.append(f"Quectel AT data port is unavailable: {config.voice.data_port}")
    if config.voice.audio_mode == "serial" and not Path(config.voice.audio_port).exists():
        messages.append(f"Quectel serial audio port is unavailable: {config.voice.audio_port}")
    if config.voice.audio_mode == "uac" and shutil.which("arecord") is None:
        messages.append("arecord is unavailable; ALSA UAC presence cannot be checked")
    module_candidates = (
        Path("/usr/lib/asterisk/modules/chan_quectel.so"),
        Path("/usr/lib64/asterisk/modules/chan_quectel.so"),
        Path("/usr/local/lib/asterisk/modules/chan_quectel.so"),
    )
    if not any(path.is_file() for path in module_candidates):
        messages.append("chan_quectel.so is not installed in a standard Asterisk module directory")
    certificate = config.voice.tls_cert_file
    private_key = config.voice.tls_private_key_file
    if not certificate.is_file() or not os.access(certificate, os.R_OK):
        messages.append(f"TLS certificate is unavailable to this user: {certificate}")
    if not private_key.is_file() or not os.access(private_key, os.R_OK):
        messages.append(f"TLS private key is unavailable to this user: {private_key}")
    if certificate.is_file() and private_key.is_file():
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private_key)
            decoded = ssl._ssl._test_decode_cert(str(certificate))
            ssl.match_hostname(decoded, config.voice.server_name)
            if ssl.cert_time_to_seconds(decoded["notAfter"]) <= time.time() + 86400:
                messages.append("TLS certificate is expired or expires within 24 hours")
        except (KeyError, OSError, ssl.SSLError, ssl.CertificateError, ValueError) as error:
            messages.append(f"TLS certificate verification failed: {error}")
    if not messages:
        messages.append("static prerequisites are present; a real cellular call is still required to verify audio")
    ok = len(messages) == 1 and messages[0].startswith("static prerequisites")
    return VoiceCheck(ok, tuple(messages))


def run_asterisk(config: AppConfig, output_dir: Path = RUNTIME_CONFIG_DIR) -> None:
    render_asterisk(config, output_dir)
    binary = config.voice.asterisk_binary
    os.execv(binary, [binary, "-f", "-C", str(output_dir / "asterisk.conf")])


def record_agi_event(config: AppConfig, environment: dict[str, str], arguments: list[str]) -> bool:
    if not arguments:
        return False
    kind = arguments[0]
    caller = environment.get("agi_callerid", "").strip()
    unique_id = environment.get("agi_uniqueid", "")
    store = EventStore(config.service.database_path)
    now = datetime.now(timezone.utc).isoformat()
    if kind == "sms" and len(arguments) >= 2:
        try:
            body = base64.b64decode(arguments[1], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        service_date = ""
        if len(arguments) >= 3 and arguments[2]:
            try:
                service_date = base64.b64decode(arguments[2], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                service_date = ""
        identity = "\0".join((caller, service_date, body))
        event_id = "sms:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return store.enqueue(
            event_id,
            "sms",
            {"sender": caller, "body": body, "service_date": service_date},
            deliver=config.smtp.enabled,
        )
    if kind == "call":
        disposition = arguments[1] if len(arguments) >= 2 else "UNKNOWN"
        fallback = hashlib.sha256(f"{caller}\0{now}".encode()).hexdigest()
        event_id = "call:" + (unique_id or fallback)
        return store.enqueue(
            event_id,
            "call",
            {"caller": caller, "observed_at": now, "disposition": disposition, "rejected": False},
            deliver=config.calls.send_email and config.smtp.enabled,
        )
    return False


def read_agi_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    while True:
        line = input()
        if not line:
            return environment
        name, separator, value = line.partition(":")
        if separator:
            environment[name.strip()] = value.strip()


def run_agi(config: AppConfig, arguments: list[str]) -> int:
    try:
        environment = read_agi_environment()
        stored = record_agi_event(config, environment, arguments)
        print(f'SET VARIABLE EC25_EVENT_STORED "{1 if stored else 0}"', flush=True)
        return 0 if stored else 1
    except EOFError:
        return 1
