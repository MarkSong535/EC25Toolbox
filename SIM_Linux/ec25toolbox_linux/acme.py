from __future__ import annotations

import grp
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time

from .config import AppConfig


DEPLOY_HOOK = "/opt/ec25toolbox/ec25toolbox-acme deploy"


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("ACME issuance and certificate deployment must run as root")


def _validate_credentials(path: Path) -> None:
    try:
        metadata = path.stat()
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"cannot read Cloudflare credentials {path}: {error}") from error
    if metadata.st_uid != 0:
        raise RuntimeError(f"Cloudflare credentials must be owned by root: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"Cloudflare credentials must have mode 0600: {path}")
    token = re.search(r"^\s*dns_cloudflare_api_token\s*=\s*(\S+)\s*$", content, re.MULTILINE)
    if token is None:
        raise RuntimeError("Cloudflare credentials must contain dns_cloudflare_api_token")
    if token.group(1) in {"YOUR_TOKEN", "YOUR_ZONE_SCOPED_TOKEN"} or len(token.group(1)) < 20:
        raise RuntimeError("Cloudflare API token is still a placeholder or is unexpectedly short")
    if "dns_cloudflare_api_key" in content:
        raise RuntimeError("a Cloudflare Global API Key is not accepted; use a zone-scoped API token")


def issue_certificate(config: AppConfig) -> None:
    _require_root()
    if not config.acme.enabled:
        raise RuntimeError("acme.enabled is false")
    certbot = shutil.which("certbot")
    if certbot is None:
        raise RuntimeError("certbot is not installed")
    _validate_credentials(config.acme.credentials_file)
    arguments = [
        certbot,
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        config.acme.email,
        "--dns-cloudflare",
        "--dns-cloudflare-credentials",
        str(config.acme.credentials_file),
        "--dns-cloudflare-propagation-seconds",
        str(config.acme.propagation_seconds),
        "--cert-name",
        config.voice.server_name,
        "--domain",
        config.voice.server_name,
        "--key-type",
        "rsa",
        "--rsa-key-size",
        "3072",
        "--deploy-hook",
        DEPLOY_HOOK,
    ]
    if config.acme.staging:
        arguments.append("--staging")
    subprocess.run(arguments, check=True)
    # A deploy hook does not run when Certbot reuses an existing certificate
    # that is not due for renewal. Populate newly configured destination paths
    # in that case without forcing or duplicating an issuance.
    if not config.voice.tls_cert_file.is_file() or not config.voice.tls_private_key_file.is_file():
        deploy_certificate(config, Path("/etc/letsencrypt/live") / config.voice.server_name)


def renew_certificate(config: AppConfig) -> None:
    _require_root()
    if not config.acme.enabled:
        raise RuntimeError("acme.enabled is false")
    certbot = shutil.which("certbot")
    if certbot is None:
        raise RuntimeError("certbot is not installed")
    _validate_credentials(config.acme.credentials_file)
    subprocess.run(
        [
            certbot,
            "renew",
            "--non-interactive",
            "--cert-name",
            config.voice.server_name,
            "--deploy-hook",
            DEPLOY_HOOK,
        ],
        check=True,
    )


def _validate_certificate(config: AppConfig, certificate: Path, private_key: Path) -> None:
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        decoded = ssl._ssl._test_decode_cert(str(certificate))
        ssl.match_hostname(decoded, config.voice.server_name)
        if ssl.cert_time_to_seconds(decoded["notAfter"]) <= time.time() + 86400:
            raise RuntimeError("issued certificate expires within 24 hours")
    except (KeyError, OSError, ssl.SSLError, ssl.CertificateError, ValueError) as error:
        raise RuntimeError(f"issued certificate failed validation: {error}") from error


def _atomic_install(source: Path, destination: Path, group_id: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(destination.parent, 0, group_id)
    os.chmod(destination.parent, 0o750)
    temporary_name = ""
    try:
        with source.open("rb") as source_handle, tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as destination_handle:
            temporary_name = destination_handle.name
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.chown(temporary_name, 0, group_id)
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, destination)
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def deploy_certificate(config: AppConfig, lineage: str | os.PathLike[str] | None = None) -> None:
    _require_root()
    if not config.acme.enabled:
        raise RuntimeError("acme.enabled is false")
    lineage_path = Path(lineage or os.environ.get("RENEWED_LINEAGE", ""))
    if not lineage_path.is_absolute():
        raise RuntimeError("Certbot did not provide an absolute RENEWED_LINEAGE path")
    source_certificate = lineage_path / "fullchain.pem"
    source_private_key = lineage_path / "privkey.pem"
    _validate_certificate(config, source_certificate, source_private_key)
    group_id = grp.getgrnam("ec25toolbox").gr_gid
    _atomic_install(source_certificate, config.voice.tls_cert_file, group_id)
    _atomic_install(source_private_key, config.voice.tls_private_key_file, group_id)
    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        subprocess.run([systemctl, "try-restart", "ec25toolbox-voice.service"], check=True)
