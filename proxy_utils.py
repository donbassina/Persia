"""Proxy parsing and probing utilities."""

from __future__ import annotations

from urllib.parse import urlparse

import requests

__all__ = ["parse_proxy", "probe_proxy", "ProxyError"]


class ProxyError(Exception):
    """Raised when proxy URL has invalid format."""


def parse_proxy(url: str) -> dict:
    """Return proxy components from *url*.

    Supported schemes: http, https, socks5, socks5h.
    """

    if not url:
        raise ProxyError("empty proxy url")
    info = urlparse(url)
    scheme = info.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ProxyError(f"unsupported scheme: {scheme}")
    if not info.hostname or not info.port:
        raise ProxyError("host or port missing")
    return {
        "scheme": scheme,
        "host": info.hostname,
        "port": info.port,
        "user": info.username,
        "password": info.password,
    }


def _build_requests_proxy(parsed: dict) -> str:
    auth = ""
    if parsed.get("user"):
        auth = parsed["user"]
        if parsed.get("password"):
            auth += f":{parsed['password']}"
        auth += "@"
    return f"{parsed['scheme']}://{auth}{parsed['host']}:{parsed['port']}"


def probe_proxy(parsed: dict, timeout: float = 5.0) -> bool:
    """Return ``True`` if proxy is reachable."""

    proxy = _build_requests_proxy(parsed)
    proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.head("http://example.com", proxies=proxies, timeout=timeout)
        return 200 <= resp.status_code < 400
    except Exception:
        return False
