"""Bounded public-page fetching.

The only network access the research layer performs beyond a provider's own
search API. Policy, enforced in code:

* ``http``/``https`` only — anything else is refused before any I/O;
* no private, loopback, link-local, reserved, multicast or unspecified
  address may be contacted (SSRF guard). Per hop the host is resolved **once**
  (IP literal, or every DNS answer via an injectable resolver), every address
  must be public, and the connection is then **pinned** to the validated
  address: the request URL's host is rewritten to that IP literal, the
  ``Host`` header carries the original host, and for https the
  ``sni_hostname`` request extension carries the original hostname so TLS SNI
  and certificate verification still use it. httpx never resolves the name
  itself, so a nameserver that changes its answer between check and connect
  (DNS rebinding) cannot redirect the connection. The robots.txt request and
  the page request use the same pinned address; each redirect hop repeats the
  whole procedure. Not covered: a compromised *public* host, or an upstream
  proxy configured outside this fetcher (none is used here);
* ``robots.txt`` is fetched and honored for our User-Agent token (tiny parser:
  longest-match Allow/Disallow within the matching group, ``*`` fallback);
* one timeout for the whole request, a byte cap on the body (the stream is
  closed at the cap and the page is marked ``truncated``), text/html or
  text/plain only, and text-only extraction (scripts, styles and tags dropped);
* the result is a :class:`RawPage` with mandatory provenance. It is untrusted
  data: callers fence it with ``untrusted_block`` before a model sees it.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from callswarm.models import Provenance, RawPage, SourceType
from callswarm.research.provider import PageFetchRefused

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
TEXT_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
ROBOTS_MAX_BYTES = 64_000
MAX_REDIRECTS = 3
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
PRIVATE_ADDRESS_REASON = "private or non-public address"

Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    """Resolve ``host`` with the event loop's getaddrinfo; every address returned."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return []
    return [str(info[4][0]) for info in infos]


def address_is_public(address: str) -> bool:
    """False for any address that must never be contacted (IPv4 and IPv6,
    including IPv4-mapped IPv6)."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


@dataclass(frozen=True)
class PinnedTarget:
    """A URL whose connection target is fixed to one validated address."""

    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    address: str

    @classmethod
    def build(cls, url: str, address: str) -> PinnedTarget:
        parts = urlsplit(url)
        assert parts.hostname  # checked by check_host_public
        return cls(
            scheme=parts.scheme.lower(),
            host=parts.hostname.lower(),
            port=parts.port,
            path=parts.path or "/",
            query=parts.query,
            address=address,
        )

    @property
    def origin(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{self.host}{port}"

    @property
    def host_header(self) -> str:
        return self.origin.split("://", 1)[1]

    def pinned_url(self, path: str | None = None) -> str:
        """The URL httpx connects to: the validated IP literal in place of the host."""
        literal = f"[{self.address}]" if ":" in self.address else self.address
        port = f":{self.port}" if self.port is not None else ""
        query = f"?{self.query}" if self.query and path is None else ""
        return f"{self.scheme}://{literal}{port}{path or self.path}{query}"

    def headers(self, base: dict[str, str]) -> dict[str, str]:
        return {**base, "Host": self.host_header}

    def extensions(self) -> dict[str, Any]:
        # httpcore reads ``sni_hostname`` for TLS ``server_hostname``; the
        # default ssl context therefore verifies the certificate against the
        # original hostname, not the IP literal.
        return {"sni_hostname": self.host} if self.scheme == "https" else {}


# --- robots.txt --------------------------------------------------------------------


@dataclass
class RobotsRules:
    """Allow/Disallow rules for one user-agent group. Longest match wins."""

    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)

    def permits(self, path: str) -> bool:
        best_len = -1
        best_allow = True
        for rule, allowed in [(r, True) for r in self.allow] + [(r, False) for r in self.disallow]:
            if rule and path.startswith(rule) and len(rule) > best_len:
                best_len, best_allow = len(rule), allowed
        return best_allow


def parse_robots(text: str, user_agent_token: str) -> RobotsRules:
    """Return the rules for ``user_agent_token`` (case-insensitive prefix match),
    falling back to the ``*`` group. Unparseable lines are ignored."""
    token = user_agent_token.lower()
    groups: dict[str, RobotsRules] = {}
    current: list[str] = []
    saw_directive = True
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if saw_directive:
                current = []
                saw_directive = False
            current.append(value.lower())
            for agent in current:
                groups.setdefault(agent, RobotsRules())
            continue
        saw_directive = True
        if key not in ("allow", "disallow"):
            continue
        for agent in current:
            rules = groups.setdefault(agent, RobotsRules())
            (rules.allow if key == "allow" else rules.disallow).append(value)
    for agent, rules in groups.items():
        if agent != "*" and (token.startswith(agent) or agent.startswith(token)):
            return rules
    return groups.get("*", RobotsRules())


# --- text extraction ---------------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def extract_text(body: str, *, content_type: str) -> tuple[str, str]:
    """Return ``(title, text)`` with tags, scripts and styles removed."""
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(body)
        parser.close()
        raw_text = "".join(parser.parts)
        title = parser.title
    else:
        raw_text, title = html.unescape(body), ""
    lines = [" ".join(line.split()) for line in raw_text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return " ".join(title.split()), text


# --- fetcher -----------------------------------------------------------------------


class PublicPageFetcher:
    """Fetches one public page under the policy in the module docstring.

    ``client`` may be injected (tests pass an ``httpx.MockTransport`` client).
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        max_bytes: int,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._client = client
        self._resolver: Resolver = resolver or system_resolver
        self._robots_cache: dict[str, RobotsRules | None] = {}

    @property
    def user_agent_token(self) -> str:
        return self.user_agent.split("/", 1)[0].split(" ", 1)[0]

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "text/html, text/plain;q=0.9"}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds), follow_redirects=False
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @staticmethod
    def check_scheme(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
            raise PageFetchRefused(url, "only http(s) URLs may be fetched")
        if parts.username is not None or parts.password is not None:
            raise PageFetchRefused(url, "credentials in URLs are not allowed")

    async def check_host_public(self, url: str) -> str:
        """SSRF guard: resolve once, refuse anything non-public, return the one
        validated address the connection will be pinned to."""
        host = urlsplit(url).hostname
        if not host:
            raise PageFetchRefused(url, "missing host")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            addresses = await self._resolver(host)
        else:
            addresses = [host]
        if not addresses or not all(address_is_public(a) for a in addresses):
            raise PageFetchRefused(url, PRIVATE_ADDRESS_REASON)
        return addresses[0]

    async def pin(self, url: str) -> PinnedTarget:
        """Validate ``url``'s host and build the pinned request parameters."""
        self.check_scheme(url)
        address = await self.check_host_public(url)
        return PinnedTarget.build(url, address)

    async def _robots_for(self, target: PinnedTarget) -> RobotsRules | None:
        """Rules for the target's origin, fetched over the pinned address;
        ``None`` means the crawl policy is unknown (server error) and the page
        is treated as disallowed."""
        origin = target.origin
        if origin in self._robots_cache:
            return self._robots_cache[origin]
        rules: RobotsRules | None
        try:
            response = await self._get_client().get(
                target.pinned_url("/robots.txt"),
                headers=target.headers(self._headers()),
                extensions=target.extensions(),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.info("robots.txt unavailable for %s: %s", origin, type(exc).__name__)
            rules = None
        else:
            if response.status_code == 200:
                rules = parse_robots(response.text[:ROBOTS_MAX_BYTES], self.user_agent_token)
            elif 400 <= response.status_code < 500:
                rules = RobotsRules()  # no robots file: everything permitted
            else:
                rules = None
        self._robots_cache[origin] = rules
        return rules

    async def fetch(
        self, url: str, *, source_type: SourceType, provider_name: str
    ) -> RawPage | None:
        """Fetch ``url``. Raises :class:`PageFetchRefused` on a policy refusal and
        returns ``None`` on a transport failure, timeout or non-success status.

        Redirects are followed manually (at most ``MAX_REDIRECTS``) so that the
        scheme and robots.txt policy is re-applied to every hop — search
        providers commonly return redirect URIs rather than the page itself.
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            outcome = await self._fetch_once(current, source_type, provider_name)
            if isinstance(outcome, str):
                current = outcome
                continue
            return outcome
        logger.info("fetch of %s exceeded %d redirects", url, MAX_REDIRECTS)
        return None

    async def _fetch_once(
        self, url: str, source_type: SourceType, provider_name: str
    ) -> RawPage | str | None:
        """One hop: a page, a redirect target (``str``) or ``None``."""
        target = await self.pin(url)  # scheme + SSRF check, once per hop
        rules = await self._robots_for(target)
        if rules is None:
            raise PageFetchRefused(url, "robots.txt could not be read; treating as disallowed")
        path = urlsplit(url).path or "/"
        if not rules.permits(path):
            raise PageFetchRefused(url, "disallowed by robots.txt")
        chunks: list[bytes] = []
        received = 0
        truncated = False
        try:
            async with self._get_client().stream(
                "GET",
                target.pinned_url(),
                headers=target.headers(self._headers()),
                extensions=target.extensions(),
                timeout=self.timeout_seconds,
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    return urljoin(url, response.headers["location"])
                if response.status_code != 200:
                    logger.info("fetch of %s returned %s", url, response.status_code)
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith(TEXT_CONTENT_TYPES):
                    raise PageFetchRefused(url, f"unsupported content type {content_type!r}")
                async for chunk in response.aiter_bytes():
                    remaining = self.max_bytes - received
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        received += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException:
            logger.info("fetch of %s timed out", url)
            return None
        except httpx.HTTPError as exc:
            logger.info("fetch of %s failed: %s", url, type(exc).__name__)
            return None
        body = b"".join(chunks).decode(encoding, errors="replace")
        title, text = extract_text(body, content_type=content_type)
        return RawPage(
            url=url,
            title=title,
            text=text,
            truncated=truncated,
            provenance=Provenance(
                source_type=source_type, provider_name=provider_name, source_url=url
            ),
        )
