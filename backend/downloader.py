"""The two real download paths: plain HTTP streaming, and yt-dlp.

Both enforce the configured byte ceiling while the transfer is in flight, not
just from the advertised Content-Length, and both write only inside the
per-job directory handed to them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .extractor import (
    ExtractionError,
    build_ytdlp_options,
    ffmpeg_available,
)
from .models import JobState
from .security import MAX_REDIRECTS, UrlRejected, safe_filename, validate_remote_url

CHUNK_SIZE = 256 * 1024

# progress(state, downloaded_bytes, total_bytes, speed_bps, eta_seconds)
ProgressCallback = Callable[[JobState, int, int | None, float | None, int | None], None]


@dataclass
class DownloadOutcome:
    path: Path
    filename: str
    filesize_bytes: int


class DownloadFailed(Exception):
    """Raised when a download cannot be completed. The message is user-facing."""

    def __init__(self, message: str, *, code: str = "download_failed") -> None:
        super().__init__(message)
        self.code = code


def _too_large_message(settings: Settings) -> str:
    limit_mb = settings.max_download_bytes / 1_048_576
    return (
        f"That file is larger than this server's {limit_mb:.0f} MB limit, so the "
        "download was stopped."
    )


async def run_direct_download(
    url: str,
    dest_dir: Path,
    filename_hint: str,
    settings: Settings,
    progress: ProgressCallback,
) -> DownloadOutcome:
    """Stream a published media file to disk, revalidating every redirect hop."""
    filename = safe_filename(filename_hint, fallback="download")
    if "." not in filename:
        filename = f"{filename}.mp4"
    destination = dest_dir / filename

    timeout = httpx.Timeout(settings.request_timeout_seconds, read=None)
    current = url
    downloaded = 0

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                validate_remote_url(
                    current,
                    allow_private=settings.allow_private_addresses,
                    allowed_ports=settings.allowed_ports,
                )
            except UrlRejected as exc:
                raise DownloadFailed(str(exc), code=exc.code) from exc

            try:
                async with client.stream("GET", current, headers={"Accept": "*/*"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadFailed(
                                "That URL redirected without a destination.",
                                code="bad_redirect",
                            )
                        current = str(httpx.URL(current).join(location))
                        continue

                    if response.status_code >= 400:
                        raise DownloadFailed(
                            f"The origin returned HTTP {response.status_code}.",
                            code="http_error",
                        )

                    total: int | None = None
                    if (length := response.headers.get("content-length")) and length.isdigit():
                        total = int(length)
                    if total and total > settings.max_download_bytes:
                        raise DownloadFailed(_too_large_message(settings), code="too_large")

                    progress(JobState.DOWNLOADING, 0, total, None, None)
                    loop = asyncio.get_running_loop()
                    started = loop.time()

                    with destination.open("wb") as handle:
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            downloaded += len(chunk)
                            if downloaded > settings.max_download_bytes:
                                handle.close()
                                destination.unlink(missing_ok=True)
                                raise DownloadFailed(
                                    _too_large_message(settings), code="too_large",
                                )
                            handle.write(chunk)
                            elapsed = max(loop.time() - started, 1e-6)
                            speed = downloaded / elapsed
                            eta = (
                                int((total - downloaded) / speed)
                                if total and speed > 0 and total > downloaded
                                else None
                            )
                            progress(JobState.DOWNLOADING, downloaded, total, speed, eta)

                    return DownloadOutcome(
                        path=destination,
                        filename=destination.name,
                        filesize_bytes=destination.stat().st_size,
                    )
            except httpx.HTTPError as exc:
                destination.unlink(missing_ok=True)
                raise DownloadFailed(
                    "The transfer failed partway through. Try again.",
                    code="transfer_failed",
                ) from exc

    raise DownloadFailed("That URL redirected too many times.", code="too_many_redirects")


def _format_selector(format_id: str | None, *, is_progressive: bool, allow_merge: bool) -> str:
    """Pick a yt-dlp format expression, preferring a single MP4 file."""
    if format_id:
        if is_progressive or not allow_merge:
            return format_id
        return f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}"
    if allow_merge:
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/"
            "bestvideo+bestaudio/best"
        )
    return (
        "best[ext=mp4][vcodec!=none][acodec!=none]/"
        "best[vcodec!=none][acodec!=none]/best"
    )


def _ytdlp_download_sync(
    url: str,
    dest_dir: Path,
    format_id: str | None,
    is_progressive: bool,
    wants_video: bool,
    settings: Settings,
    progress: ProgressCallback,
) -> DownloadOutcome:
    """Blocking yt-dlp download. Runs in a worker thread."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    allow_merge = ffmpeg_available()
    reported_total: int | None = None

    def hook(status: dict) -> None:
        nonlocal reported_total
        phase = status.get("status")
        if phase == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            reported_total = int(total) if total else reported_total
            progress(
                JobState.DOWNLOADING,
                int(status.get("downloaded_bytes") or 0),
                reported_total,
                status.get("speed"),
                int(status["eta"]) if status.get("eta") else None,
            )
        elif phase == "finished":
            progress(
                JobState.PROCESSING,
                int(status.get("downloaded_bytes") or 0),
                reported_total,
                None,
                None,
            )

    options = build_ytdlp_options(settings) | {
        "format": _format_selector(
            format_id, is_progressive=is_progressive, allow_merge=allow_merge,
        ),
        "outtmpl": {"default": str(dest_dir / "%(title).120B.%(ext)s")},
        "paths": {"home": str(dest_dir)},
        "windowsfilenames": True,
        "max_filesize": settings.max_download_bytes,
        "overwrites": True,
        "progress_hooks": [hook],
        "noprogress": True,
        "writethumbnail": False,
        "writesubtitles": False,
        "postprocessors": [],
    }
    if allow_merge:
        options["merge_output_format"] = "mp4"
        if wants_video:
            # Remux (no re-encode) into MP4 when the codecs allow it.
            options["postprocessors"] = [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ]

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        message = str(exc)
        if "File is larger than max-filesize" in message:
            raise DownloadFailed(_too_large_message(settings), code="too_large") from exc
        raise DownloadFailed(
            "The download failed. The source may have withdrawn the file or changed "
            "its formats. Try inspecting the link again.",
            code="download_failed",
        ) from exc

    if not info:
        raise DownloadFailed("The download produced no file.", code="download_empty")
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise DownloadFailed("The download produced no file.", code="download_empty")
        info = entries[0]

    path: Path | None = None
    for candidate in info.get("requested_downloads") or []:
        raw = candidate.get("filepath") or candidate.get("_filename")
        if raw and Path(raw).exists():
            path = Path(raw)
            break
    raw = info.get("filepath") or info.get("_filename")
    if path is None and raw and Path(raw).exists():
        path = Path(raw)
    if path is None:
        produced = sorted(
            (p for p in dest_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        path = produced[0] if produced else None
    if path is None:
        raise DownloadFailed(
            "The download finished but no file was written.", code="download_missing",
        )

    size = path.stat().st_size
    if size > settings.max_download_bytes:
        path.unlink(missing_ok=True)
        raise DownloadFailed(_too_large_message(settings), code="too_large")

    return DownloadOutcome(path=path, filename=path.name, filesize_bytes=size)


async def run_ytdlp_download(
    url: str,
    dest_dir: Path,
    format_id: str | None,
    settings: Settings,
    progress: ProgressCallback,
    *,
    is_progressive: bool = True,
    wants_video: bool = True,
) -> DownloadOutcome:
    """Download from an allowlisted source, off the event loop."""
    try:
        return await asyncio.to_thread(
            _ytdlp_download_sync,
            url,
            dest_dir,
            format_id,
            is_progressive,
            wants_video,
            settings,
            progress,
        )
    except ExtractionError as exc:
        raise DownloadFailed(str(exc), code=exc.code) from exc


def remove_tree(path: Path) -> None:
    """Best-effort recursive delete of a job directory."""
    import shutil

    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)
