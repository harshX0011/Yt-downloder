"""Source policy: what this app will and will not download.

The policy is deliberately a *allowlist*. A URL only becomes downloadable if it
is either a plainly published media file or handled by a yt-dlp extractor that
appears in `Settings.allowed_extractors`.

YouTube is routed to the official-metadata path before any extraction happens,
because YouTube exposes no authorized mechanism for downloading a video file
and obtaining one anyway requires defeating its signature, rate-limit and
bot-detection measures. See README.md for the full reasoning.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlsplit

from .config import YOUTUBE_HOSTS, Settings

# Container extensions we are willing to serve as a direct file download.
DIRECT_MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ogv", ".avi",
    ".m4a", ".mp3", ".ogg", ".oga", ".opus", ".flac", ".wav",
})

DIRECT_MEDIA_CONTENT_TYPES = ("video/", "audio/", "application/octet-stream")

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class SourceKind(str, Enum):
    """How a URL will be handled."""

    YOUTUBE = "youtube"
    DIRECT_MEDIA = "direct_media"
    EXTRACTOR = "extractor"


@dataclass(frozen=True)
class Classification:
    """The routing decision for a single URL."""

    kind: SourceKind
    hostname: str
    youtube_video_id: str | None = None


def extract_youtube_video_id(url: str) -> str | None:
    """Pull the 11-character video id out of any common YouTube URL shape."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or ""

    if host in {"youtu.be", "www.youtu.be"}:
        candidate = path.lstrip("/").split("/")[0]
        return candidate if _YOUTUBE_ID.match(candidate) else None

    if host not in YOUTUBE_HOSTS:
        return None

    query_id = parse_qs(parts.query).get("v", [""])[0]
    if _YOUTUBE_ID.match(query_id):
        return query_id

    # /embed/ID, /shorts/ID, /live/ID, /v/ID
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2 and segments[0] in {"embed", "shorts", "live", "v"}:
        candidate = segments[1]
        if _YOUTUBE_ID.match(candidate):
            return candidate

    return None


def is_youtube_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host in YOUTUBE_HOSTS


def looks_like_direct_media(url: str) -> bool:
    """True when the URL path itself names a media container."""
    path = urlsplit(url).path or ""
    extension = posixpath.splitext(path)[1].lower()
    return extension in DIRECT_MEDIA_EXTENSIONS


def classify(url: str, settings: Settings) -> Classification:
    """Decide how a URL should be handled. Does not touch the network."""
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")

    if hostname in YOUTUBE_HOSTS:
        return Classification(
            kind=SourceKind.YOUTUBE,
            hostname=hostname,
            youtube_video_id=extract_youtube_video_id(url),
        )

    if settings.allow_direct_media_urls and looks_like_direct_media(url):
        return Classification(kind=SourceKind.DIRECT_MEDIA, hostname=hostname)

    return Classification(kind=SourceKind.EXTRACTOR, hostname=hostname)


def is_extractor_allowed(extractor_key: str, settings: Settings) -> bool:
    """Whether a yt-dlp extractor may produce a file download."""
    return extractor_key in settings.allowed_extractor_set


def allowed_sources_summary(settings: Settings) -> list[dict[str, str]]:
    """Human-readable description of what the deployment will download."""
    known = {
        "ArchiveOrg": (
            "Internet Archive",
            "archive.org",
            "Public domain and openly licensed audio and video collections.",
        ),
        "Wikimedia": (
            "Wikimedia Commons",
            "commons.wikimedia.org",
            "Freely licensed media from the Wikimedia projects.",
        ),
        "PeerTube": (
            "PeerTube",
            "any PeerTube instance",
            "Open, self-hosted federated video instances.",
        ),
        "CCC": (
            "media.ccc.de",
            "media.ccc.de",
            "Creative Commons licensed conference recordings.",
        ),
    }

    summary: list[dict[str, str]] = []
    for key in settings.allowed_extractors:
        name, host, description = known.get(
            key, (key, "see yt-dlp extractor list", "Enabled through ALLOWED_EXTRACTORS."),
        )
        summary.append({"key": key, "name": name, "host": host, "description": description})

    if settings.allow_direct_media_urls:
        summary.append({
            "key": "DirectMedia",
            "name": "Direct media URL",
            "host": "any public https:// host",
            "description": (
                "A link that points straight at a media file, for example "
                "https://example.org/talk.mp4"
            ),
        })

    return summary
