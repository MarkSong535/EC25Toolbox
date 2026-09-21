from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ArchiveConfig:
    enabled: bool = False
    remote_path: str = "/my-files/RPI-SMS"
    binary: str = "/usr/local/bin/proton-drive"
    interval_hours: int = 24
    max_archive_mb: int = 64


def parse_archive(value):
    c = ArchiveConfig(bool(value.get("enabled", False)), str(value.get("remote_path", "/my-files/RPI-SMS")),
                      str(value.get("binary", "/usr/local/bin/proton-drive")),
                      int(value.get("interval_hours", 24)), int(value.get("max_archive_mb", 64)))
    if not re.fullmatch(r"/my-files/[A-Za-z0-9_-]{1,64}", c.remote_path):
        raise ValueError("archive.remote_path must name one folder below /my-files")
    if not c.binary.startswith("/") or not 1 <= c.interval_hours <= 168 or not 1 <= c.max_archive_mb <= 1024:
        raise ValueError("Invalid archive executable, interval or size limit")
    return c
