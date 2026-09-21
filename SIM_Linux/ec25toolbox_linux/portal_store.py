"""Append-only outbound requests and audit records, plus paginated history.

Triggers prevent accidental mutation; root/database-file owners remain trusted.
No client operation is mapped to SQL or filesystem access.
"""
import json
import sqlite3
import time
from contextlib import contextmanager


class PortalStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sms_outbox (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portal_audit (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_request ON portal_audit(request_id, id);
                CREATE INDEX IF NOT EXISTS outbox_time ON sms_outbox(created_at);
                CREATE TABLE IF NOT EXISTS portal_logins (
                    id INTEGER PRIMARY KEY, session_key TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL, identity_source TEXT NOT NULL,
                    client_type TEXT NOT NULL, browser TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portal_archives (
                    id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    remote_path TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS archives_no_update BEFORE UPDATE ON portal_archives
                    BEGIN SELECT RAISE(ABORT, 'archive receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS archives_no_delete BEFORE DELETE ON portal_archives
                    BEGIN SELECT RAISE(ABORT, 'archive receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS logins_no_update BEFORE UPDATE ON portal_logins
                    BEGIN SELECT RAISE(ABORT, 'login history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS logins_no_delete BEFORE DELETE ON portal_logins
                    BEGIN SELECT RAISE(ABORT, 'login history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS outbox_no_update BEFORE UPDATE ON sms_outbox
                    BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS outbox_no_delete BEFORE DELETE ON sms_outbox
                    BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON portal_audit
                    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON portal_audit
                    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS messages_no_edit BEFORE UPDATE OF
                    event_id,kind,payload_json,created_at ON events
                    BEGIN SELECT RAISE(ABORT, 'message content is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'messages are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS audit_message_received AFTER INSERT ON events
                    WHEN NEW.kind='sms'
                    BEGIN INSERT INTO portal_audit(request_id,actor,action,detail,created_at)
                    VALUES(NEW.event_id,'gateway','sms_recorded','',NEW.created_at); END;
                CREATE TRIGGER IF NOT EXISTS audit_email_sent AFTER UPDATE OF delivered_at ON events
                    WHEN OLD.delivered_at IS NULL AND NEW.delivered_at IS NOT NULL
                    BEGIN INSERT INTO portal_audit(request_id,actor,action,detail,created_at)
                    VALUES(NEW.event_id,'gateway','smtp_accepted','',NEW.delivered_at); END;
                CREATE TRIGGER IF NOT EXISTS audit_email_failure AFTER UPDATE OF attempts ON events
                    WHEN NEW.attempts > OLD.attempts
                    BEGIN INSERT INTO portal_audit(request_id,actor,action,detail,created_at)
                    VALUES(NEW.event_id,'gateway','smtp_retry_scheduled','',strftime('%s','now')); END;
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _audit(c, request_id, actor, action, detail=""):
        c.execute("INSERT INTO portal_audit(request_id,actor,action,detail,created_at) VALUES(?,?,?,?,?)",
                  (request_id, actor, action, detail, time.time()))

    def enqueue(self, request_id, actor, number, body, hourly, daily):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM sms_outbox WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (existing["actor"], existing["recipient"], existing["body"]) != (actor, number, body):
                    raise ValueError("Idempotency key already belongs to a different request")
                return False
            now = time.time()
            # Global persisted limits also prevent bypass by using several accounts/restarting.
            for window, limit in ((3600, hourly), (86400, daily)):
                if c.execute("SELECT count(*) FROM sms_outbox WHERE created_at>?", (now-window,)).fetchone()[0] >= limit:
                    raise OverflowError("SMS sending limit reached")
            c.execute("INSERT INTO sms_outbox(request_id,actor,recipient,body,created_at) VALUES(?,?,?,?,?)",
                      (request_id, actor, number, body, now))
            self._audit(c, request_id, actor, "queued")
            return True

    def claim(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("""SELECT * FROM sms_outbox o WHERE NOT EXISTS (
                SELECT 1 FROM portal_audit a WHERE a.request_id=o.request_id
                AND a.action IN ('sending','sent','unknown')) ORDER BY id LIMIT 1""").fetchone()
            if row is None:
                return None
            self._audit(c, row["request_id"], "gateway", "sending")
            return dict(row)

    def finish(self, request_id, action, detail=""):
        if action not in {"sent", "unknown"}:
            raise ValueError("Invalid gateway status")
        with self.connect() as c:
            self._audit(c, request_id, "gateway", action, detail)

    def recover(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("""SELECT a.request_id FROM portal_audit a
                WHERE a.action='sending' AND NOT EXISTS (
                SELECT 1 FROM portal_audit b WHERE b.request_id=a.request_id
                AND b.action IN ('sent','unknown'))""").fetchall()
            for row in rows:
                self._audit(c, row[0], "gateway", "unknown", "Gateway restarted; no automatic retry")

    def history(self, direction, before, limit=100):
        with self.connect() as c:
            if direction == "in":
                rows = c.execute("""SELECT rowid AS sequence,* FROM events
                    WHERE kind='sms' AND rowid < ? ORDER BY rowid DESC LIMIT ?""", (before, limit)).fetchall()
                items = []
                for r in rows:
                    p = json.loads(r["payload_json"])
                    items.append(dict(sequence=r["sequence"], id=r["event_id"], direction="in",
                                      number=p.get("sender", "unknown"), body=p.get("body", ""),
                                      created_at=r["created_at"], service_date=p.get("service_date", ""),
                                      email_status="pending" if r["delivered_at"] is None else "processed",
                                      email_error=bool(r["last_error"])))
                return items
            rows = c.execute("""SELECT o.*, (SELECT action FROM portal_audit a
                WHERE a.request_id=o.request_id ORDER BY a.id DESC LIMIT 1) AS status
                FROM sms_outbox o WHERE o.id < ? ORDER BY o.id DESC LIMIT ?""", (before, limit)).fetchall()
            return [dict(sequence=r["id"], id=r["request_id"], direction="out", number=r["recipient"],
                         body=r["body"], created_at=r["created_at"], status=r["status"]) for r in rows]

    def audit(self, before, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM portal_audit WHERE id<? ORDER BY id DESC LIMIT ?",
                                             (before, limit))]

    def record_login(self, session_key, actor, identity_source, client_type, browser):
        with self.connect() as c:
            c.execute("""INSERT OR IGNORE INTO portal_logins
                (session_key,actor,identity_source,client_type,browser,created_at)
                VALUES(?,?,?,?,?,?)""", (session_key, actor, identity_source, client_type, browser, time.time()))

    def logins(self, before, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute("""SELECT id,actor,identity_source,client_type,browser,created_at
                FROM portal_logins WHERE id<? ORDER BY id DESC LIMIT ?""", (before, limit))]

    def record_archive(self, digest, name, remote_path, size):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO portal_archives(sha256,name,remote_path,size,created_at) VALUES(?,?,?,?,?)",
                      (digest, name, remote_path, size, time.time()))

    def archives(self, before=9223372036854775807, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM portal_archives WHERE id<? ORDER BY id DESC LIMIT ?",
                                             (before, limit))]
