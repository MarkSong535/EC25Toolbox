"""Portal settings live in the gateway's single config.toml."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PortalConfig:
    enabled: bool = False
    auth_mode: str = "access_jwt"
    origin: str = "https://phone.markso.ng"
    port: int = 9561
    team_domain: str = ""
    audience: str = ""
    allowed_emails: tuple[str, ...] = ()
    sends_per_hour: int = 20
    sends_per_day: int = 100


def parse_portal(value: dict) -> PortalConfig:
    emails = value.get("allowed_emails", [])
    if not isinstance(emails, list) or not all(isinstance(x, str) and "@" in x for x in emails):
        raise ValueError("portal.allowed_emails must be an array of email addresses")
    c = PortalConfig(
        enabled=bool(value.get("enabled", False)),
        auth_mode=str(value.get("auth_mode", "access_jwt")),
        origin=str(value.get("origin", "https://phone.markso.ng")),
        port=int(value.get("port", 9561)),
        team_domain=str(value.get("team_domain", "")),
        audience=str(value.get("audience", "")),
        allowed_emails=tuple(x.lower() for x in emails),
        sends_per_hour=int(value.get("sends_per_hour", 20)),
        sends_per_day=int(value.get("sends_per_day", 100)),
    )
    u = urlsplit(c.origin)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise ValueError("portal.origin must be an HTTPS origin without a trailing slash")
    if not 1024 <= c.port <= 65535 or not 1 <= c.sends_per_hour <= c.sends_per_day <= 1000:
        raise ValueError("invalid portal port or send limits")
    if c.auth_mode not in {"access_jwt", "tunnel"}:
        raise ValueError("portal.auth_mode must be access_jwt or tunnel")
    if c.enabled and c.auth_mode == "access_jwt" and (not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", c.team_domain)
                      or not c.audience or not c.allowed_emails):
        raise ValueError("enabled portal requires team_domain, audience and allowed_emails")
    return c
