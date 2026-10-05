"""Outbound URL check: public http(s) only, as resolved by DNS."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _public(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            return False
    return bool(addresses)


def validate_public_http_url(url: str, *, max_length: int = 2048) -> str:
    cleaned = (url or "").strip()
    if len(cleaned) > max_length:
        raise ValueError("URL is too long")
    parts = urlparse(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname or not _public(parts.hostname):
        raise ValueError("URL must point to a public HTTP(S) endpoint")
    return cleaned
