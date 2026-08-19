from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


class UnsafeUrlError(ValueError):
    pass


def validate_public_http_url(url: str, *, resolve_dns: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are permitted")
    if not parsed.hostname:
        raise UnsafeUrlError("URL host is required")
    host = parsed.hostname.lower()
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        raise UnsafeUrlError("Localhost and metadata hosts are not permitted")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if resolve_dns:
            for _, _, _, _, sockaddr in socket.getaddrinfo(host, parsed.port or 443):
                ip = ipaddress.ip_address(sockaddr[0])
                if _is_blocked_ip(ip):
                    raise UnsafeUrlError("Resolved private or metadata IP is not permitted")
    else:
        if _is_blocked_ip(ip):
            raise UnsafeUrlError("Private, local, link-local, multicast, and metadata IPs are not permitted")
    return url


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip in BLOCKED_IPS
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
