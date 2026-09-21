"""Loopback HTTP portal: verified Access JWTs or explicitly edge-managed auth."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid
import re

import jwt
from flask import Flask, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature

from .outbound import encode_submit


def describe_client(user_agent):
    ua = user_agent[:1024]
    device = "Tablet" if "iPad" in ua or ("Android" in ua and "Mobile" not in ua) else (
        "Mobile" if any(x in ua for x in ("iPhone", "Android", "Mobile")) else "Desktop")
    os_name = next((label for marker, label in (("iPhone", "iOS"), ("iPad", "iPadOS"),
                   ("Android", "Android"), ("Windows", "Windows"), ("Macintosh", "macOS"),
                   ("Linux", "Linux")) if marker in ua), "Unknown OS")
    browser = next((label for marker, label in (("Edg/", "Edge"), ("OPR/", "Opera"),
                   ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("Chrome/", "Chrome"),
                   ("CriOS/", "Chrome"), ("Safari/", "Safari")) if marker in ua), "Unknown client")
    return (f"{device} · {os_name}" if os_name != "Unknown OS" else "Unknown device", browser)


class AccessVerifier:
    def __init__(self, config):
        self.config = config
        self.issuer = "https://" + config.team_domain
        self.keys = {}
        self.fetched_at = 0.0
        self.last_attempt = -100.0
        self.lock = threading.Lock()

    def verify(self, token):
        if not token or len(token) > 16384:
            raise ValueError("Missing Access token")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Invalid signing algorithm")
        kid = header["kid"]
        with self.lock:
            now = time.monotonic()
            if now - self.fetched_at > 300 or kid not in self.keys:
                if now - self.last_attempt < 30:
                    raise ValueError("Signing keys unavailable")
                self.last_attempt = now
                # URL comes only from validated admin configuration, never JWT headers.
                with urlopen(self.issuer + "/cdn-cgi/access/certs", timeout=5) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Oversized signing key response")
                keys = jwt.PyJWKSet.from_dict(json.loads(raw)).keys
                self.keys = {k.key_id: k.key for k in keys if k.algorithm_name == "RS256"}
                self.fetched_at = now
            key = self.keys.get(kid)
            if key is None:
                raise ValueError("Unknown signing key")
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.config.audience,
                            issuer=self.issuer, options={"require": ["exp", "iat", "sub", "email", "aud", "iss"]})
        if claims.get("type") != "app" or not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("Human application token required")
        email = claims["email"]
        if not isinstance(email, str) or email.lower() not in self.config.allowed_emails:
            raise ValueError("Identity is not allowed")
        return claims


def create_app(config, store, ready=lambda: False):
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=8192)
    verifier = AccessVerifier(config)
    app.extensions["access_verifier"] = verifier
    secret = secrets.token_bytes(32)
    visit_signer = URLSafeTimedSerializer(secret, salt="portal-visit")
    assets = Path(__file__).with_name("portal_static")

    def csrf(subject):
        return hmac.new(secret, subject.encode(), hashlib.sha256).hexdigest()

    @app.before_request
    def authorize():
        if request.host not in {urlsplit(config.origin).netloc, f"127.0.0.1:{config.port}", f"localhost:{config.port}"}:
            abort(400)
        if config.auth_mode == "tunnel":
            # In this explicit mode, Access is enforced by the operator's edge.
            # Do not present unsigned identity headers as authenticated people.
            g.identity = {"sub": "tunnel", "email": "Tunnel client (identity managed at edge)"}
        else:
            try:
                g.identity = verifier.verify(request.headers.get("Cf-Access-Jwt-Assertion", ""))
            except Exception:
                # Deliberately do not log credentials, JWTs, or attacker-controlled headers.
                abort(401)
        if request.method not in {"GET", "HEAD", "POST"}:
            abort(405)
        if request.method == "POST":
            if request.headers.get("Origin") != config.origin:
                abort(403)
            if request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                abort(403)
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf(g.identity["sub"])):
                abort(403)
            if request.mimetype != "application/json":
                abort(415)

    @app.after_request
    def secure(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Strict-Transport-Security": "max-age=31536000",
        })
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name), error.code

    @app.get("/")
    def index():
        response = send_from_directory(assets, "index.html")
        if request.method == "GET":
            user_agent = request.headers.get("User-Agent", "")[:1024]
            assertion = request.headers.get("Cf-Access-Jwt-Assertion", "")
            # Opaque fingerprint only: never persist a bearer token or decode an
            # unverified token to invent an authenticated identity/time.
            if assertion and len(assertion) <= 16384:
                session_id = assertion
            else:
                cookie = request.cookies.get("__Host-portal-visit", "")
                try:
                    session_id = visit_signer.loads(cookie, max_age=86400)
                    if not isinstance(session_id, str):
                        raise BadSignature("Invalid session")
                except BadSignature:
                    session_id = secrets.token_hex(32)
                    response.set_cookie("__Host-portal-visit", visit_signer.dumps(session_id),
                                        secure=True, httponly=True, samesite="Lax", path="/")
            key = hashlib.sha256((session_id + "\0" + user_agent).encode()).hexdigest()
            if config.auth_mode == "access_jwt":
                actor, source = g.identity["email"], "Verified Access session"
            else:
                reported = request.headers.get("Cf-Access-Authenticated-User-Email", "")
                actor = reported if re.fullmatch(r"[^\s<>@]{1,128}@[^\s<>@]{1,128}", reported) else "Unknown user"
                source = "Identity reported by edge" if actor != "Unknown user" else "Portal visit · identity unavailable"
            client_type, browser = describe_client(user_agent)
            store.record_login(key, actor, source, client_type, browser)
        return response

    @app.get("/assets/<name>")
    def asset(name):
        if name not in {"app.js", "style.css", "layout.css", "theme.css", "apple-touch-icon.png"}:
            abort(404)
        return send_from_directory(assets, name)

    @app.get("/api/session")
    def session():
        return jsonify(email=g.identity["email"], csrf=csrf(g.identity["sub"]),
                       modem_ready=ready(), max_characters=70,
                       sends_per_hour=config.sends_per_hour, sends_per_day=config.sends_per_day)

    def before():
        try:
            value = int(request.args.get("before", 9223372036854775807))
            if not 0 < value <= 9223372036854775807:
                raise ValueError()
            return value
        except ValueError:
            abort(400)

    @app.get("/api/messages")
    def messages():
        direction = request.args.get("direction", "in")
        if direction not in {"in", "out"}:
            abort(400)
        items = store.history(direction, before())
        return jsonify(items=items, next_before=items[-1]["sequence"] if len(items) == 100 else None)

    @app.get("/api/audit")
    def audit():
        items = store.audit(before())
        return jsonify(items=items, next_before=items[-1]["id"] if len(items) == 100 else None)

    @app.get("/api/logins")
    def logins():
        items = store.logins(before())
        return jsonify(items=items, next_before=items[-1]["id"] if len(items) == 100 else None)

    @app.get("/api/archives")
    def archives():
        items = store.archives(before())
        return jsonify(items=items, next_before=items[-1]["id"] if len(items) == 100 else None)

    @app.post("/api/send")
    def send():
        data = request.get_json()
        if not isinstance(data, dict) or set(data) != {"request_id", "number", "body"}:
            abort(400)
        try:
            if str(uuid.UUID(data["request_id"])) != data["request_id"]:
                raise ValueError("Invalid request ID")
            encode_submit(data["number"], data["body"])
        except (ValueError, TypeError, AttributeError):
            return jsonify(error="Use an international number and 1–70 basic Unicode characters; no emoji or controls"), 400
        if not ready():
            return jsonify(error="Modem is offline; nothing was queued"), 503
        try:
            inserted = store.enqueue(data["request_id"], g.identity["email"].lower(), data["number"], data["body"],
                                     config.sends_per_hour, config.sends_per_day)
        except OverflowError:
            return jsonify(error="SMS sending limit reached"), 429
        except ValueError:
            return jsonify(error="Request ID conflict"), 409
        return jsonify(request_id=data["request_id"], status="queued" if inserted else "already accepted"), 202

    return app


def start_portal(config, store, ready):
    from waitress import create_server
    server = create_server(create_app(config, store, ready), host="127.0.0.1", port=config.port,
                           threads=4, connection_limit=32, channel_timeout=30,
                           max_request_body_size=8192, max_request_header_size=32768,
                           clear_untrusted_proxy_headers=True, expose_tracebacks=False)
    threading.Thread(target=server.run, name="ec25-portal", daemon=True).start()
    return server
