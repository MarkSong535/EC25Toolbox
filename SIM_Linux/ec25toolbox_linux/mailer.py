from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr
import logging
import smtplib
import ssl
import threading
from typing import Callable

from .config import SMTPConfig
from .storage import EventStore, PendingEvent


LOG = logging.getLogger(__name__)


class SMTPMailer:
    def __init__(
        self,
        config: SMTPConfig,
        store: EventStore,
        delivered_handler: Callable[[PendingEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.delivered_handler = delivered_handler
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ec25-smtp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def notify(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            events = self.store.pending()
            if not events:
                self._wake.wait(2)
                self._wake.clear()
                continue
            for event in events:
                if self._stop.is_set():
                    return
                try:
                    self.send(event)
                    self.store.mark_delivered(event.event_id)
                    LOG.info("SMTP delivery completed for %s", event.event_id)
                    if self.delivered_handler is not None:
                        self.delivered_handler(event)
                except Exception as error:
                    delay = min(
                        self.config.retry_max_seconds,
                        self.config.retry_min_seconds * (2 ** min(event.attempts, 10)),
                    )
                    self.store.mark_failure(event.event_id, str(error), delay)
                    LOG.error("SMTP delivery failed for %s; retrying in %.0fs: %s", event.event_id, delay, error)

    def send(self, event: PendingEvent) -> None:
        message = self._message(event)
        context = ssl.create_default_context()
        if self.config.security == "tls":
            with smtplib.SMTP_SSL(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout_seconds,
                context=context,
            ) as client:
                self._authenticate_and_send(client, message)
            return

        with smtplib.SMTP(self.config.host, self.config.port, timeout=self.config.timeout_seconds) as client:
            client.ehlo()
            if self.config.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            self._authenticate_and_send(client, message)

    def _authenticate_and_send(self, client: smtplib.SMTP, message: EmailMessage) -> None:
        if self.config.username:
            client.login(self.config.username, self.config.password)
        client.send_message(message)

    def _message(self, event: PendingEvent) -> EmailMessage:
        payload = event.payload
        message = EmailMessage()
        message["From"] = formataddr((self.config.from_name, self.config.from_address))
        message["To"] = ", ".join(self.config.to_addresses)
        if event.kind == "sms":
            sender = payload.get("sender") or "unknown sender"
            # Keep forwarded texts usable in an email client: the sender number
            # is the subject and the original SMS is the entire message body.
            message["Subject"] = str(sender)
            body = str(payload.get("body", ""))
        else:
            caller = payload.get("caller") or "unknown caller"
            disposition = str(payload.get("disposition") or "").upper()
            action = "Rejected call" if payload.get("rejected") else "Incoming call"
            if disposition:
                action = f"Call {disposition.lower()}"
            message["Subject"] = f"{self.config.subject_prefix} {action} from {caller}"
            body = (
                f"{action}.\n\n"
                f"Caller: {caller}\n"
                f"Observed at: {payload.get('observed_at')}\n"
                f"SIM ICCID: {payload.get('iccid') or 'unknown'}\n"
            )
        message.set_content(body)
        return message
