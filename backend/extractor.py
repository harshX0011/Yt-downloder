"""Metadata probing for authorized sources.

Two probes live here:

* `probe_extractor_source` runs yt-dlp in metadata-only mode and refuses to
  return anything unless the extractor that handled the URL is on the
  allowlist. Protection-defeating behaviour is switched off explicitly:
  no cookies, no browser cookie import, and `geo_bypass=False`.
* `probe_direct_media` issues a redirect-checked HEAD (falling back to a
  ranged GET) against a URL that already names a media file.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import httpx

from .config import Settings
from .models import MediaFormat
from .security import MAX_REDIRECTS, UrlRejected, validate_remote_url
from .sources import (
    DIRECT_MEDIA_CONTENT_TYPES,
    is_extractor_allowed,
)

_FILENAME_FROM_DISPOSITION = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


class ExtractionError(Exception):
    """Raised when a source cannot be probed. The message is user-facing."""

    def __init__(self, message: str, *, code: str = "extraction_failed") -> None:
        super().__init__(message)
        self.code = code


class SourceNotAllowed(ExtractionError):
    """Raised when a URL resolves to a source outside the allowlist."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="source_not_allowed")


@dataclass
class ProbeResult:
    """Normalized metadata for a downloadable source."""

    title: str | None
    uploader: str | None
    uploader_url: str | None
    duration_seconds: int | None
    thumbnail_url: str | None
    description: str | None
    upload_date: str | None
    view_count: int | None
    license_name: str | None
    webpage_url: str | None
    source_name: str
    metadata_source: str
    formats: list[MediaFormat] = field(default_factory=list)
    recommended_format_id: str | None = None
    total_bytes: int | None = None


def ffmpeg_available() -> bool:
    """Whether streams can be merged and remuxed into MP4."""
    return shutil.which("ffmpeg") is not None


def build_ytdlp_options(settings: Settings) -> dict:
    """Base yt-dlp options shared by probing and downloading.

    `allowed_extractors` is a second line of defence behind `sources.classify`:
    even if a URL slipped through, yt-dlp itself will refuse to handle it with
    anything other than an allowlisted extractor.
    """
    patterns = [f"^{re.escape(key)}$" for key in settings.allowed_extractors]
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": settings.request_timeout_seconds,
        "retries": 2,
        "fragment_retries": 2,
        "allowed_extractors": patterns,
        # Do not work around any access control.
        "geo_bypass": False,
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "nocheckcertificate": False,
        "age_limit": None,
        "call_home": False,
    }


def _format_entries(info: dict, *, allow_merge: bool) -> tuple[list[MediaFormat], str | None]:
    """Turn yt-dlp's format list into our schema and pick a recommendation."""
    raw_formats = [f for f in (info.get("formats") or []) if f.get("url")]

    # A single-URL result (many archive.org items) has no format list.
    if not raw_formats and info.get("url"):
        raw_formats = [{
            "format_id": info.get("format_id") or "source",
            "url": info["url"],
            "ext": info.get("ext"),
            "vcodec": info.get("vcodec"),
            "acodec": info.get("acodec"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
        }]

    entries: list[MediaFormat] = []
    for raw in raw_formats:
        vcodec = (raw.get("vcodec") or "").lower()
        acodec = (raw.get("acodec") or "").lower()
        has_video = bool(vcodec) and vcodec != "none"
        has_audio = bool(acodec) and acodec != "none"
        if not has_video and not has_audio:
            continue
        # Manifest-only entries cannot be served as a plain file.
        streaming_protocols = {"m3u8", "m3u8_native", "dash", "http_dash_segments"}
        if not allow_merge and raw.get("protocol") in streaming_protocols:
            continue

        height = raw.get("height")
        quality = raw.get("format_note") or (f"{height}p" if height else None)
        if not has_video and has_audio:
            quality = quality or "audio only"

        entries.append(MediaFormat(
            format_id=str(raw.get("format_id") or "source"),
            container=raw.get("ext"),
            quality_label=quality,
            width=raw.get("width"),
            height=height,
            fps=raw.get("fps"),
            filesize_bytes=raw.get("filesize") or raw.get("filesize_approx"),
            video_codec=raw.get("vcodec") if has_video else None,
            audio_codec=raw.get("acodec") if has_audio else None,
            has_audio=has_audio,
            has_video=has_video,
            is_progressive=has_video and has_audio,
            note=(
                None if (has_video and has_audio)
                else "Needs merging with a separate track (ffmpeg required)."
            ),
        ))

    if not entries:
        return [], None

    def sort_key(entry: MediaFormat) -> tuple:
        return (
            entry.container == "mp4",
            entry.is_progressive,
            entry.height or 0,
            entry.filesize_bytes or 0,
        )

    entries.sort(key=sort_key, reverse=True)

    usable = entries if allow_merge else [e for e in entries if e.is_progressive]
    mp4_progressive = [e for e in usable if e.container == "mp4" and e.is_progressive]
    progressive = [e for e in usable if e.is_progressive]
    recommended = (mp4_progressive or progressive or usable or [None])[0]

    return entries, (recommended.format_id if recommended else None)


def _extract_sync(url: str, settings: Settings) -> dict:
    """Blocking yt-dlp metadata extraction. Runs in a worker thread."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError

    options = build_ytdlp_options(settings) | {"skip_download": True, "simulate": True}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except UnsupportedError as exc:
        raise SourceNotAllowed(
            "This app does not support that site. Supported sources are listed below.",
        ) from exc
    except (DownloadError, ExtractorError) as exc:
        message = str(exc)
        allowlist_markers = ("not in the allowlist", "--ies", "allowed_extractors")
        if any(marker in message for marker in allowlist_markers):
            raise SourceNotAllowed(
                "That site is not on this deployment's authorized source list.",
            ) from exc
        raise ExtractionError(
            "Could not read that page. It may be private, region-locked, removed, "
            "or it may require a sign-in, which this app does not attempt to work around.",
            code="extraction_failed",
        ) from exc

    if not info:
        raise ExtractionError("That page returned no media.", code="extraction_empty")

    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise ExtractionError(
                "That link is a playlist with no playable entries. "
                "Paste a link to a single item instead.",
                code="playlist_unsupported",
            )
        info = entries[0]

    return info


async def probe_extractor_source(url: str, settings: Settings) -> ProbeResult:
    """Probe an allowlisted site for real metadata and downloadable formats."""
    info = await asyncio.to_thread(_extract_sync, url, settings)

    extractor_key = info.get("extractor_key") or info.get("ie_key") or ""
    if not is_extractor_allowed(extractor_key, settings):
        raise SourceNotAllowed(
            "That site is not on this deployment's authorized source list, so no "
            "download was attempted. Supported sources are listed below.",
        )

    duration = info.get("duration")
    duration_seconds = (
        int(duration) if isinstance(duration, (int, float)) and duration > 0 else None
    )
    if duration_seconds and duration_seconds > settings.max_duration_seconds:
        raise ExtractionError(
            f"That item runs {duration_seconds // 60} minutes, which is over this "
            f"server's {settings.max_duration_seconds // 60} minute limit.",
            code="too_long",
        )

    allow_merge = ffmpeg_available()
    formats, recommended = _format_entries(info, allow_merge=allow_merge)
    if not formats:
        raise ExtractionError(
            "No downloadable rendition was published for that item."
            + ("" if allow_merge else " ffmpeg is not installed, so split audio and "
               "video streams cannot be combined on this server."),
            code="no_formats",
        )

    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return ProbeResult(
        title=info.get("title"),
        uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
        uploader_url=info.get("uploader_url") or info.get("channel_url"),
        duration_seconds=duration_seconds,
        thumbnail_url=info.get("thumbnail"),
        description=info.get("description"),
        upload_date=upload_date if isinstance(upload_date, str) else None,
        view_count=info.get("view_count"),
        license_name=info.get("license"),
        webpage_url=info.get("webpage_url") or url,
        source_name=info.get("extractor") or extractor_key,
        metadata_source=f"yt-dlp {extractor_key} extractor",
        formats=formats,
        recommended_format_id=recommended,
    )


def _filename_from_url(url: str) -> str | None:
    name = posixpath.basename(urlsplit(url).path or "")
    return unquote(name) or None


async def _head_with_redirect_checks(url: str, settings: Settings) -> httpx.Response:
    """Follow redirects manually, revalidating the target at every hop."""
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_remote_url(
                current,
                allow_private=settings.allow_private_addresses,
                allowed_ports=settings.allowed_ports,
            )
            try:
                response = await client.head(current, headers={"Accept": "*/*"})
                if response.status_code in (403, 405, 501):
                    # Some CDNs reject HEAD. Ask for one byte instead.
                    response = await client.get(current, headers={"Range": "bytes=0-0"})
            except httpx.HTTPError as exc:
                raise ExtractionError(
                    "Could not reach that URL. Check the link and try again.",
                    code="unreachable",
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ExtractionError(
                        "That URL redirected without a destination.", code="bad_redirect",
                    )
                current = str(httpx.URL(current).join(location))
                continue

            if response.status_code >= 400:
                raise ExtractionError(
                    f"That URL returned HTTP {response.status_code}.",
                    code="http_error",
                )
            return response

    raise ExtractionError("That URL redirected too many times.", code="too_many_redirects")


async def probe_direct_media(url: str, settings: Settings) -> ProbeResult:
    """Probe a URL that points straight at a media file."""
    try:
        response = await _head_with_redirect_checks(url, settings)
    except UrlRejected as exc:
        raise ExtractionError(str(exc), code=exc.code) from exc

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and not content_type.startswith(DIRECT_MEDIA_CONTENT_TYPES):
        raise ExtractionError(
            f"That URL serves '{content_type}', not a media file.",
            code="not_media",
        )

    total_bytes: int | None = None
    if content_range := response.headers.get("content-range"):
        # "bytes 0-0/12345"
        tail = content_range.rsplit("/", 1)[-1]
        total_bytes = int(tail) if tail.isdigit() else None
    length = response.headers.get("content-length")
    if total_bytes is None and length and length.isdigit() and response.status_code != 206:
        total_bytes = int(length)

    if total_bytes and total_bytes > settings.max_download_bytes:
        raise ExtractionError(
            f"That file is {total_bytes / 1_048_576:.0f} MB, over this server's "
            f"{settings.max_download_bytes / 1_048_576:.0f} MB limit.",
            code="too_large",
        )

    filename = None
    disposition = response.headers.get("content-disposition")
    if disposition and (match := _FILENAME_FROM_DISPOSITION.search(disposition)):
        filename = unquote(match.group(1))
    filename = filename or _filename_from_url(str(response.url)) or "download"
    container = posixpath.splitext(filename)[1].lstrip(".").lower() or None

    return ProbeResult(
        title=filename,
        uploader=urlsplit(str(response.url)).hostname,
        uploader_url=None,
        duration_seconds=None,
        thumbnail_url=None,
        description=None,
        upload_date=None,
        view_count=None,
        license_name=None,
        webpage_url=str(response.url),
        source_name="Direct media URL",
        metadata_source="HTTP response headers",
        formats=[MediaFormat(
            format_id="direct",
            container=container,
            quality_label="Original file",
            filesize_bytes=total_bytes,
            has_audio=True,
            has_video=not content_type.startswith("audio/"),
            is_progressive=True,
            note="Served exactly as the origin publishes it.",
        )],
        recommended_format_id="direct",
        total_bytes=total_bytes,
    )
