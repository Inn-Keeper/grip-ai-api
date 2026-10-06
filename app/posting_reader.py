"""Fetches a job posting's visible text for any public https page, guarded
against server side request forgery. Nothing is logged or stored."""

import asyncio
import ipaddress
import json
import socket
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 1_000_000
MAX_TEXT = 20_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 5
TEXT_TYPES = ("text/html", "text/plain")
# zlib window bits: gzip or zlib header auto-detected, and raw deflate.
DECODERS = {"gzip": 47, "deflate": 47, "identity": None, "": None}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


@dataclass
class Posting:
    status: str
    text: str | None = None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ld_json: list[str] = []
        self.skip = 0
        self.in_ld = False

    def handle_starttag(self, tag, attrs):
        self.skip += tag in SKIP_TAGS
        self.in_ld = tag == "script" and ("type", "application/ld+json") in attrs

    def handle_endtag(self, tag):
        self.skip -= tag in SKIP_TAGS and self.skip > 0
        self.in_ld = False

    def handle_data(self, data):
        if self.in_ld:
            self.ld_json.append(data)
        elif not self.skip and data.strip():
            self.parts.append(" ".join(data.split()))


def _job_descriptions(blocks: list[str]) -> list[str]:
    """Descriptions of schema.org JobPosting blocks, which JavaScript-built boards (Ashby) still ship."""
    found = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = (
            data
            if isinstance(data, list)
            else data.get("@graph", [data])
            if isinstance(data, dict)
            else []
        )
        for item in items:
            kind = item.get("@type") if isinstance(item, dict) else None
            if (
                kind == "JobPosting"
                or (isinstance(kind, list) and "JobPosting" in kind)
            ) and isinstance(item.get("description"), str):
                found.append(item["description"])
    return found


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parts = parser.parts + [html_to_text(d) for d in _job_descriptions(parser.ld_json)]
    return "\n".join(p for p in parts if p)[:MAX_TEXT]


async def system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


# IPv6 ranges that embed an IPv4 address (NAT64, IPv4 compatible) and can route to private IPv4.
EMBEDS_IPV4 = [
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "::/96")
]


def is_public(address: str) -> bool:
    # is_global still admits multicast (224.0.0.0/4), so reject it explicitly.
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and any(ip in net for net in EMBEDS_IPV4):
        return False
    return ip.is_global and not ip.is_multicast


class Blocked(Exception):
    pass


class PostingReader:
    def __init__(
        self,
        resolve: Callable[[str], Awaitable[list[str]]] = system_resolve,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.resolve = resolve
        self.transport = transport

    async def read(self, url: str) -> Posting:
        # One deadline for DNS, redirects and the body, so a slow drip cannot hold a slot.
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                return await self._read(url)
        except TimeoutError:
            return Posting("unreachable")

    async def _read(self, url: str) -> Posting:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    host, target = await self._pin(url)
                    # httpx keeps Set-Cookie from a redirect; the next hop must not send it.
                    client.cookies.clear()
                    request = client.build_request(
                        "GET",
                        target,
                        headers={
                            "Host": host,
                            "User-Agent": "GripPostingReader",
                            "Accept": "text/html",
                            "Accept-Encoding": "gzip, deflate",
                        },
                        extensions={"sni_hostname": host},
                    )
                    response = await client.send(request, stream=True)
                    try:
                        if response.is_redirect:
                            url = urljoin(url, response.headers.get("location", ""))
                            continue
                        return await self._body(response)
                    finally:
                        await response.aclose()
                return Posting("unreachable")
        except Blocked:
            return Posting("blocked")
        except (httpx.HTTPError, OSError, zlib.error):
            return Posting("unreachable")

    async def _pin(self, url: str) -> tuple[str, str]:
        """The original host, and the URL rewritten to a checked public IP."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise Blocked from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or port not in (None, 443)
        ):
            raise Blocked
        addresses = await self.resolve(parts.hostname)
        if not addresses or not all(map(is_public, addresses)):
            raise Blocked
        ip = addresses[0]
        netloc = f"[{ip}]" if ":" in ip else ip
        return parts.hostname, parts._replace(netloc=netloc).geturl()

    @staticmethod
    async def _body(response: httpx.Response) -> Posting:
        if response.status_code != 200:
            return Posting("unreachable")
        if not response.headers.get("content-type", "").startswith(TEXT_TYPES):
            return Posting("not_html")
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in DECODERS:
            return Posting("not_html")
        # Raw bytes, inflated here with a cap per chunk, so a gzip bomb never expands past MAX_BYTES.
        inflate = zlib.decompressobj(DECODERS[encoding]) if DECODERS[encoding] else None
        body = bytearray()
        async for chunk in response.aiter_raw():
            if inflate:
                chunk = inflate.decompress(chunk, MAX_BYTES + 1 - len(body))
                if inflate.unconsumed_tail:
                    return Posting("too_large")
            body += chunk
            if len(body) > MAX_BYTES:
                return Posting("too_large")
        text = html_to_text(body.decode(response.encoding or "utf-8", errors="replace"))
        return Posting("ok", text) if text else Posting("empty")
