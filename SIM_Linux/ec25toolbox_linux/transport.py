from __future__ import annotations

from dataclasses import dataclass, field
from glob import glob
import fcntl
import logging
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import termios
import threading
import time
from typing import Callable

from .config import ModemConfig


LOG = logging.getLogger(__name__)


class ATError(RuntimeError):
    pass


class ATTimeout(ATError):
    pass


@dataclass
class _PendingCommand:
    command: str
    lines: list[str] = field(default_factory=list)
    complete: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    prompt: threading.Event = field(default_factory=threading.Event)
    expects_prompt: bool = False


class SerialATTransport:
    """One-reader AT transport that preserves unsolicited modem events."""

    def __init__(
        self,
        path: str,
        baudrate: int,
        exclusive: bool = True,
        urc_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.baudrate = baudrate
        self.exclusive = exclusive
        self.urc_handler = urc_handler or (lambda _line: None)
        self._fd: int | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.closed = threading.Event()
        self._command_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._pending: _PendingCommand | None = None
        self._write_lock = threading.Lock()

    def open(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if self.exclusive and hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            self._configure(fd)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._stop.clear()
        self.closed.clear()
        self._reader = threading.Thread(target=self._read_loop, name="ec25-at-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        with self._pending_lock:
            if self._pending is not None:
                self._pending.error = "modem disconnected"
                self._pending.complete.set()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        self.closed.set()

    def set_urc_handler(self, handler: Callable[[str], None]) -> None:
        self.urc_handler = handler

    def command(self, command: str, timeout: float = 8) -> list[str]:
        clean = command.strip()
        if not clean or "\r" in clean or "\n" in clean:
            raise ValueError("AT command must be one non-empty line")
        with self._command_lock:
            if self._fd is None:
                raise ATError("modem is not open")
            pending = _PendingCommand(clean)
            with self._pending_lock:
                self._pending = pending
            try:
                self._write((clean + "\r").encode("ascii"))
                if not pending.complete.wait(timeout):
                    raise ATTimeout(f"timed out waiting for {clean}")
                if pending.error is not None:
                    raise ATError(pending.error)
                return pending.lines
            finally:
                with self._pending_lock:
                    if self._pending is pending:
                        self._pending = None

    def _write(self, payload: bytes) -> None:
        with self._write_lock:
            fd = self._fd
            if fd is None:
                raise ATError("modem is not open")
            offset = 0
            while offset < len(payload):
                try:
                    written = os.write(fd, payload[offset:])
                except BlockingIOError:
                    select.select([], [fd], [], 0.25)
                    continue
                if written <= 0:
                    raise ATError("modem write made no progress")
                offset += written

    def send_sms(self, number: str, body: str) -> str:
        from .outbound import encode_submit
        pdu, length = encode_submit(number, body)
        with self._command_lock:
            try:
                self.command("AT+CMGF=0")
                pending = _PendingCommand(f"AT+CMGS={length}", expects_prompt=True)
                with self._pending_lock:
                    self._pending = pending
                self._write((pending.command + "\r").encode("ascii"))
                deadline = time.monotonic() + 10
                while not pending.prompt.wait(0.05):
                    if pending.complete.is_set() or time.monotonic() >= deadline:
                        raise ATError("SMS prompt unavailable")
                if pending.error:
                    raise ATError("SMS prompt rejected")
                self._write(pdu.encode("ascii") + b"\x1a")
                if not pending.complete.wait(60) or pending.error:
                    raise ATError("SMS submission unconfirmed")
                references = [x for x in pending.lines if re.fullmatch(r"\+CMGS: \d+", x)]
                if not references:
                    raise ATError("SMS reference missing")
                self.command("AT+CMGF=1")
                return references[0]
            except Exception:
                # Never retry a possibly accepted SMS. Abort input mode, close,
                # and let the daemon reinitialize before processing another task.
                try:
                    self._write(b"\x1b")
                except Exception:
                    pass
                self.close()
                raise
            finally:
                with self._pending_lock:
                    self._pending = None

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                fd = self._fd
                if fd is None:
                    break
                try:
                    ready, _, _ = select.select([fd], [], [], 0.5)
                except OSError:
                    if self._stop.is_set() or self._fd is None:
                        break
                    raise
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as error:
                    if not self._stop.is_set():
                        LOG.warning("AT port %s failed: %s", self.path, error)
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                self._consume_buffer(buffer)
        finally:
            self.closed.set()
            with self._pending_lock:
                if self._pending is not None:
                    self._pending.error = "modem disconnected"
                    self._pending.complete.set()

    def _consume_buffer(self, buffer: bytearray) -> None:
        while True:
            with self._pending_lock:
                pending = self._pending
                if pending and pending.expects_prompt and not pending.prompt.is_set():
                    trimmed = bytes(buffer).lstrip(b"\r\n ")
                    if trimmed.startswith(b">"):
                        consumed = len(buffer) - len(trimmed) + 1
                        del buffer[:consumed]
                        pending.prompt.set()
            positions = [position for separator in (b"\r", b"\n") if (position := buffer.find(separator)) >= 0]
            if not positions:
                return
            position = min(positions)
            raw = bytes(buffer[:position])
            del buffer[: position + 1]
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if line == "NO CARRIER":
            try:
                self.urc_handler(line)
            except Exception:
                LOG.exception("unsolicited-event handler failed")
            with self._pending_lock:
                pending = self._pending
                if pending is not None and pending.command == "ATH":
                    pending.complete.set()
            return
        if self._is_urc(line):
            try:
                self.urc_handler(line)
            except Exception:
                LOG.exception("unsolicited-event handler failed")
            return
        with self._pending_lock:
            pending = self._pending
            if pending is None:
                LOG.debug("unhandled modem line: %s", line)
                return
            if line == pending.command:
                return
            if line == "OK":
                pending.complete.set()
            elif line == "ERROR" or line.startswith("+CME ERROR:") or line.startswith("+CMS ERROR:"):
                pending.error = line
                pending.complete.set()
            else:
                pending.lines.append(line)

    @staticmethod
    def _is_urc(line: str) -> bool:
        return (
            line == "RING"
            or line.startswith("+CRING:")
            or line.startswith("+CLIP:")
            or line.startswith("+CMTI:")
        )

    def _configure(self, fd: int) -> None:
        attributes = termios.tcgetattr(fd)
        attributes[0] = termios.IGNPAR
        attributes[1] = 0
        attributes[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attributes[3] = 0
        speed_name = f"B{self.baudrate}"
        if not hasattr(termios, speed_name):
            raise ValueError(f"unsupported baud rate: {self.baudrate}")
        speed = getattr(termios, speed_name)
        attributes[4] = speed
        attributes[5] = speed
        attributes[6][termios.VMIN] = 0
        attributes[6][termios.VTIME] = 1
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        termios.tcflush(fd, termios.TCIOFLUSH)


def candidate_ports(config: ModemConfig) -> list[str]:
    if config.port != "auto":
        return [config.port]
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in config.candidate_globs:
        for path in sorted(glob(pattern)):
            try:
                identity = str(Path(path).resolve())
            except OSError:
                identity = path
            if identity not in seen:
                seen.add(identity)
                paths.append(path)
    return paths


def discover_transport(config: ModemConfig) -> SerialATTransport:
    paths = candidate_ports(config)
    if not paths:
        raise ATError("no candidate serial ports were found")
    failures: list[str] = []
    for path in paths:
        transport = SerialATTransport(path, config.baudrate, config.exclusive)
        try:
            transport.open()
            transport.command("AT", timeout=config.probe_timeout_seconds)
            LOG.info("selected AT port %s without requiring a particular USB ID", path)
            return transport
        except Exception as error:
            failures.append(f"{path}: {error}")
            transport.close()
    raise ATError("no candidate port responded to AT (" + "; ".join(failures) + ")")


def bind_configured_option_interfaces(
    config: ModemConfig,
    sysfs_root: Path = Path("/sys/bus/usb"),
    load_module: bool = True,
) -> list[str]:
    """Bind selected interfaces without changing the USB device identity."""
    if not config.bind_option_driver:
        return []
    accepted: set[tuple[str, str]] = set()
    for value in config.option_driver_ids:
        parts = value.lower().split(":", 1)
        if len(parts) != 2 or any(re.fullmatch(r"[0-9a-f]{4}", part) is None for part in parts):
            raise ATError(f"invalid modem.option_driver_ids value: {value}")
        accepted.add((parts[0], parts[1]))
    if not accepted:
        return []

    if load_module:
        modprobe = shutil.which("modprobe")
        if modprobe is not None:
            subprocess.run([modprobe, "option"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    devices = sysfs_root / "devices"
    drivers_probe = sysfs_root / "drivers_probe"
    if not devices.is_dir() or not drivers_probe.exists():
        raise ATError(f"USB sysfs is unavailable below {sysfs_root}")

    bound: list[str] = []
    for device in sorted(devices.iterdir()):
        try:
            vendor = (device / "idVendor").read_text(encoding="ascii").strip().lower()
            product = (device / "idProduct").read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        if (vendor, product) not in accepted:
            continue
        for interface_number in config.option_driver_interfaces:
            for interface in sorted(devices.glob(f"{device.name}:*.{interface_number}")):
                if (interface / "driver").exists():
                    continue
                override = interface / "driver_override"
                if not override.exists():
                    LOG.warning("%s has no driver_override; cannot bind it safely", interface.name)
                    continue
                try:
                    override.write_text("option\n", encoding="ascii")
                    drivers_probe.write_text(interface.name + "\n", encoding="ascii")
                    bound.append(interface.name)
                except OSError as error:
                    raise ATError(f"failed to bind {interface.name} to option driver: {error}") from error
    return bound
