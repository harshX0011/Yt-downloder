"""Official YouTube metadata handling."""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from backend.youtube import (
    YouTubeMetadata,
    YouTubeMetadataError,
    authorized_alternatives,
    fetch_metadata,
    parse_iso8601_duration,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT2H", 7200),
        ("P1DT30S", 86430),
        ("PT0S", None),
        ("", None),
        (None, None),
        ("not a duration", None),
    ],
)
def test_parse_iso8601_duration(value: str | None, expected: int | None) -> None:
    assert parse_iso8601_duration(value) == expected


def install_mock_transport(monkeypatch, handler) -> None:
    """Route every AsyncClient the module builds through a mock transport."""

    class MockedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("backend.youtube.httpx.AsyncClient", MockedClient)


@pytest.mark.asyncio
async def test_data_api_path_returns_real_fields(settings, monkeypatch) -> None:
    keyed = dataclasses.replace(settings, youtube_api_key="test-key")
    assert keyed.has_youtube_api_key is True
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.host == "www.googleapis.com"
        assert request.url.params["id"] == "dQw4w9WgXcQ"
        assert request.url.params["key"] == "test-key"
        return httpx.Response(200, json={
            "items": [{
                "snippet": {
                    "title": "A real title",
                    "channelTitle": "A channel",
                    "channelId": "UC123",
                    "publishedAt": "2009-10-25T06:57:33Z",
                    "description": "Some description",
                    "thumbnails": {"maxres": {"url": "https://i.ytimg.com/vi/x/maxres.jpg"}},
                },
                "contentDetails": {"duration": "PT3M33S"},
                "statistics": {"viewCount": "1234567"},
                "status": {"license": "creativeCommon"},
            }],
        })

    install_mock_transport(monkeypatch, handler)

    metadata = await fetch_metadata("dQw4w9WgXcQ", keyed)
    assert seen == ["/youtube/v3/videos"]
    assert metadata.title == "A real title"
    assert metadata.uploader == "A channel"
    assert metadata.uploader_url == "https://www.youtube.com/channel/UC123"
    assert metadata.duration_seconds == 213
    assert metadata.view_count == 1234567
    assert metadata.license_name == "Creative Commons Attribution (CC BY)"
    assert metadata.upload_date == "2009-10-25"
    assert metadata.source == "YouTube Data API v3"


@pytest.mark.asyncio
async def test_oembed_is_used_when_no_api_key_is_configured(settings, monkeypatch) -> None:
    keyless = dataclasses.replace(settings, youtube_api_key="")
    assert keyless.has_youtube_api_key is False

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.youtube.com"
        assert request.url.path == "/oembed"
        return httpx.Response(200, json={
            "title": "oEmbed title",
            "author_name": "oEmbed channel",
            "author_url": "https://www.youtube.com/@channel",
            "thumbnail_url": "https://i.ytimg.com/vi/x/hqdefault.jpg",
        })

    install_mock_transport(monkeypatch, handler)

    metadata = await fetch_metadata("dQw4w9WgXcQ", keyless)
    assert metadata.title == "oEmbed title"
    assert metadata.uploader == "oEmbed channel"
    assert metadata.source == "YouTube oEmbed"
    # oEmbed genuinely carries no duration, so it stays null rather than being faked.
    assert metadata.duration_seconds is None


@pytest.mark.asyncio
async def test_a_rejected_api_key_falls_back_to_oembed(settings, monkeypatch) -> None:
    keyed = dataclasses.replace(settings, youtube_api_key="bad-key")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.host == "www.googleapis.com":
            return httpx.Response(403, json={"error": {"message": "keyInvalid"}})
        return httpx.Response(200, json={"title": "Fallback title"})

    install_mock_transport(monkeypatch, handler)

    metadata = await fetch_metadata("dQw4w9WgXcQ", keyed)
    assert paths == ["/youtube/v3/videos", "/oembed"]
    assert metadata.title == "Fallback title"
    assert metadata.source == "YouTube oEmbed"


@pytest.mark.asyncio
async def test_missing_video_raises_not_found(settings, monkeypatch) -> None:
    keyless = dataclasses.replace(settings, youtube_api_key="")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    install_mock_transport(monkeypatch, handler)

    with pytest.raises(YouTubeMetadataError) as excinfo:
        await fetch_metadata("dQw4w9WgXcQ", keyless)
    assert excinfo.value.code == "youtube_not_found"


@pytest.mark.asyncio
async def test_api_key_is_never_echoed_in_an_error(settings, monkeypatch) -> None:
    keyed = dataclasses.replace(settings, youtube_api_key="super-secret-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            return httpx.Response(400, json={"error": {"message": "API key not valid"}})
        return httpx.Response(500, text="oEmbed down")

    install_mock_transport(monkeypatch, handler)

    with pytest.raises(YouTubeMetadataError) as excinfo:
        await fetch_metadata("dQw4w9WgXcQ", keyed)
    assert "super-secret-key" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_embedding_disabled_is_explained(settings, monkeypatch) -> None:
    keyless = dataclasses.replace(settings, youtube_api_key="")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    install_mock_transport(monkeypatch, handler)

    with pytest.raises(YouTubeMetadataError) as excinfo:
        await fetch_metadata("dQw4w9WgXcQ", keyless)
    assert excinfo.value.code == "youtube_embedding_disabled"


def test_authorized_alternatives_are_official_routes() -> None:
    metadata = YouTubeMetadata(
        video_id="dQw4w9WgXcQ",
        title="t", uploader="u", uploader_url=None, duration_seconds=None,
        thumbnail_url=None, description=None, upload_date=None, view_count=None,
        license_name=None, source="test",
    )
    alternatives = authorized_alternatives(metadata)
    urls = [item["url"] for item in alternatives]
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in urls
    assert any("premium" in (url or "") for url in urls)
    assert any("studio.youtube.com" in (url or "") for url in urls)
    assert metadata.embed_url == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
