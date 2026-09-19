"""Runtime configuration, read from the environment.

Every knob has a safe default so the app boots with an empty environment.
Secrets (currently only YOUTUBE_API_KEY) are never written to logs or to any
API response.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_ports(name: str, default: frozenset[int]) -> frozenset[int]:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    ports = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 65535:
            ports.add(int(part))
    return frozenset(ports) or default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


# yt-dlp extractor keys that are allowed to produce a real file download.
#
# The bar for being on this list: the extractor reads a public API or a plainly
# published media URL, and getting the file involves no circumvention of any
# authentication, DRM, CAPTCHA, rate limit or signature scheme.
#
#   ArchiveOrg  - archive.org, public domain / openly licensed collections
#   Wikimedia   - commons.wikimedia.org, freely licensed media
#   PeerTube    - open, self-hosted ActivityPub video instances
#   CCC         - media.ccc.de, Creative Commons conference recordings
#
# Extend it at your own legal discretion with ALLOWED_EXTRACTORS.
DEFAULT_ALLOWED_EXTRACTORS: tuple[str, ...] = (
    "ArchiveOrg",
    "Wikimedia",
    "PeerTube",
    "CCC",
)

# Hosts handled by the official metadata path only. These never reach yt-dlp.
YOUTUBE_HOSTS: frozenset[str] = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})


@dataclass(frozen=True)
class Settings:
    """Immutable view of the process configuration."""

    # --- server ---
    host: str = field(default_factory=lambda: _env_str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000, minimum=1))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CORS_ORIGINS", ()),
    )
    public_base_url: str = field(default_factory=lambda: _env_str("PUBLIC_BASE_URL", ""))

    # --- official YouTube metadata ---
    youtube_api_key: str = field(default_factory=lambda: _env_str("YOUTUBE_API_KEY", ""))

    # --- source policy ---
    allowed_extractors: tuple[str, ...] = field(
        default_factory=lambda: _env_list("ALLOWED_EXTRACTORS", DEFAULT_ALLOWED_EXTRACTORS),
    )
    allow_direct_media_urls: bool = field(
        default_factory=lambda: _env_bool("ALLOW_DIRECT_MEDIA_URLS", True),
    )

    # --- limits ---
    max_download_bytes: int = field(
        default_factory=lambda: _env_int("MAX_DOWNLOAD_BYTES", 1024 * 1024 * 1024, minimum=1),
    )
    max_duration_seconds: int = field(
        default_factory=lambda: _env_int("MAX_DURATION_SECONDS", 4 * 60 * 60, minimum=1),
    )
    max_concurrent_downloads: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENT_DOWNLOADS", 2, minimum=1),
    )
    job_ttl_seconds: int = field(
        default_factory=lambda: _env_int("JOB_TTL_SECONDS", 1800, minimum=60),
    )
    request_timeout_seconds: int = field(
        default_factory=lambda: _env_int("REQUEST_TIMEOUT_SECONDS", 20, minimum=1),
    )

    # --- rate limiting (per client IP, sliding window) ---
    rate_limit_requests: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_REQUESTS", 30, minimum=1),
    )
    rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1),
    )

    # --- storage ---
    download_dir: Path = field(
        default_factory=lambda: Path(
            _env_str("DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "amd-downloads")),
        ),
    )

    # --- networking / SSRF guard ---
    allow_private_addresses: bool = field(
        default_factory=lambda: _env_bool("ALLOW_PRIVATE_ADDRESSES", False),
    )
    # Origins on non-standard ports are refused unless listed here.
    allowed_ports: frozenset[int] = field(
        default_factory=lambda: _env_ports("ALLOWED_PORTS", frozenset({80, 443})),
    )
    trust_forwarded_for: bool = field(
        default_factory=lambda: _env_bool("TRUST_FORWARDED_FOR", False),
    )

    @property
    def has_youtube_api_key(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def allowed_extractor_set(self) -> frozenset[str]:
        return frozenset(self.allowed_extractors)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    settings = Settings()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    return settings
