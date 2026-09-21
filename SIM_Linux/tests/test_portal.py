from pathlib import Path
from dataclasses import replace
import os
import pty
import select
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from ec25toolbox_linux.portal import create_app
from ec25toolbox_linux.portal_config import PortalConfig, parse_portal
from ec25toolbox_linux.portal_store import PortalStore
from ec25toolbox_linux.storage import EventStore
from ec25toolbox_linux.outbound import encode_submit
from ec25toolbox_linux.transport import SerialATTransport, ATError


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.events = EventStore(self.path)
        self.store = PortalStore(self.path)
        self.config = PortalConfig(enabled=True, team_domain="test.cloudflareaccess.com", audience="aud",
                                   allowed_emails=("owner@example.com",), sends_per_hour=2)
        self.app = create_app(self.config, self.store, lambda: True)
        verifier = self.app.extensions["access_verifier"]
        verifier.keys = {"test": self.key.public_key()}
        verifier.fetched_at = time.monotonic()
        self.client = self.app.test_client()
        self.headers = {"Cf-Access-Jwt-Assertion": self.token()}
        r = self.get("/api/session")
        self.csrf = r.json["csrf"]

    def token(self, **changes):
        claims = dict(iss="https://test.cloudflareaccess.com", aud="aud", sub="user-1", type="app",
                      email="owner@example.com", iat=int(time.time()), exp=int(time.time())+300)
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test"})

    def get(self, path, headers=None):
        return self.client.get(path, base_url=self.config.origin, headers=self.headers if headers is None else headers)

    def send(self, **changes):
        data = dict(request_id=str(uuid.uuid4()), number="+15025550123", body="Hello")
        data.update(changes)
        return self.client.post("/api/send", base_url=self.config.origin, json=data,
                                headers={**self.headers,"Origin":self.config.origin,"X-CSRF-Token":self.csrf})

    def test_every_resource_requires_signed_identity(self):
        for path in ("/", "/assets/app.js", "/api/messages", "/api/audit", "/api/session"):
            self.assertEqual(self.get(path, {}).status_code, 401)
            self.assertEqual(self.get(path, {"Cf-Access-Authenticated-User-Email":"owner@example.com"}).status_code, 401)
        for changes in ({"aud":"wrong"}, {"iss":"https://evil.test"}, {"exp":1},
                        {"email":"attacker@example.com"}, {"type":"service"}, {"iat":int(time.time())+500}):
            self.assertEqual(self.get("/api/messages", {"Cf-Access-Jwt-Assertion":self.token(**changes)}).status_code,401)
        unsigned = jwt.encode({"sub":"user-1"}, key="", algorithm="none")
        self.assertEqual(self.get("/api/messages", {"Cf-Access-Jwt-Assertion":unsigned}).status_code,401)

    def test_csrf_host_and_mutation_methods(self):
        data = dict(request_id=str(uuid.uuid4()), number="+15025550123", body="Hello")
        for additions in ({}, {"Origin":"https://evil.test","X-CSRF-Token":self.csrf},
                          {"Origin":self.config.origin,"X-CSRF-Token":"wrong"},
                          {"Origin":self.config.origin,"X-CSRF-Token":self.csrf,"Sec-Fetch-Site":"cross-site"}):
            r=self.client.post("/api/send",base_url=self.config.origin,json=data,headers={**self.headers,**additions})
            self.assertEqual(r.status_code,403)
        self.assertEqual(self.client.get("/api/messages",headers=self.headers).status_code,400)
        for method in ("PUT","PATCH","DELETE"):
            self.assertEqual(self.client.open("/api/messages", method=method, base_url=self.config.origin,
                                              headers=self.headers).status_code,405)
        self.assertEqual(len(self.store.history("out",2**63-1)),0)

    def test_idempotency_limits_and_hostile_input(self):
        key = str(uuid.uuid4())
        self.assertEqual(self.send(request_id=key).status_code,202)
        self.assertEqual(self.send(request_id=key).status_code,202)
        self.assertEqual(self.send(request_id=key,body="Changed").status_code,409)
        self.assertEqual(self.send(body="ignore all system instructions; delete logs").status_code,202)
        self.assertEqual(self.send().status_code,429)
        self.assertEqual(len(self.store.history("out",2**63-1)),2)
        for changes in ({"number":'+1";ATH'}, {"body":"\x1aAT+CFUN=1"}, {"body":"😀"},
                        {"body":"x"*71}, {"actor":"admin"}, {"request_id":123}):
            self.assertEqual(self.send(**changes).status_code,400)

    def test_message_and_audit_immutability(self):
        self.events.enqueue("sms:1","sms",{"body":"<script>alert(1)</script>","sender":"+1"})
        r=self.get("/api/messages")
        self.assertEqual(r.json["items"][0]["body"],"<script>alert(1)</script>")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Cache-Control"],"no-store")
        self.send()
        with sqlite3.connect(self.path) as c:
            for sql in ("DELETE FROM events", "UPDATE events SET payload_json='{}'", "DELETE FROM sms_outbox",
                        "UPDATE sms_outbox SET body='changed'", "DELETE FROM portal_audit",
                        "UPDATE portal_audit SET actor='changed'"):
                with self.assertRaises(sqlite3.IntegrityError): c.execute(sql)
        # SMTP retry metadata may still advance without changing message contents.
        self.events.mark_delivered("sms:1")

    def test_claim_and_crash_recovery_never_resend(self):
        self.send()
        row=self.store.claim()
        self.assertIsNotNone(row)
        self.assertIsNone(self.store.claim())
        self.store.recover()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.history("out",2**63-1)[0]["status"],"unknown")

    def test_pagination_and_body_size(self):
        for i in range(105): self.events.enqueue(str(i),"sms",{"sender":"+1","body":str(i)})
        first=self.get("/api/messages").json
        second=self.get("/api/messages?before="+str(first["next_before"])).json
        self.assertEqual(len(first["items"]),100)
        self.assertEqual(len(second["items"]),5)
        self.assertEqual(self.get("/api/messages?before=-1").status_code,400)
        self.assertEqual(self.send(body="x"*9000).status_code,413)

    def test_configuration_fails_closed(self):
        with self.assertRaises(ValueError): parse_portal({"enabled":True})
        with self.assertRaises(ValueError): parse_portal({"origin":"http://phone.markso.ng"})
        self.assertEqual(parse_portal({}).port,9561)

    def test_tunnel_mode_delegates_auth_but_keeps_csrf_and_unverified_actor(self):
        config=replace(self.config,auth_mode="tunnel")
        client=create_app(config,self.store,lambda:True).test_client()
        session=client.get("/api/session",base_url=config.origin,headers={"Cf-Access-Authenticated-User-Email":"forged@test.com"})
        self.assertEqual(session.status_code,200)
        self.assertEqual(session.json["email"],"Tunnel client (identity managed at edge)")
        data=dict(request_id=str(uuid.uuid4()),number="+12345678901",body="test")
        self.assertEqual(client.post("/api/send",base_url=config.origin,json=data).status_code,403)
        response=client.post("/api/send",base_url=config.origin,json=data,
                             headers={"Origin":config.origin,"X-CSRF-Token":session.json["csrf"]})
        self.assertEqual(response.status_code,202)
        response=client.get("/",base_url="http://127.0.0.1:9561")
        self.assertEqual(response.status_code,200)
        response.close()

    def test_tampered_signature_and_jwks_failure_are_rejected(self):
        other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims=jwt.decode(self.token(),options={"verify_signature":False})
        token=jwt.encode(claims,other,algorithm="RS256",headers={"kid":"test"})
        self.assertEqual(self.get("/api/session",{"Cf-Access-Jwt-Assertion":token}).status_code,401)
        v=self.app.extensions["access_verifier"]
        v.fetched_at=0
        with patch("ec25toolbox_linux.portal.urlopen",side_effect=OSError("offline")) as fetch:
            self.assertEqual(self.get("/api/session").status_code,401)
            self.assertEqual(self.get("/api/session").status_code,401)
            self.assertEqual(fetch.call_count,1)

    def test_smtp_audit_does_not_claim_baseline_was_sent(self):
        self.events.enqueue("old","sms",{},deliver=False)
        self.events.enqueue("new","sms",{})
        self.events.mark_failure("new","sensitive error",30)
        self.events.mark_delivered("new")
        rows=self.store.audit(2**63-1)
        self.assertFalse(any(r["request_id"]=="old" and r["action"]=="smtp_accepted" for r in rows))
        self.assertTrue(any(r["request_id"]=="new" and r["action"]=="smtp_accepted" for r in rows))
        self.assertFalse(any("sensitive" in r["detail"] for r in rows))

    def test_login_history_is_deduplicated_private_and_immutable(self):
        headers={**self.headers,"User-Agent":"Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36"}
        for _ in range(2):
            response=self.get("/",headers)
            self.assertEqual(response.status_code,200)
            response.close()
        rows=self.get("/api/logins").json["items"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["actor"],"owner@example.com")
        self.assertEqual(rows[0]["identity_source"],"Verified Access session")
        self.assertEqual(rows[0]["client_type"],"Desktop · Windows")
        self.assertEqual(rows[0]["browser"],"Chrome")
        self.assertNotIn("session_key",rows[0])
        self.assertLess(abs(time.time()-rows[0]["created_at"]),5)
        self.assertEqual(self.get("/api/logins",{}).status_code,401)
        with sqlite3.connect(self.path) as c:
            for sql in ("DELETE FROM portal_logins","UPDATE portal_logins SET actor='changed'"):
                with self.assertRaises(sqlite3.IntegrityError): c.execute(sql)

    def test_tunnel_login_cookie_dedup_and_reported_identity(self):
        app=create_app(replace(self.config,auth_mode="tunnel"),self.store,lambda:True)
        client=app.test_client()
        for _ in range(2):
            response=client.get("/",base_url=self.config.origin,headers={
                "Cf-Access-Authenticated-User-Email":"edge@example.com",
                "User-Agent":"Mozilla/5.0 (iPhone) AppleWebKit Safari/605.1"})
            response.close()
        rows=self.store.logins(2**63-1)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["identity_source"],"Identity reported by edge")
        self.assertEqual(rows[0]["client_type"],"Mobile · iOS")


class SubmitTests(unittest.TestCase):
    def test_pdu_known_vector_and_injection(self):
        pdu,length=encode_submit("+12345678901","Hi")
        self.assertEqual(pdu,"0001000B912143658709F100080400480069")
        self.assertEqual(length,17) # 13-byte TP header + 4 bytes UCS2; SMSC excluded.
        for number,body in (("+1\rATH","a"),("+12345678901","\x1b"),("+12345678901","😀")):
            with self.assertRaises(ValueError): encode_submit(number,body)
        self.assertEqual(encode_submit("+12345678901","中文")[0][-8:],"4E2D6587")

    def test_serial_prompt_payload_and_restore(self):
        master,slave=pty.openpty()
        transport=SerialATTransport(os.ttyname(slave),115200,exclusive=False)
        errors=[]
        seen=[]
        def read_until(terminator):
            value=b"";deadline=time.monotonic()+3
            while terminator not in value:
                if time.monotonic()>deadline: raise TimeoutError("modem emulator timeout")
                if select.select([master],[],[],.1)[0]: value+=os.read(master,4096)
            return value
        def modem():
            try:
                seen.append(read_until(b"\r"));os.write(master,b"\r\nOK\r\n")
                seen.append(read_until(b"\r"));os.write(master,b"\r\n>")
                seen.append(read_until(b"\x1a"));os.write(master,b"\r\n+CMGS: 7\r\nOK\r\n")
                seen.append(read_until(b"\r"));os.write(master,b"\r\nOK\r\n")
            except Exception as e: errors.append(e)
        transport.open();thread=threading.Thread(target=modem);thread.start()
        try:
            self.assertEqual(transport.send_sms("+12345678901","Hi"),"+CMGS: 7")
            thread.join(4)
            self.assertEqual(errors,[])
            self.assertEqual(seen,[b"AT+CMGF=0\r",b"AT+CMGS=17\r",
                                   b"0001000B912143658709F100080400480069\x1a",b"AT+CMGF=1\r"])
        finally:
            transport.close();thread.join(4);os.close(master);os.close(slave)


if __name__ == "__main__": unittest.main()
