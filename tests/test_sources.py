"""Source classification: the policy that decides what may be downloaded."""

from __future__ import annotations

import pytest

from backend.sources import (
    SourceKind,
    allowed_sources_summary,
    classify,
    extract_youtube_video_id,
    is_extractor_allowed,
    is_youtube_url,
    looks_like_direct_media,
)


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=tooshort", None),
        ("https://www.youtube.com/feed/subscriptions", None),
        ("https://example.org/watch?v=dQw4w9WgXcQ", None),
    ],
)
def test_extract_youtube_video_id(url: str, video_id: str | None) -> None:
    assert extract_youtube_video_id(url) == video_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://YOUTU.BE/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_youtube_hosts_are_recognised(url: str) -> None:
    assert is_youtube_url(url) is True


def test_lookalike_host_is_not_treated_as_youtube() -> None:
    # A host that merely contains "youtube.com" must not match.
    assert is_youtube_url("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ") is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org/talk.mp4", True),
        ("https://example.org/a/b/clip.WEBM", True),
        ("https://example.org/song.m4a", True),
        ("https://example.org/file.mp4?token=1", True),
        ("https://example.org/page.html", False),
        ("https://example.org/watch", False),
        ("https://example.org/archive.zip", False),
    ],
)
def test_looks_like_direct_media(url: str, expected: bool) -> None:
    assert looks_like_direct_media(url) is expected


def test_youtube_is_classified_before_anything_else(settings) -> None:
    decision = classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings)
    assert decision.kind is SourceKind.YOUTUBE
    assert decision.youtube_video_id == "dQw4w9WgXcQ"


def test_direct_media_classification(settings) -> None:
    decision = classify("https://example.org/talk.mp4", settings)
    assert decision.kind is SourceKind.DIRECT_MEDIA
    assert decision.hostname == "example.org"


def test_unknown_page_goes_to_the_extractor_path(settings) -> None:
    decision = classify("https://archive.org/details/some-item", settings)
    assert decision.kind is SourceKind.EXTRACTOR


def test_allowlist_membership(settings) -> None:
    assert is_extractor_allowed("ArchiveOrg", settings) is True
    assert is_extractor_allowed("Wikimedia", settings) is True
    # The point of the allowlist: YouTube's extractor is never permitted.
    assert is_extractor_allowed("Youtube", settings) is False
    assert is_extractor_allowed("Generic", settings) is False


def test_allowed_sources_summary_describes_every_entry(settings) -> None:
    summary = allowed_sources_summary(settings)
    assert summary
    for entry in summary:
        assert entry["name"] and entry["host"] and entry["description"]
    assert not any(entry["name"] == "YouTube" for entry in summary)
