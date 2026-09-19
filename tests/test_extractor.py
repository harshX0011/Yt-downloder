"""Format selection and the yt-dlp option hardening."""

from __future__ import annotations

from backend.downloader import _format_selector
from backend.extractor import _format_entries, build_ytdlp_options


def test_ytdlp_options_disable_protection_workarounds(settings) -> None:
    options = build_ytdlp_options(settings)
    assert options["geo_bypass"] is False
    assert options["cookiefile"] is None
    assert options["cookiesfrombrowser"] is None
    assert options["nocheckcertificate"] is False
    assert options["noplaylist"] is True


def test_ytdlp_options_pin_the_extractor_allowlist(settings) -> None:
    patterns = build_ytdlp_options(settings)["allowed_extractors"]
    assert patterns
    for pattern in patterns:
        assert pattern.startswith("^") and pattern.endswith("$")
    assert "^ArchiveOrg$" in patterns
    # yt-dlp itself must refuse a YouTube URL even if one reached it.
    assert not any("Youtube" in pattern for pattern in patterns)


def test_format_entries_prefers_progressive_mp4() -> None:
    info = {"formats": [
        {"format_id": "webm-720", "url": "https://e.test/720.webm", "ext": "webm",
         "vcodec": "vp9", "acodec": "opus", "height": 720},
        {"format_id": "mp4-360", "url": "https://e.test/360.mp4", "ext": "mp4",
         "vcodec": "avc1", "acodec": "mp4a", "height": 360},
        {"format_id": "mp4-1080-video-only", "url": "https://e.test/1080.mp4", "ext": "mp4",
         "vcodec": "avc1", "acodec": "none", "height": 1080},
    ]}
    formats, recommended = _format_entries(info, allow_merge=True)
    assert recommended == "mp4-360"
    by_id = {f.format_id: f for f in formats}
    assert by_id["mp4-1080-video-only"].is_progressive is False
    assert by_id["mp4-1080-video-only"].note


def test_format_entries_drops_split_streams_without_ffmpeg() -> None:
    info = {"formats": [
        {"format_id": "video-only", "url": "https://e.test/1080.mp4", "ext": "mp4",
         "vcodec": "avc1", "acodec": "none", "height": 1080},
        {"format_id": "both", "url": "https://e.test/480.mp4", "ext": "mp4",
         "vcodec": "avc1", "acodec": "mp4a", "height": 480},
    ]}
    _, recommended = _format_entries(info, allow_merge=False)
    assert recommended == "both"


def test_format_entries_handles_single_url_results() -> None:
    info = {"url": "https://archive.org/download/item/file.mp4", "ext": "mp4",
            "vcodec": "h264", "acodec": "aac", "format_id": "source",
            "filesize": 1024}
    formats, recommended = _format_entries(info, allow_merge=True)
    assert recommended == "source"
    assert formats[0].filesize_bytes == 1024


def test_format_entries_returns_nothing_for_unusable_info() -> None:
    formats, recommended = _format_entries({"formats": []}, allow_merge=True)
    assert formats == []
    assert recommended is None


def test_format_selector_merges_only_when_needed() -> None:
    assert _format_selector("137", is_progressive=True, allow_merge=True) == "137"
    merged = _format_selector("137", is_progressive=False, allow_merge=True)
    assert merged.startswith("137+bestaudio")
    assert _format_selector("137", is_progressive=False, allow_merge=False) == "137"


def test_default_format_selector_prefers_mp4() -> None:
    assert "bestvideo[ext=mp4]" in _format_selector(None, is_progressive=True, allow_merge=True)
    assert "best[ext=mp4]" in _format_selector(None, is_progressive=True, allow_merge=False)
