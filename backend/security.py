"""URL validation, SSRF protection, rate limiting and filename hygiene.

Anything that turns user input into an outbound network request goes through
`validate_remote_url` first, and every redirect hop is re-validated, so a
public hostname cannot bounce us onto a private address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 5

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_REPEATED_DOTS = re.compile(r"\.{2,}")
_WHITESPACE = re.compile(r"\s+")


class UrlRejected(ValueError):
    """Raised when a URL must not be fetched. The message is user-facing."""

    def __init__(self, message: str, *, code: str = "url_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedUrl:
    """A URL that passed every check, plus the addresses it resolved to."""

    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _is_forbidden_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject anything that is not a routable public address."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    # IPv4-mapped / translated IPv6 (::ffff:127.0.0.1, 64:ff9b::/96) can smuggle
    # a private IPv4 target past a naive IPv6 check.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or getattr(ip, "sixtofour", None) or getattr(ip, "teredo", None)
        if isinstance(mapped, tuple):
            mapped = mapped[1]
        if mapped is not None and _is_forbidden_address(mapped):
            return True
        if ip in ipaddress.IPv6Network("64:ff9b::/96"):
            return True
        if ip in ipaddress.IPv6Network("fc00::/7"):  # unique local
            return True
    return False


def _resolve(hostname: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlRejected(
            f"Could not resolve the host '{hostname}'. Check the URL and try again.",
            code="dns_failed",
        ) from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise UrlRejected(
            f"Could not resolve the host '{hostname}'.",
            code="dns_failed",
        )
    return addresses


def validate_remote_url(
    raw_url: str,
    *,
    allow_private: bool = False,
    allowed_ports: frozenset[int] | None = None,
) -> ValidatedUrl:
    """Validate a user-supplied URL for outbound fetching.

    Raises `UrlRejected` with a message safe to show to the user.
    """
    if not raw_url or not raw_url.strip():
        raise UrlRejected("Please enter a URL.", code="url_empty")

    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected("That URL is too long.", code="url_too_long")
    if any(ch in url for ch in ("\n", "\r", "\t")):
        raise UrlRejected("That URL contains invalid characters.", code="url_invalid")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if not scheme:
        raise UrlRejected(
            "The URL needs to start with http:// or https://.",
            code="url_no_scheme",
        )
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected(
            f"'{scheme}' links are not supported. Use an http:// or https:// URL.",
            code="url_bad_scheme",
        )
    if parts.username or parts.password:
        raise UrlRejected(
            "URLs containing credentials are not accepted.",
            code="url_has_credentials",
        )

    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UrlRejected("That URL has no host.", code="url_no_host")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UrlRejected("That URL has an invalid port.", code="url_bad_port") from exc
    permitted_ports = allowed_ports if allowed_ports is not None else DEFAULT_ALLOWED_PORTS
    if port not in permitted_ports:
        allowed = ", ".join(str(candidate) for candidate in sorted(permitted_ports))
        raise UrlRejected(
            f"Port {port} is not allowed. This server accepts port {allowed}.",
            code="url_bad_port",
        )

    if allow_private:
        return ValidatedUrl(url=url, scheme=scheme, hostname=hostname, port=port, addresses=())

    # A literal IP in the URL is checked directly; a name is checked after DNS.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_forbidden_address(literal):
            raise UrlRejected(
                "That address is not publicly routable, so it cannot be fetched.",
                code="url_private_address",
            )
        return ValidatedUrl(
            url=url, scheme=scheme, hostname=hostname, port=port, addresses=(hostname,),
        )

    addresses = _resolve(hostname)
    for address in addresses:
        if _is_forbidden_address(ipaddress.ip_address(address)):
            raise UrlRejected(
                f"'{hostname}' resolves to a non-public address, so it cannot be fetched.",
                code="url_private_address",
            )

    return ValidatedUrl(
        url=url, scheme=scheme, hostname=hostname, port=port, addresses=addresses,
    )


def safe_filename(name: str, *, fallback: str = "download", max_length: int = 120) -> str:
    """Turn an arbitrary title into a filename that is safe on every OS."""
    normalized = unicodedata.normalize("NFKC", name or "")
    cleaned = _UNSAFE_FILENAME_CHARS.sub(" ", normalized)
    cleaned = _REPEATED_DOTS.sub(".", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .")
    # Reserved device names on Windows.
    if cleaned.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


def content_disposition(filename: str) -> str:
    """Build an RFC 6266 Content-Disposition value that survives non-ASCII names."""
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "download"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


class RateLimiter:
    """Sliding-window rate limiter keyed by client identity, in-process."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._hits[key]
            cutoff = now - self._window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                retry_after = max(1, int(bucket[0] + self._window - now) + 1)
                return False, retry_after
            bucket.append(now)
            if len(self._hits) > 10_000:
                self._evict(cutoff)
            return True, 0

    def _evict(self, cutoff: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
