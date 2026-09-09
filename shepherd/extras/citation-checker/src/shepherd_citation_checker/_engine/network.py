"""Host-owned HTTP retrieval between bounded model review passes."""

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse


def public_url(url) -> Any:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Retrieval requires a public HTTP(S) URL without credentials")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Retrieval is limited to standard web ports")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private/local source URLs are not permitted")
