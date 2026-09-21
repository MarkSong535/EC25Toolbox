"""Upload-only Proton archives. No sync, remote deletion, or live DB pruning."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid

from .config import load_config
from .mailer import SMTPMailer
from .portal_store import PortalStore
from .storage import PendingEvent


class ArchiveError(RuntimeError):
    pass


def digest_file(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class ProtonClient:
    def __init__(self, config):
        self.config = config

    def run(self, *arguments):
        env = dict(os.environ)
        env.update(PROTON_DRIVE_CREDENTIALS_STORE="pass", PROTON_DRIVE_LOG_LEVEL="ERROR")
        # Secrets remain in the service user's GPG-protected pass store.
        result = subprocess.run([self.config.archive.binary, *arguments], cwd=self.config.service.database_path.parent,
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=300, check=False)
        if result.returncode:
            raise ArchiveError("Proton operation failed: check sign-in, unlocked credential store and remote folder")

    def upload(self, path):
        self.run("filesystem", "upload", "-f", "skip", "-d", "merge", "-t", str(path), self.config.archive.remote_path)

    def download(self, name, directory):
        if not re.fullmatch(r"sms-[0-9TZ-]+-[0-9a-f]{32}\.tar\.gz", name):
            raise ArchiveError("Invalid archive filename")
        self.run("filesystem", "download", "-f", "skip", "-d", "merge",
                 self.config.archive.remote_path + "/" + name, str(directory))
        return Path(directory) / name


@contextmanager
def archive_lock(config):
    with open(config.service.database_path.parent / ".archive.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ArchiveError("Another archive operation is in progress") from error
        yield


def build_bundle(config, directory):
    root = Path(directory)
    source = config.service.database_path
    limit = config.archive.max_archive_mb * 1024 * 1024
    if source.stat().st_size > limit:
        raise ArchiveError("Database exceeds configured archive limit; increase it only after checking free space")
    if shutil.disk_usage(root).free < source.stat().st_size * 6 + 32 * 1024 * 1024:
        raise ArchiveError("Not enough temporary space to safely build and verify an archive")
    snapshot = root / "gateway.sqlite3"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(snapshot) as dst:
            src.backup(dst)
    name = "sms-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".tar.gz"
    bundle = root / name
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "database_sha256": digest_file(snapshot),
                "emails": "Reconstructed from gateway events; not an export of the Proton mailbox or original SMTP bytes",
                "scope": "Gateway messages, email delivery state, outgoing requests, access/activity logs and archive receipts",
                "files": {}}
    mailer = SMTPMailer(config.smtp, None)
    with sqlite3.connect(snapshot) as db, tarfile.open(bundle, "w:gz") as tar:
        def add_bytes(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        tar.add(snapshot, arcname="gateway.sqlite3", recursive=False)
        manifest["files"]["gateway.sqlite3"] = manifest["database_sha256"]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("events", "sms_outbox", "portal_audit", "portal_logins", "portal_archives"):
            if table not in tables:
                continue
            rows = db.execute("SELECT * FROM " + table) # fixed allowlisted table identifiers only
            names = [x[0] for x in rows.description]
            export = root / (table + ".jsonl")
            with export.open("w", encoding="utf-8") as output:
                for row in rows:
                    output.write(json.dumps(dict(zip(names, row)), ensure_ascii=True) + "\n")
            tar.add(export, arcname=export.name, recursive=False)
            manifest["files"][export.name] = digest_file(export)
        for event_id, kind, payload, created in db.execute("SELECT event_id,kind,payload_json,created_at FROM events"):
            if kind not in {"sms", "call"}:
                continue
            message = mailer._message(PendingEvent(event_id, kind, json.loads(payload), 0))
            message["Date"] = format_datetime(datetime.fromtimestamp(created, timezone.utc))
            add_bytes("emails/" + hashlib.sha256(event_id.encode()).hexdigest() + ".eml", message.as_bytes())
        add_bytes("manifest.json", json.dumps(manifest, indent=2).encode())
    if bundle.stat().st_size > limit:
        raise ArchiveError("Compressed archive exceeds configured size limit")
    return bundle


def run_archive(config, client=None, force=False):
    if not config.archive.enabled:
        return "Archiving disabled; no upload performed"
    client = client or ProtonClient(config)
    store = PortalStore(config.service.database_path)
    with archive_lock(config):
        previous = store.archives(limit=1)
        if not force and previous and time.time() - previous[0]["created_at"] < config.archive.interval_hours * 3600:
            return "Archive not due"
        # Only temporary export copies are removed. The live database is never pruned.
        with tempfile.TemporaryDirectory(prefix="archive-", dir=config.service.database_path.parent) as directory:
            bundle = build_bundle(config, directory)
            digest = digest_file(bundle)
            client.upload(bundle)
            check = Path(directory) / "verify"
            check.mkdir()
            recovered = client.download(bundle.name, check)
            if not recovered.is_file() or digest_file(recovered) != digest:
                raise ArchiveError("Remote round-trip checksum failed; no verified receipt recorded")
            store.record_archive(digest, bundle.name, config.archive.remote_path, bundle.stat().st_size)
            return "Uploaded and download-verified " + bundle.name


def recover_archive(config, name, expected_digest, output, client=None):
    # Explicit user/operator download only, never part of a bidirectional sync.
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ArchiveError("A SHA-256 receipt is required")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / name
    client = client or ProtonClient(config)
    if not re.fullmatch(r"sms-[0-9TZ-]+-[0-9a-f]{32}\.tar\.gz", name) or destination.exists():
        raise ArchiveError("Invalid name or destination already exists")
    with archive_lock(config), tempfile.TemporaryDirectory(prefix="recover-", dir=output) as temporary:
        downloaded = client.download(name, temporary)
        if digest_file(downloaded) != expected_digest:
            raise ArchiveError("Downloaded archive checksum mismatch")
        # Exclusive destination creation prevents clobbering an existing recovery.
        with destination.open("xb") as dst, downloaded.open("rb") as src:
            shutil.copyfileobj(src, dst)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/ec25toolbox/config.toml")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--recover", metavar="ARCHIVE_NAME")
    parser.add_argument("--sha256")
    parser.add_argument("--output")
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        if args.recover:
            if not args.sha256 or not args.output:
                parser.error("--recover requires --sha256 and --output")
            print(recover_archive(config, args.recover, args.sha256, args.output))
        else:
            print(run_archive(config, force=args.force))
    except (ArchiveError, OSError, subprocess.TimeoutExpired) as error:
        print("Archive unavailable:", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
