"""End-to-end API behaviour, with the network stubbed out."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import extractor
from backend.models import MediaFormat
from backend.youtube import YouTubeMetadata


@pytest.fixture
def stub_youtube(monkeypatch):
    async def fake_fetch(video_id: str, settings):
        return YouTubeMetadata(
            video_id=video_id,
            title="Stubbed title",
            uploader="Stubbed channel",
            uploader_url="https://www.youtube.com/@stub",
            duration_seconds=213,
            thumbnail_url="https://i.ytimg.com/vi/x/maxres.jpg",
            description="desc",
            upload_date="2009-10-25",
            view_count=42,
            license_name="Standard YouTube licence",
            source="YouTube Data API v3",
        )

    monkeypatch.setattr("backend.main.fetch_metadata", fake_fetch)


@pytest.fixture
def stub_direct_probe(monkeypatch):
    async def fake_probe(url: str, settings):
        return extractor.ProbeResult(
            title="clip.mp4",
            uploader="example.org",
            uploader_url=None,
            duration_seconds=12,
            thumbnail_url=None,
            description=None,
            upload_date=None,
            view_count=None,
            license_name=None,
            webpage_url=url,
            source_name="Direct media URL",
            metadata_source="HTTP response headers",
            formats=[MediaFormat(
                format_id="direct",
                container="mp4",
                quality_label="Original file",
                filesize_bytes=2048,
            )],
            recommended_format_id="direct",
            total_bytes=2048,
        )

    monkeypatch.setattr("backend.main.probe_direct_media", fake_probe)


def test_health(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_policy_never_advertises_youtube_downloads(client) -> None:
    response = client.get("/api/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["youtube_download_supported"] is False
    assert body["youtube_policy_note"]
    assert body["max_download_bytes"] > 0


def test_frontend_is_served(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Authorized Media Downloader" in response.text


def test_security_headers_are_present(client) -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-src https://www.youtube-nocookie.com" in response.headers[
        "content-security-policy"
    ]


def test_inspect_youtube_returns_real_metadata_and_refuses_download(
    client, stub_youtube,
) -> None:
    response = client.post(
        "/api/inspect", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["source_kind"] == "youtube"
    assert body["title"] == "Stubbed title"
    assert body["duration_seconds"] == 213
    assert body["thumbnail_url"]
    assert body["metadata_source"] == "YouTube Data API v3"

    assert body["downloadable"] is False
    assert body["formats"] == []
    assert "no authorized" in body["download_blocked_reason"].lower() or \
        "does not publish" in body["download_blocked_reason"].lower()
    assert body["embed_url"] == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert len(body["authorized_alternatives"]) >= 3


def test_download_of_a_youtube_url_is_refused(client) -> None:
    response = client.post(
        "/api/download", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "source_not_allowed"


def test_youtube_url_without_a_video_id_is_rejected(client) -> None:
    response = client.post(
        "/api/inspect", json={"url": "https://www.youtube.com/feed/subscriptions"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "youtube_no_video_id"


def test_inspect_rejects_private_targets(client) -> None:
    response = client.post("/api/inspect", json={"url": "http://169.254.169.254/latest/"})
    assert response.status_code == 400
    assert response.json()["code"] == "url_private_address"


def test_inspect_rejects_non_http_schemes(client) -> None:
    response = client.post("/api/inspect", json={"url": "file:///etc/passwd"})
    assert response.status_code == 400
    assert response.json()["code"] == "url_bad_scheme"


def test_inspect_direct_media_offers_a_download(client, stub_direct_probe) -> None:
    response = client.post("/api/inspect", json={"url": "https://example.org/clip.mp4"})
    assert response.status_code == 200
    body = response.json()
    assert body["downloadable"] is True
    assert body["recommended_format_id"] == "direct"
    assert body["formats"][0]["container"] == "mp4"


def test_a_page_url_ending_in_mp4_falls_back_to_the_extractor(
    client, monkeypatch,
) -> None:
    """A wiki page whose URL ends in .webm must not die on the direct-file path."""
    calls: list[str] = []

    async def fake_direct(url: str, settings):
        calls.append("direct")
        raise extractor.ExtractionError(
            "That URL serves 'text/html', not a media file.", code="not_media",
        )

    async def fake_extractor(url: str, settings):
        calls.append("extractor")
        return extractor.ProbeResult(
            title="A freely licensed clip",
            uploader="Wikimedia Commons",
            uploader_url=None,
            duration_seconds=30,
            thumbnail_url=None,
            description=None,
            upload_date=None,
            view_count=None,
            license_name="CC BY-SA",
            webpage_url=url,
            source_name="wikimedia",
            metadata_source="yt-dlp Wikimedia extractor",
            formats=[MediaFormat(format_id="0", container="webm", quality_label="720p")],
            recommended_format_id="0",
        )

    monkeypatch.setattr("backend.main.probe_direct_media", fake_direct)
    monkeypatch.setattr("backend.main.probe_extractor_source", fake_extractor)

    response = client.post(
        "/api/inspect",
        json={"url": "https://commons.wikimedia.org/wiki/File:Some_clip.webm"},
    )
    assert response.status_code == 200
    body = response.json()
    assert calls == ["direct", "extractor"]
    assert body["source_kind"] == "extractor"
    assert body["downloadable"] is True
    assert body["title"] == "A freely licensed clip"


def test_a_real_direct_file_error_is_not_swallowed(client, monkeypatch) -> None:
    """Only 'not_media' falls through; a genuine failure still surfaces."""

    async def fake_direct(url: str, settings):
        raise extractor.ExtractionError("That URL returned HTTP 404.", code="http_error")

    monkeypatch.setattr("backend.main.probe_direct_media", fake_direct)
    response = client.post("/api/inspect", json={"url": "https://example.org/clip.mp4"})
    assert response.status_code == 502
    assert response.json()["code"] == "http_error"


def test_unsupported_site_is_refused_with_a_clear_reason(client, monkeypatch) -> None:
    async def fake_probe(url: str, settings):
        raise extractor.SourceNotAllowed("That site is not on the allowlist.")

    monkeypatch.setattr("backend.main.probe_extractor_source", fake_probe)
    response = client.post("/api/inspect", json={"url": "https://example.org/some/page"})
    assert response.status_code == 422
    assert response.json()["code"] == "source_not_allowed"


def test_full_direct_download_lifecycle(client, stub_direct_probe, monkeypatch) -> None:
    payload = b"fake mp4 bytes" * 64

    async def fake_download(url, dest_dir, filename_hint, settings, progress):
        from backend.downloader import DownloadOutcome
        from backend.models import JobState

        destination = Path(dest_dir) / "clip.mp4"
        destination.write_bytes(payload)
        progress(JobState.DOWNLOADING, len(payload), len(payload), 1000.0, 0)
        return DownloadOutcome(
            path=destination, filename="clip.mp4", filesize_bytes=len(payload),
        )

    monkeypatch.setattr("backend.main.run_direct_download", fake_download)

    accepted = client.post("/api/download", json={"url": "https://example.org/clip.mp4"})
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    # The SSE stream terminates on its own once the job settles.
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        assert stream.status_code == 200
        events = b"".join(stream.iter_bytes())
    assert b"completed" in events

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["state"] == "completed"
    assert status["filesize_bytes"] == len(payload)
    assert status["download_url"] == f"/api/jobs/{job_id}/file"

    downloaded = client.get(status["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "clip.mp4" in downloaded.headers["content-disposition"]

    # Releasing the job removes the file.
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_failed_job_reports_the_reason(client, stub_direct_probe, monkeypatch) -> None:
    async def failing_download(url, dest_dir, filename_hint, settings, progress):
        from backend.downloader import DownloadFailed

        raise DownloadFailed("The origin returned HTTP 503.", code="http_error")

    monkeypatch.setattr("backend.main.run_direct_download", failing_download)

    job_id = client.post(
        "/api/download", json={"url": "https://example.org/clip.mp4"},
    ).json()["job_id"]

    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        b"".join(stream.iter_bytes())

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["state"] == "failed"
    assert status["error"] == "The origin returned HTTP 503."
    assert status["download_url"] is None


def test_requesting_an_unknown_format_is_rejected(client, stub_direct_probe) -> None:
    response = client.post(
        "/api/download",
        json={"url": "https://example.org/clip.mp4", "format_id": "does-not-exist"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "format_unavailable"


def test_unknown_job_returns_404(client) -> None:
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/file").status_code == 404
    assert client.get("/api/jobs/nope/events").status_code == 404


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch) -> None:
    from backend.security import RateLimiter

    client.app.state.limiter = RateLimiter(limit=1, window_seconds=60)
    first = client.post("/api/inspect", json={"url": "file:///etc/passwd"})
    assert first.status_code == 400

    second = client.post("/api/inspect", json={"url": "file:///etc/passwd"})
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert int(second.headers["retry-after"]) > 0
