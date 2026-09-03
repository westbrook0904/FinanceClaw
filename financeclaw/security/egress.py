"""Fail-closed validation for configured outbound HTTP destinations."""

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse


class EgressDenied(ValueError):
    """Raised before a request can target an unapproved destination."""


def _normalized_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _is_private_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Validate URLs without resolving DNS or following attacker-controlled redirects."""

    allowed_hosts: frozenset[str]
    require_https: bool = True
    allow_private_hosts: bool = False

    def validate(self, url: str) -> str:
        parsed = urlparse(url)
        host = _normalized_host(parsed.hostname or "")
        allowed = {_normalized_host(value) for value in self.allowed_hosts}
        if not host or parsed.username or parsed.password:
            raise EgressDenied("outbound URL must contain a hostname and no userinfo")
        if parsed.scheme not in {"http", "https"}:
            raise EgressDenied("outbound URL must use HTTP or HTTPS")
        if self.require_https and parsed.scheme != "https":
            raise EgressDenied("outbound URL must use HTTPS")
        if host not in allowed and not (self.allow_private_hosts and _is_private_host(host)):
            raise EgressDenied(f"outbound host is not allowlisted: {host}")
        return url
