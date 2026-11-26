"""Handling untrusted external input: URL validation (SSRF), text cleaning, guarded fetching."""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings


class UnsafeURL(ValueError):
    pass


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str | None, max_len: int) -> str | None:
    """Strip control characters and cap length. HTML is NOT stripped here: templates always escape output."""
    if value is None:
        return None
    return _CTRL.sub("", str(value)).strip()[:max_len]


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified):
            return False
    return True


def safe_external_url(url: str | None, *, image: bool = False, resolve: bool = False) -> str | None:
    """Accept only http(s) URLs to public hosts. Rejects credentials, odd ports, IP literals in private ranges,
    and (when resolve=True) hostnames that resolve to internal addresses."""
    if not url:
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http(s) links are allowed")
    if image and parts.scheme != "https":
        raise UnsafeURL("Images must use https")
    if parts.username or parts.password:
        raise UnsafeURL("Links with embedded credentials are not allowed")
    host = (parts.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal") or "." not in host:
        raise UnsafeURL("Internal hostnames are not allowed")
    if parts.port not in (None, 80, 443):
        raise UnsafeURL("Non-standard ports are not allowed")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global:
            raise UnsafeURL("Private or reserved IP addresses are not allowed")
    except ValueError as e:
        if isinstance(e, UnsafeURL):
            raise
    if resolve and not _host_is_public(host):
        raise UnsafeURL("Host does not resolve to a public address")
    if len(url) > 1000:
        raise UnsafeURL("URL too long")
    return url


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def fetch_listing_metadata(url: str, client: httpx.Client | None = None) -> dict:
    """User-initiated fetch of one public page; extracts only standard OpenGraph/meta fields.

    Redirects are followed manually so every hop is re-validated. Respects size and time limits.
    Sites that block automated requests simply fail; the UI then falls back to manual entry."""
    cfg = get_settings()
    own = client is None
    client = client or httpx.Client(timeout=cfg.http_timeout_s, follow_redirects=False,
                                    headers={"User-Agent": "DealHunter/0.1 (personal research tool)"})
    try:
        current = safe_external_url(url, resolve=True)
        for _ in range(4):
            with client.stream("GET", current) as r:
                if r.is_redirect:
                    current = safe_external_url(urljoin(current, r.headers.get("location", "")), resolve=True)
                    continue
                r.raise_for_status()
                if "html" not in r.headers.get("content-type", ""):
                    raise ValueError("Not an HTML page")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > cfg.max_fetch_bytes:
                        break
                break
        else:
            raise ValueError("Too many redirects")
    finally:
        if own:
            client.close()
    p = _MetaParser()
    p.feed(body.decode("utf-8", errors="replace"))
    m = p.meta
    out = {"title": clean_text(m.get("og:title") or p.title, 500),
           "description": clean_text(m.get("og:description") or m.get("description"), 5000),
           "image_url": None, "price": m.get("product:price:amount") or m.get("og:price:amount"),
           "currency": m.get("product:price:currency") or m.get("og:price:currency"), "final_url": current}
    try:
        out["image_url"] = safe_external_url(m.get("og:image"), image=True)
    except UnsafeURL:
        pass
    return out
