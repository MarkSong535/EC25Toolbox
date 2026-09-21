from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ipaddress
import os
import re
import tomllib
from .portal_config import PortalConfig, parse_portal
from .archive_config import ArchiveConfig, parse_archive


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    log_level: str
    database_path: Path
    reconnect_seconds: float
    reconcile_seconds: float


@dataclass(frozen=True)
class ModemConfig:
    port: str
    baudrate: int
    candidate_globs: tuple[str, ...]
    probe_timeout_seconds: float
    command_timeout_seconds: float
    exclusive: bool
    bind_option_driver: bool
    option_driver_ids: tuple[str, ...]
    option_driver_interfaces: tuple[int, ...]


@dataclass(frozen=True)
class SMSConfig:
    enabled: bool
    storages: tuple[str, ...]
    forward_existing_on_first_start: bool
    delete_after_forward: bool


@dataclass(frozen=True)
class CallConfig:
    mode: str
    reject: bool
    reject_delay_ms: int
    send_email: bool


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool
    modem_name: str
    data_port: str
    audio_mode: str
    audio_port: str
    alsa_device: str
    rx_gain: int
    tx_gain: int
    ring_seconds: int
    sip_username: str
    sip_password: str
    server_name: str
    sip_bind: str
    tls_cert_file: Path
    tls_private_key_file: Path
    external_address: str
    rtp_start: int
    rtp_end: int
    max_contacts: int
    codecs: tuple[str, ...]
    permitted_networks: tuple[str, ...]
    asterisk_binary: str


@dataclass(frozen=True)
class SMTPConfig:
    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password: str
    from_address: str
    to_addresses: tuple[str, ...]
    subject_prefix: str
    timeout_seconds: float
    retry_min_seconds: float
    retry_max_seconds: float
    from_name: str = ""


@dataclass(frozen=True)
class ACMEConfig:
    enabled: bool
    email: str
    credentials_file: Path
    propagation_seconds: int
    staging: bool


@dataclass(frozen=True)
class AppConfig:
    source: Path
    service: ServiceConfig
    modem: ModemConfig
    sms: SMSConfig
    calls: CallConfig
    voice: VoiceConfig
    smtp: SMTPConfig
    acme: ACMEConfig
    portal: PortalConfig = PortalConfig()
    archive: ArchiveConfig = ArchiveConfig()


def _section(document: dict, name: str) -> dict:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def _tuple_of_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{name} must be an array of strings")
    return tuple(value)


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as error:
        raise ConfigurationError(f"cannot read configuration {source}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid TOML in {source}: {error}") from error

    service = _section(document, "service")
    modem = _section(document, "modem")
    sms = _section(document, "sms")
    calls = _section(document, "calls")
    voice = _section(document, "voice")
    smtp = _section(document, "smtp")
    acme = _section(document, "acme")
    try:
        portal = parse_portal(_section(document, "portal"))
        archive = parse_archive(_section(document, "archive"))
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    if portal.enabled and bool(voice.get("enabled", False)):
        raise ConfigurationError("portal currently requires voice.enabled=false (single AT owner)")

    security = str(smtp.get("security", "starttls")).lower()
    if security not in {"starttls", "tls", "plain"}:
        raise ConfigurationError("smtp.security must be starttls, tls, or plain")

    recipients = _tuple_of_strings(smtp.get("to_addresses", []), "smtp.to_addresses")
    smtp_enabled = bool(smtp.get("enabled", True))
    smtp_host = str(smtp.get("host", ""))
    smtp_port = int(smtp.get("port", 587))
    smtp_username = str(smtp.get("username", ""))
    smtp_password = str(smtp.get("password", ""))
    smtp_from_address = str(smtp.get("from_address", ""))
    required = {
        "smtp.host": smtp_host,
        "smtp.from_address": smtp_from_address,
    }
    if smtp_enabled:
        missing = [name for name, value in required.items() if not value]
        if not recipients:
            missing.append("smtp.to_addresses")
        if missing:
            raise ConfigurationError("missing required SMTP settings: " + ", ".join(missing))
    if smtp_enabled and smtp_host.lower() == "smtp.protonmail.ch":
        if smtp_port != 587 or security != "starttls":
            raise ConfigurationError(
                "smtp.protonmail.ch requires smtp.port=587 and smtp.security=\"starttls\""
            )
        proton_missing = []
        if not smtp_username:
            proton_missing.append("smtp.username")
        if not smtp_password or smtp_password in {"CHANGE_ME", "SMTP_TOKEN"}:
            proton_missing.append("smtp.password (the generated Proton SMTP token)")
        if proton_missing:
            raise ConfigurationError("missing required Proton SMTP settings: " + ", ".join(proton_missing))
        if smtp_from_address.lower() != smtp_username.lower():
            raise ConfigurationError(
                "Proton SMTP requires smtp.from_address to match the address paired with smtp.username"
            )

    reject_delay_ms = int(calls.get("reject_delay_ms", 0))
    if reject_delay_ms < 0 or reject_delay_ms > 10_000:
        raise ConfigurationError("calls.reject_delay_ms must be between 0 and 10000")

    voice_enabled = bool(voice.get("enabled", False))
    call_mode = str(calls.get("mode", "bridge" if voice_enabled else "reject")).lower()
    if call_mode not in {"reject", "observe", "bridge"}:
        raise ConfigurationError("calls.mode must be reject, observe, or bridge")
    if call_mode == "bridge" and not voice_enabled:
        raise ConfigurationError("calls.mode=bridge requires voice.enabled=true")
    if voice_enabled and call_mode != "bridge":
        raise ConfigurationError("voice.enabled=true requires calls.mode=bridge")

    audio_mode = str(voice.get("audio_mode", "serial")).lower()
    if audio_mode not in {"serial", "uac"}:
        raise ConfigurationError("voice.audio_mode must be serial or uac")
    data_port = str(voice.get("data_port", ""))
    audio_port = str(voice.get("audio_port", ""))
    alsa_device = str(voice.get("alsa_device", ""))
    if voice_enabled:
        missing_voice: list[str] = []
        if not data_port:
            missing_voice.append("voice.data_port")
        if audio_mode == "serial" and not audio_port:
            missing_voice.append("voice.audio_port")
        if audio_mode == "uac" and not alsa_device:
            missing_voice.append("voice.alsa_device")
        if missing_voice:
            raise ConfigurationError("missing required voice settings: " + ", ".join(missing_voice))

    sip_username = str(voice.get("sip_username", "ec25phone"))
    sip_password = str(voice.get("sip_password", ""))
    server_name = str(voice.get("server_name", ""))
    modem_name = str(voice.get("modem_name", "quectel0"))
    safe_name = re.compile(r"^[A-Za-z0-9_-]+$")
    if not safe_name.fullmatch(modem_name):
        raise ConfigurationError("voice.modem_name may contain only letters, digits, _ and -")
    if not safe_name.fullmatch(sip_username):
        raise ConfigurationError("voice.sip_username may contain only letters, digits, _ and -")
    if voice_enabled:
        if len(sip_password) < 16 or sip_password == "CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD":
            raise ConfigurationError("voice.sip_password must be at least 16 characters and not the example value")
        if not re.fullmatch(r"[A-Za-z0-9_.@!$%^&*+\-]+", sip_password):
            raise ConfigurationError("voice.sip_password contains characters unsafe for Asterisk configuration")
        if not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
            server_name,
        ):
            raise ConfigurationError("voice.server_name must be the DNS name covered by the TLS certificate")

    codecs = _tuple_of_strings(voice.get("codecs", ["ulaw", "alaw"]), "voice.codecs")
    supported_codecs = {"ulaw", "alaw", "gsm", "g722"}
    if not codecs or any(codec not in supported_codecs for codec in codecs):
        raise ConfigurationError("voice.codecs must use one or more of: ulaw, alaw, gsm, g722")
    permitted_networks = _tuple_of_strings(
        voice.get("permitted_networks", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]),
        "voice.permitted_networks",
    )
    for network in permitted_networks:
        try:
            ipaddress.ip_network(network, strict=False)
        except ValueError as error:
            raise ConfigurationError(f"invalid voice.permitted_networks entry {network}: {error}") from error
    sip_bind = str(voice.get("sip_bind", "0.0.0.0:5061"))
    if "\n" in sip_bind or "\r" in sip_bind or not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", sip_bind):
        raise ConfigurationError("voice.sip_bind is not a safe address:port value")
    tls_cert_file = Path(str(voice.get("tls_cert_file", "")))
    tls_private_key_file = Path(str(voice.get("tls_private_key_file", "")))
    if voice_enabled and (not tls_cert_file.is_absolute() or not tls_private_key_file.is_absolute()):
        raise ConfigurationError("voice TLS certificate and private-key paths must be absolute")
    if voice_enabled and any("\n" in str(path) or "\r" in str(path) for path in (tls_cert_file, tls_private_key_file)):
        raise ConfigurationError("voice TLS certificate paths must not contain line breaks")
    if voice_enabled and tls_cert_file == tls_private_key_file:
        raise ConfigurationError("voice TLS certificate and private key must be separate files")
    external_address = str(voice.get("external_address", ""))
    if external_address:
        try:
            ipaddress.ip_address(external_address)
        except ValueError as error:
            raise ConfigurationError("voice.external_address must be a public IP address, not a hostname") from error
    rtp_start = int(voice.get("rtp_start", 10000))
    rtp_end = int(voice.get("rtp_end", 10100))
    if not (1024 <= rtp_start <= rtp_end <= 65535):
        raise ConfigurationError("voice RTP range must satisfy 1024 <= rtp_start <= rtp_end <= 65535")
    asterisk_binary = str(voice.get("asterisk_binary", "/usr/sbin/asterisk"))
    if not Path(asterisk_binary).is_absolute():
        raise ConfigurationError("voice.asterisk_binary must be an absolute path")

    database_path = Path(str(service.get("database_path", "/var/lib/ec25toolbox/events.sqlite3")))
    if not database_path.is_absolute():
        database_path = (source.parent / database_path).resolve()

    acme_enabled = bool(acme.get("enabled", False))
    acme_email = str(acme.get("email", ""))
    acme_credentials = Path(str(acme.get("credentials_file", "/etc/ec25toolbox/cloudflare.ini")))
    acme_propagation = int(acme.get("propagation_seconds", 30))
    if acme_enabled:
        if not voice_enabled:
            raise ConfigurationError("acme.enabled=true requires voice.enabled=true")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", acme_email):
            raise ConfigurationError("acme.email must be a valid contact email address")
        if not acme_credentials.is_absolute():
            raise ConfigurationError("acme.credentials_file must be an absolute path")
        if not 10 <= acme_propagation <= 600:
            raise ConfigurationError("acme.propagation_seconds must be between 10 and 600")

    return AppConfig(
        source=source,
        service=ServiceConfig(
            log_level=str(service.get("log_level", "INFO")).upper(),
            database_path=database_path,
            reconnect_seconds=max(0.5, float(service.get("reconnect_seconds", 5))),
            reconcile_seconds=max(10, float(service.get("reconcile_seconds", 300))),
        ),
        modem=ModemConfig(
            port=str(modem.get("port", "auto")),
            baudrate=int(modem.get("baudrate", 115200)),
            candidate_globs=_tuple_of_strings(
                modem.get(
                    "candidate_globs",
                    ["/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"],
                ),
                "modem.candidate_globs",
            ),
            probe_timeout_seconds=max(0.2, float(modem.get("probe_timeout_seconds", 2))),
            command_timeout_seconds=max(0.5, float(modem.get("command_timeout_seconds", 8))),
            exclusive=bool(modem.get("exclusive", True)),
            bind_option_driver=bool(modem.get("bind_option_driver", True)),
            option_driver_ids=_tuple_of_strings(
                modem.get("option_driver_ids", []),
                "modem.option_driver_ids",
            ),
            option_driver_interfaces=tuple(
                int(value) for value in modem.get("option_driver_interfaces", [2, 3])
            ),
        ),
        sms=SMSConfig(
            enabled=bool(sms.get("enabled", True)),
            storages=_tuple_of_strings(sms.get("storages", ["ME", "SM"]), "sms.storages"),
            forward_existing_on_first_start=bool(sms.get("forward_existing_on_first_start", False)),
            delete_after_forward=bool(sms.get("delete_after_forward", False)),
        ),
        calls=CallConfig(
            mode=call_mode,
            reject=bool(calls.get("reject", True)),
            reject_delay_ms=reject_delay_ms,
            send_email=bool(calls.get("send_email", True)),
        ),
        voice=VoiceConfig(
            enabled=voice_enabled,
            modem_name=modem_name,
            data_port=data_port,
            audio_mode=audio_mode,
            audio_port=audio_port,
            alsa_device=alsa_device,
            rx_gain=int(voice.get("rx_gain", 0)),
            tx_gain=int(voice.get("tx_gain", 0)),
            ring_seconds=max(5, min(120, int(voice.get("ring_seconds", 30)))),
            sip_username=sip_username,
            sip_password=sip_password,
            server_name=server_name,
            sip_bind=sip_bind,
            tls_cert_file=tls_cert_file,
            tls_private_key_file=tls_private_key_file,
            external_address=external_address,
            rtp_start=rtp_start,
            rtp_end=rtp_end,
            max_contacts=max(1, min(20, int(voice.get("max_contacts", 5)))),
            codecs=codecs,
            permitted_networks=permitted_networks,
            asterisk_binary=asterisk_binary,
        ),
        portal=portal,
        archive=archive,
        smtp=SMTPConfig(
            enabled=smtp_enabled,
            host=smtp_host,
            port=smtp_port,
            security=security,
            username=smtp_username,
            password=smtp_password,
            from_address=smtp_from_address,
            from_name=str(smtp.get("from_name", "")),
            to_addresses=recipients,
            subject_prefix=str(smtp.get("subject_prefix", "[EC25]")),
            timeout_seconds=max(1, float(smtp.get("timeout_seconds", 20))),
            retry_min_seconds=max(1, float(smtp.get("retry_min_seconds", 30))),
            retry_max_seconds=max(1, float(smtp.get("retry_max_seconds", 3600))),
        ),
        acme=ACMEConfig(
            enabled=acme_enabled,
            email=acme_email,
            credentials_file=acme_credentials,
            propagation_seconds=acme_propagation,
            staging=bool(acme.get("staging", False)),
        ),
    )
