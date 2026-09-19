"""FastAPI application: routes, middleware and error translation.

See README.md for the source policy. In short: YouTube URLs get real metadata
from an official Google endpoint and an official embed player, and every file
download comes from the authorized source allowlist in `backend/sources.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, get_settings
from .downloader import run_direct_download, run_ytdlp_download
from .extractor import (
    ExtractionError,
    SourceNotAllowed,
    ffmpeg_available,
    probe_direct_media,
    probe_extractor_source,
)
from .jobs import JobManager
from .models import (
    AuthorizedAlternative,
    DownloadAccepted,
    DownloadRequest,
    InspectRequest,
    InspectResponse,
    JobState,
    JobStatus,
    SourcePolicy,
)
from .security import RateLimiter, UrlRejected, content_disposition, validate_remote_url
from .sources import SourceKind, allowed_sources_summary, classify
from .youtube import (
    YOUTUBE_DOWNLOAD_BLOCKED_REASON,
    YouTubeMetadataError,
    authorized_alternatives,
    fetch_metadata,
)

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "SAMEORIGIN",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' https: data:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
        "frame-ancestors 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.jobs = JobManager(settings)
    app.state.limiter = RateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds,
    )
    await app.state.jobs.start()
    logger.info(
        "started: allowlist=%s direct_media=%s ffmpeg=%s youtube_api_key=%s",
        ",".join(settings.allowed_extractors) or "none",
        settings.allow_direct_media_urls,
        ffmpeg_available(),
        settings.has_youtube_api_key,
    )
    try:
        yield
    finally:
        await app.state.jobs.shutdown()


app = FastAPI(
    title="Authorized Media Downloader",
    version=__version__,
    description=(
        "Inspect a media URL and download it, restricted to sources that publish "
        "their files without any access protection to circumvent."
    ),
    lifespan=lifespan,
    # The static frontend owns "/", so the interactive docs live under /api.
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _client_key(request: Request, settings: Settings) -> str:
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    """Reject callers that exceed the configured sliding window."""
    settings: Settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.limiter
    allowed, retry_after = await limiter.check(_client_key(request, settings))
    if not allowed:
        raise RateLimited(retry_after)


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


# --- middleware -----------------------------------------------------------------

if origins := get_settings().cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


# --- error handling -------------------------------------------------------------


def _error(message: str, code: str, http_status: int, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"error": message, "code": code}, status_code=http_status, headers=headers or None,
    )


@app.exception_handler(RateLimited)
async def _handle_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    return _error(
        f"Too many requests. Try again in {exc.retry_after} seconds.",
        "rate_limited",
        status.HTTP_429_TOO_MANY_REQUESTS,
        **{"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(UrlRejected)
async def _handle_url_rejected(request: Request, exc: UrlRejected) -> JSONResponse:
    return _error(str(exc), exc.code, status.HTTP_400_BAD_REQUEST)


@app.exception_handler(SourceNotAllowed)
async def _handle_source_not_allowed(request: Request, exc: SourceNotAllowed) -> JSONResponse:
    return _error(str(exc), exc.code, 422)


@app.exception_handler(ExtractionError)
async def _handle_extraction_error(request: Request, exc: ExtractionError) -> JSONResponse:
    http_status = (
        status.HTTP_502_BAD_GATEWAY
        if exc.code in {"unreachable", "http_error"}
        else 422
    )
    return _error(str(exc), exc.code, http_status)


@app.exception_handler(YouTubeMetadataError)
async def _handle_youtube_error(request: Request, exc: YouTubeMetadataError) -> JSONResponse:
    http_status = (
        status.HTTP_404_NOT_FOUND
        if exc.code == "youtube_not_found"
        else status.HTTP_502_BAD_GATEWAY
    )
    return _error(str(exc), exc.code, http_status)


# --- routes ---------------------------------------------------------------------


@app.get("/api/health", tags=["meta"])
async def health(request: Request) -> dict:
    jobs = _jobs(request)
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": ffmpeg_available(),
        "active_jobs": jobs.active_count,
    }


@app.get("/api/policy", response_model=SourcePolicy, tags=["meta"])
async def policy(request: Request) -> SourcePolicy:
    settings = _settings(request)
    return SourcePolicy(
        allowed_sources=allowed_sources_summary(settings),
        youtube_download_supported=False,
        youtube_policy_note=YOUTUBE_DOWNLOAD_BLOCKED_REASON,
        max_download_bytes=settings.max_download_bytes,
        max_duration_seconds=settings.max_duration_seconds,
        youtube_metadata_source=(
            "YouTube Data API v3" if settings.has_youtube_api_key else "YouTube oEmbed"
        ),
    )


async def _probe_source(
    url: str, kind: SourceKind, settings: Settings,
) -> tuple[object, SourceKind]:
    """Probe a non-YouTube URL, correcting the routing guess when it was wrong.

    `sources.classify` decides from the path alone, so a page whose URL merely
    ends in a media extension (a Wikimedia `File:....webm` page, for one) is
    guessed to be a direct file. The origin settles it: when that fetch comes
    back as a document rather than media, the extractor allowlist takes over.
    """
    if kind is SourceKind.DIRECT_MEDIA:
        try:
            return await probe_direct_media(url, settings), SourceKind.DIRECT_MEDIA
        except ExtractionError as exc:
            if exc.code != "not_media":
                raise
    return await probe_extractor_source(url, settings), SourceKind.EXTRACTOR


@app.post(
    "/api/inspect",
    response_model=InspectResponse,
    dependencies=[Depends(rate_limit)],
    tags=["media"],
)
async def inspect(request: Request, payload: InspectRequest) -> InspectResponse:
    """Return real metadata for a URL, and say plainly whether it can be downloaded."""
    settings = _settings(request)
    validated = validate_remote_url(
        payload.url,
        allow_private=settings.allow_private_addresses,
        allowed_ports=settings.allowed_ports,
    )
    decision = classify(validated.url, settings)

    if decision.kind is SourceKind.YOUTUBE:
        if not decision.youtube_video_id:
            raise UrlRejected(
                "That looks like a YouTube link but no video id could be read from it. "
                "Use a watch, youtu.be or shorts link.",
                code="youtube_no_video_id",
            )
        metadata = await fetch_metadata(decision.youtube_video_id, settings)
        return InspectResponse(
            url=validated.url,
            source_kind=SourceKind.YOUTUBE.value,
            source_name="YouTube",
            title=metadata.title,
            uploader=metadata.uploader,
            uploader_url=metadata.uploader_url,
            duration_seconds=metadata.duration_seconds,
            thumbnail_url=metadata.thumbnail_url,
            description=metadata.description,
            upload_date=metadata.upload_date,
            view_count=metadata.view_count,
            license_name=metadata.license_name,
            webpage_url=metadata.watch_url,
            embed_url=metadata.embed_url,
            downloadable=False,
            download_blocked_reason=YOUTUBE_DOWNLOAD_BLOCKED_REASON,
            metadata_source=metadata.source,
            formats=[],
            authorized_alternatives=[
                AuthorizedAlternative(**item) for item in authorized_alternatives(metadata)
            ],
        )

    probe, resolved_kind = await _probe_source(validated.url, decision.kind, settings)

    return InspectResponse(
        url=validated.url,
        source_kind=resolved_kind.value,
        source_name=probe.source_name,
        title=probe.title,
        uploader=probe.uploader,
        uploader_url=probe.uploader_url,
        duration_seconds=probe.duration_seconds,
        thumbnail_url=probe.thumbnail_url,
        description=probe.description,
        upload_date=probe.upload_date,
        view_count=probe.view_count,
        license_name=probe.license_name,
        webpage_url=probe.webpage_url,
        downloadable=True,
        metadata_source=probe.metadata_source,
        formats=probe.formats,
        recommended_format_id=probe.recommended_format_id,
    )


@app.post(
    "/api/download",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit)],
    tags=["media"],
)
async def start_download(request: Request, payload: DownloadRequest) -> DownloadAccepted:
    """Start a real download for an authorized source."""
    settings = _settings(request)
    manager = _jobs(request)

    validated = validate_remote_url(
        payload.url,
        allow_private=settings.allow_private_addresses,
        allowed_ports=settings.allowed_ports,
    )
    decision = classify(validated.url, settings)

    if decision.kind is SourceKind.YOUTUBE:
        raise SourceNotAllowed(YOUTUBE_DOWNLOAD_BLOCKED_REASON)

    # Re-probe rather than trusting the client: the allowlist check and the
    # size and duration ceilings must hold at download time too.
    probe, resolved_kind = await _probe_source(validated.url, decision.kind, settings)

    chosen_id = payload.format_id or probe.recommended_format_id
    chosen = next((f for f in probe.formats if f.format_id == chosen_id), None)
    if payload.format_id and chosen is None:
        raise ExtractionError(
            "That quality is no longer offered for this item. Inspect the link again "
            "to refresh the list.",
            code="format_unavailable",
        )
    if chosen and chosen.filesize_bytes and chosen.filesize_bytes > settings.max_download_bytes:
        raise ExtractionError(
            f"That rendition is {chosen.filesize_bytes / 1_048_576:.0f} MB, over this "
            f"server's {settings.max_download_bytes / 1_048_576:.0f} MB limit. "
            "Pick a smaller one.",
            code="too_large",
        )

    job = manager.create(validated.url, title=probe.title)
    job.total_bytes = chosen.filesize_bytes if chosen else probe.total_bytes

    if resolved_kind is SourceKind.DIRECT_MEDIA:
        def factory():
            return run_direct_download(
                validated.url,
                job.directory,
                probe.title or "download",
                settings,
                job.report,
            )
    else:
        def factory():
            return run_ytdlp_download(
                validated.url,
                job.directory,
                chosen_id,
                settings,
                job.report,
                is_progressive=bool(chosen.is_progressive) if chosen else True,
                wants_video=bool(chosen.has_video) if chosen else True,
            )

    manager.run(job, factory)

    return DownloadAccepted(
        job_id=job.job_id,
        state=job.state,
        status_url=f"/api/jobs/{job.job_id}",
        events_url=f"/api/jobs/{job.job_id}/events",
    )


def _job_status(job, settings: Settings) -> JobStatus:
    return JobStatus(
        job_id=job.job_id,
        state=job.state,
        progress_percent=job.progress_percent,
        downloaded_bytes=job.downloaded_bytes,
        total_bytes=job.total_bytes,
        speed_bytes_per_second=job.speed_bytes_per_second,
        eta_seconds=job.eta_seconds,
        filename=job.filename,
        filesize_bytes=job.filesize_bytes,
        title=job.title,
        error=job.error,
        expires_in_seconds=job.expires_in_seconds(settings.job_ttl_seconds),
        download_url=(
            f"/api/jobs/{job.job_id}/file" if job.state is JobState.COMPLETED else None
        ),
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatus, tags=["jobs"])
async def job_status(request: Request, job_id: str) -> JobStatus | JSONResponse:
    job = _jobs(request).get(job_id)
    if job is None:
        return _error(
            "That download has expired or never existed. Start it again.",
            "job_not_found",
            status.HTTP_404_NOT_FOUND,
        )
    return _job_status(job, _settings(request))


@app.get("/api/jobs/{job_id}/events", tags=["jobs"])
async def job_events(request: Request, job_id: str) -> Response:
    """Server-sent progress events, closing as soon as the job settles."""
    manager = _jobs(request)
    settings = _settings(request)
    job = manager.get(job_id)
    if job is None:
        return _error(
            "That download has expired or never existed. Start it again.",
            "job_not_found",
            status.HTTP_404_NOT_FOUND,
        )

    async def stream() -> AsyncIterator[bytes]:
        last_version = -1
        while True:
            if await request.is_disconnected():
                return
            current = manager.get(job_id)
            if current is None:
                yield b"event: gone\ndata: {}\n\n"
                return
            if current.version != last_version:
                last_version = current.version
                payload = _job_status(current, settings).model_dump(mode="json")
                yield f"data: {json.dumps(payload)}\n\n".encode()
            if current.is_terminal:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/jobs/{job_id}/file", tags=["jobs"])
async def job_file(request: Request, job_id: str) -> Response:
    """Serve the finished file once, as an attachment."""
    job = _jobs(request).get(job_id)
    if job is None:
        return _error(
            "That download has expired. Start it again.",
            "job_not_found",
            status.HTTP_404_NOT_FOUND,
        )
    if job.state is not JobState.COMPLETED or job.path is None:
        return _error(
            "That download is not finished yet.", "job_not_ready", status.HTTP_409_CONFLICT,
        )
    if not job.path.exists():
        return _error(
            "The finished file has already been cleaned up. Start the download again.",
            "file_gone",
            status.HTTP_410_GONE,
        )

    filename = job.filename or job.path.name
    return FileResponse(
        job.path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["jobs"])
async def delete_job(request: Request, job_id: str) -> Response:
    """Cancel a running job or release a finished one's file early."""
    await _jobs(request).cancel(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- static frontend ------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def run() -> None:  # pragma: no cover - entrypoint helper
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trust_forwarded_for,
        forwarded_allow_ips="*" if settings.trust_forwarded_for else None,
    )


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        run()
