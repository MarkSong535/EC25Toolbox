from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import logging
from pathlib import Path
import queue
import re
import signal
import threading
import time
import uuid

from .config import AppConfig, ConfigurationError, load_config
from .mailer import SMTPMailer
from .sms import SMSMessage, parse_cmgl, parse_cmgr
from .storage import EventStore, PendingEvent
from .transport import ATError, SerialATTransport, bind_configured_option_interfaces, discover_transport
from .voice import check_voice, render_asterisk, run_asterisk


LOG = logging.getLogger(__name__)
_CMTI = re.compile(r'^\+CMTI:\s*"([^"]+)",\s*(\d+)')
_CLIP = re.compile(r'^\+CLIP:\s*"([^"]*)"')


class EC25LinuxDaemon:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = EventStore(config.service.database_path)
        self.stop_event = threading.Event()
        self.transport: SerialATTransport | None = None
        self.iccid = ""
        self._tasks: queue.PriorityQueue[tuple[int, int, str, object]] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._session_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._call_lock = threading.Lock()
        self._call_active = False
        self._caller = ""
        self.mailer = SMTPMailer(config.smtp, self.store, self._email_delivered)
        self.portal_store = None
        self.portal_server = None
        self.modem_ready = False

    def run(self) -> None:
        if self.config.portal and self.config.portal.enabled:
            from .portal_store import PortalStore
            from .portal import start_portal
            self.portal_store = PortalStore(self.store.path)
            self.portal_store.recover()
            self.portal_server = start_portal(self.config.portal, self.portal_store, lambda: self.modem_ready)
        self.mailer.start()
        try:
            # In bridge mode chan_quectel is the sole modem owner. This process
            # only drains durable SMTP events written by the Asterisk AGI hook.
            if self.config.voice.enabled:
                LOG.info("chan_quectel owns call and SMS handling; SMTP delivery worker is ready")
                while not self.stop_event.wait(1):
                    pass
                return
            while not self.stop_event.is_set():
                try:
                    self._run_session()
                except Exception as error:
                    if not self.stop_event.is_set():
                        LOG.error("modem session failed: %s", error)
                finally:
                    self._stop_session()
                self.stop_event.wait(self.config.service.reconnect_seconds)
        finally:
            self._stop_session()
            self.mailer.stop()
            if self.portal_server:
                self.portal_server.close()

    def stop(self) -> None:
        self.stop_event.set()
        self._session_stop.set()
        if self.transport is not None:
            self.transport.close()

    def _run_session(self) -> None:
        transport = discover_transport(self.config.modem)
        self.transport = transport
        transport.set_urc_handler(self._handle_urc)
        self._tasks = queue.PriorityQueue()
        self._session_stop.clear()
        self._worker = threading.Thread(target=self._task_loop, name="ec25-events", daemon=True)
        self._worker.start()
        self._initialize_modem()
        LOG.info("modem ready on %s; ICCID=%s", transport.path, self.iccid or "unknown")

        self._initial_reconciliation()
        self.modem_ready = True
        next_reconciliation = time.monotonic() + self.config.service.reconcile_seconds
        while not self.stop_event.is_set() and not transport.closed.wait(1):
            if self.config.sms.enabled and time.monotonic() >= next_reconciliation:
                self._enqueue(10, "reconcile", None)
                next_reconciliation = time.monotonic() + self.config.service.reconcile_seconds

    def _stop_session(self) -> None:
        self.modem_ready = False
        self._session_stop.set()
        transport, self.transport = self.transport, None
        if transport is not None:
            transport.close()
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None
        with self._call_lock:
            self._call_active = False
            self._caller = ""

    def _initialize_modem(self) -> None:
        transport = self._required_transport()
        timeout = self.config.modem.command_timeout_seconds
        for command in ("AT", "ATE0", "AT+CMEE=2"):
            transport.command(command, timeout)
        try:
            transport.command("AT+CLIP=1", timeout)
        except ATError as error:
            LOG.warning("caller-ID notifications could not be enabled: %s", error)
        if self.config.sms.enabled:
            transport.command("AT+CMGF=1", timeout)
            transport.command('AT+CSCS="UCS2"', timeout)
            try:
                transport.command("AT+CNMI=2,1,0,0,0", timeout)
            except ATError as error:
                LOG.warning("new-SMS notifications unavailable; reconciliation remains active: %s", error)
        try:
            lines = transport.command("AT+QCCID", timeout)
            digits = "".join(character for line in lines for character in line if character.isdigit())
            self.iccid = digits
        except ATError:
            self.iccid = ""

    def _handle_urc(self, line: str) -> None:
        LOG.debug("URC: %s", line)
        match = _CMTI.match(line)
        if match and self.config.sms.enabled:
            self._enqueue(2, "sms", (match.group(1), int(match.group(2))))
            return
        match = _CLIP.match(line)
        if match:
            with self._call_lock:
                self._caller = match.group(1)
            return
        if line == "RING" or line.startswith("+CRING:"):
            if self.config.calls.mode == "bridge":
                return
            with self._call_lock:
                if self._call_active:
                    return
                self._call_active = True
                self._caller = ""
            self._enqueue(0, "call", None)
            return
        if line == "NO CARRIER":
            with self._call_lock:
                self._call_active = False

    def _task_loop(self) -> None:
        while not self._session_stop.is_set():
            try:
                _priority, _sequence, kind, payload = self._tasks.get(timeout=0.5)
            except queue.Empty:
                if self.modem_ready and self.portal_store and not self._call_active:
                    self._send_outbox()
                continue
            if kind == "stop":
                return
            try:
                if kind == "call":
                    self._reject_call()
                elif kind == "sms":
                    storage, index = payload  # type: ignore[misc]
                    self._read_sms(str(storage), int(index))
                elif kind == "reconcile":
                    self._reconcile_sms(deliver=True)
                elif kind == "delete":
                    storage, index = payload  # type: ignore[misc]
                    self._delete_sms(str(storage), int(index))
            except Exception as error:
                LOG.error("%s task failed: %s", kind, error)

    def _send_outbox(self) -> None:
        item = self.portal_store.claim()
        if item is None:
            return
        try:
            reference = self._required_transport().send_sms(item["recipient"], item["body"])
        except Exception:
            self.modem_ready = False
            self.portal_store.finish(item["request_id"], "unknown", "Submission unconfirmed; not retried automatically")
            LOG.warning("Outbound SMS submission unconfirmed; see portal audit")
        else:
            self.portal_store.finish(item["request_id"], "sent", reference)

    def _reject_call(self) -> None:
        delay = self.config.calls.reject_delay_ms / 1000
        if delay:
            self._session_stop.wait(delay)
        with self._call_lock:
            caller = self._caller
        if self.config.calls.mode == "reject" and self.config.calls.reject:
            self._required_transport().command("ATH", self.config.modem.command_timeout_seconds)
            LOG.info("incoming call rejected; caller=%s", caller or "unknown")
        if self.config.calls.send_email:
            payload = {
                "caller": caller,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "iccid": self.iccid,
                "rejected": self.config.calls.mode == "reject" and self.config.calls.reject,
            }
            event_id = "call:" + uuid.uuid4().hex
            if self.store.enqueue(event_id, "call", payload, deliver=self.config.smtp.enabled):
                self.mailer.notify()
        timer = threading.Timer(5, self._clear_call_state)
        timer.daemon = True
        timer.start()

    def _clear_call_state(self) -> None:
        with self._call_lock:
            self._call_active = False
            self._caller = ""

    def _read_sms(self, storage: str, index: int) -> None:
        transport = self._required_transport()
        timeout = self.config.modem.command_timeout_seconds
        transport.command(f'AT+CPMS="{storage}","{storage}","{storage}"', timeout)
        message = parse_cmgr(transport.command(f"AT+CMGR={index}", timeout), storage, index)
        if message is None:
            raise ATError(f"could not parse {storage} SMS index {index}")
        self._record_sms(message, deliver=True)

    def _initial_reconciliation(self) -> None:
        if not self.config.sms.enabled:
            return
        key = "sms-baseline:" + (self.iccid or "unknown")
        first_start = self.store.metadata(key) is None
        deliver = not first_start or self.config.sms.forward_existing_on_first_start
        reconciled = self._reconcile_sms(deliver=deliver)
        if first_start and reconciled:
            self.store.set_metadata(key, datetime.now(timezone.utc).isoformat())

    def _reconcile_sms(self, deliver: bool) -> bool:
        transport = self._required_transport()
        timeout = max(12, self.config.modem.command_timeout_seconds)
        completed = False
        for storage in self.config.sms.storages:
            try:
                transport.command(f'AT+CPMS="{storage}","{storage}","{storage}"', timeout)
                lines = transport.command('AT+CMGL="ALL"', timeout)
                for message in parse_cmgl(lines, storage):
                    self._record_sms(message, deliver=deliver)
                completed = True
            except ATError as error:
                LOG.warning("SMS reconciliation failed for %s: %s", storage, error)
        return completed

    def _record_sms(self, message: SMSMessage, deliver: bool) -> None:
        payload = {
            "storage": message.storage,
            "index": message.index,
            "status": message.status,
            "sender": message.sender,
            "service_date": message.service_date,
            "body": message.body,
            "iccid": self.iccid,
        }
        # Status changes from REC UNREAD to REC READ after AT+CMGR; excluding it
        # from the identity prevents the same stored message from being mailed twice.
        stable_identity = {
            key: payload[key]
            for key in ("storage", "index", "sender", "service_date", "body", "iccid")
        }
        identity = json.dumps(stable_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_id = "sms:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        inserted = self.store.enqueue(event_id, "sms", payload, deliver=deliver and self.config.smtp.enabled)
        if inserted:
            LOG.info(
                "%s SMS from %s (%s index %d)",
                "queued" if deliver else "recorded existing",
                message.sender or "unknown",
                message.storage,
                message.index,
            )
            self.mailer.notify()
        elif self.config.sms.delete_after_forward and self.store.is_delivered(event_id):
            self._enqueue(5, "delete", (message.storage, message.index))

    def _email_delivered(self, event: PendingEvent) -> None:
        if event.kind == "sms" and self.config.sms.delete_after_forward:
            storage = event.payload.get("storage")
            index = event.payload.get("index")
            if isinstance(storage, str) and isinstance(index, int):
                self._enqueue(5, "delete", (storage, index))

    def _delete_sms(self, storage: str, index: int) -> None:
        transport = self._required_transport()
        timeout = self.config.modem.command_timeout_seconds
        transport.command(f'AT+CPMS="{storage}","{storage}","{storage}"', timeout)
        transport.command(f"AT+CMGD={index}", timeout)
        LOG.info("deleted forwarded SMS from %s index %d", storage, index)

    def _enqueue(self, priority: int, kind: str, payload: object) -> None:
        self._tasks.put((priority, next(self._sequence), kind, payload))

    def _required_transport(self) -> SerialATTransport:
        if self.transport is None:
            raise ATError("modem is disconnected")
        return self.transport


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EC25 Linux SMS and incoming-call forwarding service")
    parser.add_argument("--config", default="/etc/ec25toolbox/config.toml", help="single TOML configuration file")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument(
        "--bind-option-driver",
        action="store_true",
        help="bind centrally configured USB interfaces to the Linux option driver and exit",
    )
    parser.add_argument("--render-asterisk", metavar="DIRECTORY", help="generate Asterisk files from central config")
    parser.add_argument("--voice-check", action="store_true", help="check static voice-bridge prerequisites")
    parser.add_argument("--run-asterisk", action="store_true", help="generate configuration and run dedicated Asterisk")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        config = load_config(arguments.config)
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    if arguments.check_config:
        print(f"configuration is valid: {config.source}")
        return 0
    if arguments.bind_option_driver:
        try:
            bound = bind_configured_option_interfaces(config.modem)
        except ATError as error:
            print(f"driver binding error: {error}")
            return 1
        if bound:
            print("bound option-driver interfaces: " + ", ".join(bound))
        return 0
    if arguments.render_asterisk:
        try:
            paths = render_asterisk(config, Path(arguments.render_asterisk))
        except (OSError, ValueError) as error:
            print(f"Asterisk configuration error: {error}")
            return 1
        print("generated Asterisk files: " + ", ".join(str(path) for path in paths))
        return 0
    if arguments.voice_check:
        result = check_voice(config)
        for message in result.messages:
            print(message)
        return 0 if result.ok else 1
    if arguments.run_asterisk:
        try:
            run_asterisk(config)
        except (OSError, ValueError) as error:
            print(f"cannot start Asterisk: {error}")
            return 1
        return 0

    logging.basicConfig(
        level=getattr(logging, config.service.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = EC25LinuxDaemon(config)
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, lambda _signum, _frame: daemon.stop())
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
