"""Canonical Atlassian site identity for portable managed Markdown."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _canonical_percent_path(path: str) -> str:
    if _INVALID_PERCENT_RE.search(path):
        raise ValueError("site-url-invalid-percent")

    def replace(match: re.Match[str]) -> str:
        value = int(match.group(1), 16)
        character = chr(value)
        return character if character in _UNRESERVED else f"%{value:02X}"

    decoded = _PERCENT_RE.sub(replace, path)
    output: list[str] = []
    for segment in decoded.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] != "":
                output.pop()
            continue
        output.append(segment)
    normalized = "/".join(output)
    if normalized and not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return "" if normalized == "/" else normalized


def normalize_site_url(raw: str) -> str:
    """Normalize only the accepted portable Atlassian site URL grammar."""

    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("site-url-control-character")
    if "\\" in raw:
        raise ValueError("site-url-backslash")
    if any(ord(character) > 127 for character in raw.split("/", 3)[2] if "://" in raw):
        raise ValueError("site-url-nonascii-host")
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("site-url-scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("site-url-userinfo")
    if parsed.query:
        raise ValueError("site-url-query")
    if parsed.fragment:
        raise ValueError("site-url-fragment")
    if not parsed.hostname:
        raise ValueError("site-url-host")
    if not parsed.hostname.isascii():
        raise ValueError("site-url-nonascii-host")

    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        host = parsed.hostname.lower()
    else:
        host = f"[{address.compressed}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("site-url-port") from error
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = _canonical_percent_path(parsed.path)
    return urlunsplit(SplitResult(scheme, netloc, path, "", ""))


def site_fingerprint(raw: str) -> str:
    normalized = normalize_site_url(raw)
    digest = hashlib.sha256(b"atls-site-v1\0" + normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = ["normalize_site_url", "site_fingerprint"]
