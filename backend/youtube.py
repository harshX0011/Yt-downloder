"""Official YouTube metadata.

Two authorized, documented endpoints are used, in this order:

1. YouTube Data API v3 `videos.list` when `YOUTUBE_API_KEY` is configured.
   Gives title, channel, duration, licence, view count and thumbnails.
2. YouTube's public oEmbed endpoint otherwise. Gives title, channel and
   thumbnail but no duration, which is why the API key is recommended.

Neither endpoint exposes a media stream, and no other Google API does either.
That is the whole reason this app cannot offer a YouTube file download: there is
no authorized mechanism to implement. We therefore surface the official watch
and embed routes instead of defeating YouTube's protections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import Settings

OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
DATA_API_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

_ISO8601_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
)

LICENSE_LABELS = {
    "youtube": "Standard YouTube licence",
    "creativeCommon": "Creative Commons Attribution (CC BY)",
}

YOUTUBE_DOWNLOAD_BLOCKED_REASON = (
    "YouTube does not publish any authorized API or endpoint that returns a video "
    "file, so this app will not produce one. Fetching the stream anyway requires "
    "defeating YouTube's signature, rate-limit and bot-detection measures, which "
    "this app does not do. The official routes are listed below."
)


class YouTubeMetadataError(Exception):
    """Raised when official metadata cannot be retrieved. Message is user-facing."""

    def __init__(self, message: str, *, code: str = "youtube_metadata_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class YouTubeMetadata:
    """Real metadata from an official endpoint. Unknown fields stay None."""

    video_id: str
    title: str | None
    uploader: str | None
    uploader_url: str | None
    duration_seconds: int | None
    thumbnail_url: str | None
    description: str | None
    upload_date: str | None
    view_count: int | None
    license_name: str | None
    source: str

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube-nocookie.com/embed/{self.video_id}"


def parse_iso8601_duration(value: str | None) -> int | None:
    """Convert an ISO 8601 duration (`PT4M13S`) to whole seconds."""
    if not value:
        return None
    match = _ISO8601_DURATION.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    total = 0.0
    total += int(parts["days"] or 0) * 86400
    total += int(parts["hours"] or 0) * 3600
    total += int(parts["minutes"] or 0) * 60
    total += float(parts["seconds"] or 0)
    return int(total) if total > 0 else None


def _best_thumbnail(thumbnails: dict[str, dict]) -> str | None:
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]
    return None


async def _fetch_via_data_api(
    client: httpx.AsyncClient, video_id: str, api_key: str,
) -> YouTubeMetadata:
    response = await client.get(
        DATA_API_ENDPOINT,
        params={
            "part": "snippet,contentDetails,statistics,status",
            "id": video_id,
            "key": api_key,
        },
    )

    if response.status_code in (400, 401, 403):
        # Never leak the key or Google's raw payload back to the caller.
        raise YouTubeMetadataError(
            "The configured YouTube Data API key was rejected. Check that the key is "
            "valid and that the YouTube Data API v3 is enabled for its project.",
            code="youtube_api_key_rejected",
        )
    if response.status_code == 429:
        raise YouTubeMetadataError(
            "The YouTube Data API quota for this key is exhausted. Try again later.",
            code="youtube_api_quota",
        )
    response.raise_for_status()

    items = response.json().get("items") or []
    if not items:
        raise YouTubeMetadataError(
            "No public video was found for that URL. It may be private, unlisted, "
            "deleted, or the id may be wrong.",
            code="youtube_not_found",
        )

    item = items[0]
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    statistics = item.get("statistics") or {}
    status = item.get("status") or {}

    published = snippet.get("publishedAt")
    view_count = statistics.get("viewCount")

    return YouTubeMetadata(
        video_id=video_id,
        title=snippet.get("title"),
        uploader=snippet.get("channelTitle"),
        uploader_url=(
            f"https://www.youtube.com/channel/{snippet['channelId']}"
            if snippet.get("channelId")
            else None
        ),
        duration_seconds=parse_iso8601_duration(content_details.get("duration")),
        thumbnail_url=_best_thumbnail(snippet.get("thumbnails") or {}),
        description=(snippet.get("description") or None),
        upload_date=(published[:10] if isinstance(published, str) else None),
        view_count=int(view_count) if str(view_count).isdigit() else None,
        license_name=LICENSE_LABELS.get(status.get("license"), status.get("license")),
        source="YouTube Data API v3",
    )


async def _fetch_via_oembed(client: httpx.AsyncClient, video_id: str) -> YouTubeMetadata:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    response = await client.get(
        OEMBED_ENDPOINT, params={"url": watch_url, "format": "json"},
    )

    if response.status_code == 404:
        raise YouTubeMetadataError(
            "No public video was found for that URL. It may be private, unlisted "
            "or deleted.",
            code="youtube_not_found",
        )
    if response.status_code == 401:
        raise YouTubeMetadataError(
            "That video's owner has disabled embedding, so YouTube will not release "
            "its metadata over oEmbed. Configure YOUTUBE_API_KEY to read public "
            "metadata for it instead.",
            code="youtube_embedding_disabled",
        )
    response.raise_for_status()

    payload = response.json()
    return YouTubeMetadata(
        video_id=video_id,
        title=payload.get("title"),
        uploader=payload.get("author_name"),
        uploader_url=payload.get("author_url"),
        duration_seconds=None,  # oEmbed genuinely does not carry duration.
        thumbnail_url=payload.get("thumbnail_url"),
        description=None,
        upload_date=None,
        view_count=None,
        license_name=None,
        source="YouTube oEmbed",
    )


async def fetch_metadata(video_id: str, settings: Settings) -> YouTubeMetadata:
    """Fetch real public metadata for a YouTube video id."""
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if settings.has_youtube_api_key:
            try:
                return await _fetch_via_data_api(client, video_id, settings.youtube_api_key)
            except YouTubeMetadataError as exc:
                # A bad key should not hide the video entirely: fall back to oEmbed.
                if exc.code not in {"youtube_api_key_rejected", "youtube_api_quota"}:
                    raise
            except httpx.HTTPError:
                pass

        try:
            return await _fetch_via_oembed(client, video_id)
        except httpx.HTTPError as exc:
            raise YouTubeMetadataError(
                "Could not reach YouTube to read this video's public metadata. "
                "Check the server's network connection and try again.",
                code="youtube_unreachable",
            ) from exc


def authorized_alternatives(metadata: YouTubeMetadata) -> list[dict[str, str | None]]:
    """The official ways to get this video, in place of an unauthorized download."""
    return [
        {
            "title": "Watch on YouTube",
            "detail": "The authorized way to view the video, with the creator credited.",
            "url": metadata.watch_url,
        },
        {
            "title": "Save offline with YouTube Premium",
            "detail": (
                "YouTube's own offline download, available in the YouTube mobile app "
                "and on youtube.com for Premium members. This is the only "
                "YouTube-sanctioned download for videos you do not own."
            ),
            "url": "https://www.youtube.com/premium",
        },
        {
            "title": "Download your own upload from YouTube Studio",
            "detail": (
                "If this is your video, YouTube Studio lets you download the original "
                "file: Content, then the menu next to the video, then Download. "
                "Google Takeout can export your whole channel."
            ),
            "url": "https://studio.youtube.com",
        },
        {
            "title": "Embed it legitimately",
            "detail": (
                "The IFrame Player API is the supported way to put this video in your "
                "own site or app, which the preview above already uses."
            ),
            "url": "https://developers.google.com/youtube/iframe_api_reference",
        },
    ]
