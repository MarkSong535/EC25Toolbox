from __future__ import annotations

from pathlib import Path
import base64
import os
import pty
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ec25toolbox_linux.acme import issue_certificate
from ec25toolbox_linux.config import ConfigurationError, load_config
from ec25toolbox_linux.daemon import EC25LinuxDaemon
from ec25toolbox_linux.mailer import SMTPMailer
from ec25toolbox_linux.sms import decode_ucs2_if_needed, parse_cmgl, parse_cmgr
from ec25toolbox_linux.storage import EventStore
from ec25toolbox_linux.transport import SerialATTransport, bind_configured_option_interfaces
from ec25toolbox_linux.voice import record_agi_event, render_asterisk


CONFIG = """
[service]
database_path = "events.sqlite3"
[modem]
port = "auto"
candidate_globs = ["/dev/ttyUSB*"]
[sms]
storages = ["ME", "SM"]
[calls]
reject = true
[smtp]
enabled = false
to_addresses = []
"""

VOICE_CONFIG = """
[service]
database_path = "events.sqlite3"
[modem]
port = "auto"
candidate_globs = ["/dev/ttyUSB*"]
[sms]
enabled = true
delete_after_forward = true
[calls]
mode = "bridge"
send_email = true
[voice]
enabled = true
data_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0"
audio_mode = "serial"
audio_port = "/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0"
sip_username = "ec25phone"
sip_password = "a-long-random-password-123"
server_name = "phone.example.com"
sip_bind = "0.0.0.0:5061"
tls_cert_file = "/etc/ec25toolbox/tls/fullchain.pem"
tls_private_key_file = "/etc/ec25toolbox/tls/privkey.pem"
permitted_networks = ["10.0.0.0/8"]
[smtp]
enabled = false
to_addresses = []
"""


class ConfigurationTests(unittest.TestCase):
    def test_one_file_loads_all_settings_and_resolves_relative_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG, encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.source, path.resolve())
            self.assertEqual(config.service.database_path, (Path(directory) / "events.sqlite3").resolve())
            self.assertEqual(config.modem.port, "auto")
            self.assertTrue(config.calls.reject)

    def test_enabled_smtp_requires_host_sender_and_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG.replace("enabled = false", "enabled = true"), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_proton_smtp_requires_starttls_token_and_matching_sender(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            proton = CONFIG.replace(
                "enabled = false\nto_addresses = []",
                'enabled = true\nhost = "smtp.protonmail.ch"\nport = 587\nsecurity = "tls"\n'
                'username = "sender@example.com"\npassword = "proton-token-value"\n'
                'from_address = "sender@example.com"\nto_addresses = ["to@example.com"]',
            )
            path.write_text(proton, encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "requires smtp.port=587"):
                load_config(path)

            path.write_text(
                proton.replace('security = "tls"', 'security = "starttls"').replace(
                    'from_address = "sender@example.com"',
                    'from_address = "other@example.com"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "match the address"):
                load_config(path)

            path.write_text(
                proton.replace('security = "tls"', 'security = "starttls"'),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.smtp.host, "smtp.protonmail.ch")
            self.assertEqual(config.smtp.port, 587)
            self.assertEqual(config.smtp.security, "starttls")
    def test_bridge_requires_voice_and_strong_sip_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(VOICE_CONFIG.replace("a-long-random-password-123", "short"), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_voice_cannot_silently_bridge_when_call_mode_is_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(VOICE_CONFIG.replace('mode = "bridge"', 'mode = "reject"'), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_voice_requires_absolute_tls_key_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                VOICE_CONFIG.replace(
                    'tls_private_key_file = "/etc/ec25toolbox/tls/privkey.pem"',
                    'tls_private_key_file = "privkey.pem"',
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_voice_requires_certificate_dns_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                VOICE_CONFIG.replace('server_name = "phone.example.com"', 'server_name = "192.0.2.1"'),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_acme_requires_voice_and_valid_contact_email(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                CONFIG + '\n[acme]\nenabled = true\nemail = "invalid"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(path)


class InstallerTests(unittest.TestCase):
    def test_voice_installer_retries_failed_parallel_build_serially_and_noisily(self) -> None:
        installer = (Path(__file__).resolve().parents[1] / "install-voice.sh").read_text(encoding="utf-8")
        self.assertIn('jobs=${EC25_BUILD_JOBS:-$detected_jobs}', installer)
        self.assertIn('if ! make -j "$jobs"; then', installer)
        self.assertIn("make -j 1 NOISY_BUILD=yes", installer)

    def test_voice_service_allows_serial_lock_and_udev_excludes_only_this_modem(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = (root / "ec25toolbox-voice.service").read_text(encoding="utf-8")
        rules = (root / "99-ec25toolbox.rules").read_text(encoding="utf-8")
        self.assertIn("ReadWritePaths=/var/lib/ec25toolbox /run/ec25toolbox-asterisk /run/lock", service)
        self.assertIn('ATTRS{idVendor}=="2ca3"', rules)
        self.assertIn('ATTRS{idProduct}=="4006"', rules)
        self.assertIn('ENV{ID_MM_DEVICE_IGNORE}="1"', rules)


class ACMETests(unittest.TestCase):
    def test_cloudflare_dns_issue_uses_token_file_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                VOICE_CONFIG.replace('/etc/ec25toolbox/tls/', directory + '/')
                + '\n[acme]\nenabled = true\nemail = "admin@example.com"\n'
                + 'credentials_file = "/etc/ec25toolbox/cloudflare.ini"\n'
                + 'propagation_seconds = 45\n',
                encoding="utf-8",
            )
            config = load_config(path)
            with (
                patch("ec25toolbox_linux.acme._require_root"),
                patch("ec25toolbox_linux.acme._validate_credentials"),
                patch("ec25toolbox_linux.acme.shutil.which", return_value="/usr/bin/certbot"),
                patch("ec25toolbox_linux.acme.subprocess.run") as run,
                patch("ec25toolbox_linux.acme.deploy_certificate") as deploy,
            ):
                issue_certificate(config)
            arguments = run.call_args.args[0]
            self.assertIn("--dns-cloudflare", arguments)
            self.assertIn("/etc/ec25toolbox/cloudflare.ini", arguments)
            self.assertIn("phone.example.com", arguments)
            self.assertIn("45", arguments)
            self.assertNotIn("YOUR_TOKEN", " ".join(arguments))
            deploy.assert_called_once()


class SMSParsingTests(unittest.TestCase):
    def test_ucs2_and_cmgr(self) -> None:
        encoded = "00480065006C006C006F"
        self.assertEqual(decode_ucs2_if_needed(encoded), "Hello")
        message = parse_cmgr(
            ['+CMGR: "REC UNREAD","002B003100320033","","26/08/18,12:00:00-16"', encoded],
            "SM",
            7,
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.sender, "+123")
        self.assertEqual(message.body, "Hello")

    def test_cmgl_multiple_messages(self) -> None:
        messages = parse_cmgl(
            [
                '+CMGL: 1,"REC READ","+100","","26/08/18,12:00:00-16"',
                "first",
                '+CMGL: 2,"REC UNREAD","+200","","26/08/18,12:01:00-16"',
                "second",
            ],
            "ME",
        )
        self.assertEqual([message.index for message in messages], [1, 2])
        self.assertEqual(messages[1].body, "second")


class StorageAndMailerTests(unittest.TestCase):
    def test_event_ids_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.sqlite3")
            self.assertTrue(store.enqueue("same", "sms", {"body": "hello"}))
            self.assertFalse(store.enqueue("same", "sms", {"body": "hello"}))
            self.assertEqual(store.count(), 1)

    def test_starttls_delivery_marks_event_only_after_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "enabled = false\nto_addresses = []",
                    'enabled = true\nhost = "smtp.example.com"\nport = 587\nsecurity = "starttls"\n'
                    'username = "sender@example.com"\npassword = "secret"\n'
                    'from_address = "sender@example.com"\nto_addresses = ["to@example.com"]',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            store = EventStore(config.service.database_path)
            store.enqueue("sms:1", "sms", {"sender": "+1", "body": "hello", "storage": "SM", "index": 1})
            event = store.pending()[0]
            mailer = SMTPMailer(config.smtp, store)
            with patch("ec25toolbox_linux.mailer.smtplib.SMTP") as smtp:
                client = smtp.return_value.__enter__.return_value
                mailer.send(event)
                client.starttls.assert_called_once()
                client.login.assert_called_once_with("sender@example.com", "secret")
                client.send_message.assert_called_once()

    def test_sms_email_subject_is_number_and_body_is_only_sms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG, encoding="utf-8")
            config = load_config(path)
            store = EventStore(config.service.database_path)
            store.enqueue("sms:format", "sms", {"sender": "+15025550123", "body": "exact text"})
            message = SMTPMailer(config.smtp, store)._message(store.pending()[0])
            self.assertEqual(message["Subject"], "+15025550123")
            self.assertEqual(message.get_content().rstrip("\n"), "exact text")

    def test_sender_display_name_preserves_proton_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG.replace(
                "enabled = false\nto_addresses = []",
                'enabled = true\nhost = "smtp.protonmail.ch"\n'
                'username = "noreply@anonymunication.ch"\npassword = "test-token"\n'
                'from_address = "noreply@anonymunication.ch"\nfrom_name = "RPI SMS"\n'
                'to_addresses = ["test@example.com"]',
            ), encoding="utf-8")
            config = load_config(path)
            store = EventStore(Path(directory) / "events.sqlite3")
            store.enqueue("sms:sender", "sms", {"sender": "+1", "body": "test"})
            message = SMTPMailer(config.smtp, store)._message(store.pending()[0])
            self.assertEqual(str(message["From"]), "RPI SMS <noreply@anonymunication.ch>")
            self.assertEqual(message["From"].addresses[0].addr_spec, config.smtp.username)


class VoiceBridgeTests(unittest.TestCase):
    def test_rendered_asterisk_uses_one_modem_owner_for_calls_and_sms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(VOICE_CONFIG, encoding="utf-8")
            output = root / "generated"
            render_asterisk(load_config(config_path), output)
            quectel = (output / "quectel.conf").read_text(encoding="utf-8")
            dialplan = (output / "extensions.conf").read_text(encoding="utf-8")
            self.assertIn(
                "data=/dev/serial/by-id/usb-BAIWANG_Baiwang-if02-port0",
                quectel,
            )
            self.assertIn(
                "audio=/dev/serial/by-id/usb-BAIWANG_Baiwang-if01-port0",
                quectel,
            )
            self.assertIn("disablesms=no", quectel)
            self.assertIn("autodeletesms=yes", quectel)
            self.assertIn("AGI(ec25toolbox-agi,sms,${SMS_BASE64}", dialplan)
            self.assertIn("Dial(Quectel/quectel0/${EXTEN},120)", dialplan)
            pjsip = (output / "pjsip.conf").read_text(encoding="utf-8")
            self.assertIn("protocol=tls", pjsip)
            self.assertIn("bind=0.0.0.0:5061", pjsip)
            self.assertIn("cert_file=/etc/ec25toolbox/tls/fullchain.pem", pjsip)
            self.assertIn("priv_key_file=/etc/ec25toolbox/tls/privkey.pem", pjsip)
            self.assertIn("media_encryption=sdes", pjsip)
            self.assertIn("media_encryption_optimistic=no", pjsip)
            self.assertIn("supported_algorithms_uas=SHA-256", pjsip)
            self.assertNotIn("protocol=udp", pjsip)

    def test_agi_decodes_and_deduplicates_sms_before_smtp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(VOICE_CONFIG, encoding="utf-8")
            config = load_config(config_path)
            body = base64.b64encode("hello from modem".encode()).decode()
            timestamp = base64.b64encode("26/08/18,12:00:00-16".encode()).decode()
            environment = {"agi_callerid": "+15025550123", "agi_uniqueid": "call-1"}
            self.assertTrue(record_agi_event(config, environment, ["sms", body, timestamp]))
            self.assertFalse(record_agi_event(config, environment, ["sms", body, timestamp]))
            store = EventStore(config.service.database_path)
            self.assertEqual(store.count(), 1)


class TransportTests(unittest.TestCase):
    def test_configured_original_usb_id_binds_only_selected_interfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            devices = root / "devices"
            device = devices / "1-2"
            interface = devices / "1-2:1.2"
            other_interface = devices / "1-2:1.4"
            device.mkdir(parents=True)
            interface.mkdir()
            other_interface.mkdir()
            (device / "idVendor").write_text("2ca3\n", encoding="ascii")
            (device / "idProduct").write_text("4006\n", encoding="ascii")
            (interface / "driver_override").write_text("\n", encoding="ascii")
            (other_interface / "driver_override").write_text("\n", encoding="ascii")
            (root / "drivers_probe").write_text("", encoding="ascii")
            config_path = root / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    'candidate_globs = ["/dev/ttyUSB*"]',
                    'candidate_globs = ["/dev/ttyUSB*"]\noption_driver_ids = ["2ca3:4006"]',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            bound = bind_configured_option_interfaces(config.modem, root, load_module=False)
            self.assertEqual(bound, ["1-2:1.2"])
            self.assertEqual((interface / "driver_override").read_text(encoding="ascii"), "option\n")
            self.assertEqual((other_interface / "driver_override").read_text(encoding="ascii"), "\n")

    def test_fragmented_response_and_interleaved_ring(self) -> None:
        master, slave = pty.openpty()
        slave_path = os.ttyname(slave)
        observed: list[str] = []
        transport = SerialATTransport(slave_path, 115200, exclusive=False, urc_handler=observed.append)

        def modem() -> None:
            request = b""
            deadline = time.monotonic() + 2
            while b"\r" not in request and time.monotonic() < deadline:
                request += os.read(master, 128)
            for chunk in (b"\r\nRI", b"NG\r\nA", b"T\r\r\nO", b"K\r\n"):
                os.write(master, chunk)
                time.sleep(0.01)

        thread = threading.Thread(target=modem)
        thread.start()
        try:
            transport.open()
            self.assertEqual(transport.command("AT", timeout=2), [])
            self.assertEqual(observed, ["RING"])
        finally:
            transport.close()
            thread.join(timeout=2)
            os.close(master)
            os.close(slave)


class DaemonBehaviorTests(unittest.TestCase):
    def test_incoming_ring_sends_ath_and_records_email_event(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def command(self, command: str, _timeout: float) -> list[str]:
                self.commands.append(command)
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG + "\n", encoding="utf-8")
            daemon = EC25LinuxDaemon(load_config(path))
            transport = FakeTransport()
            daemon.transport = transport  # type: ignore[assignment]
            daemon._reject_call()
            self.assertEqual(transport.commands, ["ATH"])
            self.assertEqual(daemon.store.count(), 1)

    def test_sms_read_status_change_does_not_duplicate_email(self) -> None:
        from ec25toolbox_linux.sms import SMSMessage

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(CONFIG + "\n", encoding="utf-8")
            daemon = EC25LinuxDaemon(load_config(path))
            daemon.iccid = "8901000000000000001"
            unread = SMSMessage("SM", 4, "REC UNREAD", "+123", "26/08/18,12:00:00-16", "hello")
            read = SMSMessage("SM", 4, "REC READ", "+123", "26/08/18,12:00:00-16", "hello")
            daemon._record_sms(unread, deliver=True)
            daemon._record_sms(read, deliver=True)
            self.assertEqual(daemon.store.count(), 1)


if __name__ == "__main__":
    unittest.main()
