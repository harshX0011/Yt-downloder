"""Request and response schemas for the public API."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class InspectRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048, description="The media page or file URL")


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    format_id: str | None = Field(
        default=None,
        max_length=128,
        description="One of the format ids returned by /api/inspect. Defaults to best MP4.",
    )


class MediaFormat(BaseModel):
    """A single downloadable rendition."""

    format_id: str
    container: str | None = None
    quality_label: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    filesize_bytes: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool = True
    has_video: bool = True
    is_progressive: bool = True
    note: str | None = None


class AuthorizedAlternative(BaseModel):
    """An official route to the file, for sources we will not download from."""

    title: str
    detail: str
    url: str | None = None


class InspectResponse(BaseModel):
    """What /api/inspect returns for any URL."""

    url: str
    source_kind: str
    source_name: str

    # Real metadata. Never placeholder values: if a field is unknown it is null.
    title: str | None = None
    uploader: str | None = None
    uploader_url: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    description: str | None = None
    upload_date: str | None = None
    view_count: int | None = None
    license_name: str | None = None
    webpage_url: str | None = None

    # Playback for sources that permit embedding but not downloading.
    embed_url: str | None = None

    # Download eligibility.
    downloadable: bool = False
    download_blocked_reason: str | None = None
    metadata_source: str | None = None
    formats: list[MediaFormat] = Field(default_factory=list)
    recommended_format_id: str | None = None
    authorized_alternatives: list[AuthorizedAlternative] = Field(default_factory=list)


class JobState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: int | None = None
    filename: str | None = None
    filesize_bytes: int | None = None
    title: str | None = None
    error: str | None = None
    expires_in_seconds: int | None = None
    download_url: str | None = None


class DownloadAccepted(BaseModel):
    job_id: str
    state: JobState
    status_url: str
    events_url: str


class SourcePolicy(BaseModel):
    """Served to the UI so it can explain the policy without hardcoding it."""

    allowed_sources: list[dict[str, str]]
    youtube_download_supported: bool = False
    youtube_policy_note: str
    max_download_bytes: int
    max_duration_seconds: int
    youtube_metadata_source: str


class ApiError(BaseModel):
    error: str
    code: str = "error"
    detail: str | None = None
