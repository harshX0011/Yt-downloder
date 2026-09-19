"""The SSRF guard, filename hygiene and the rate limiter."""

from __future__ import annotations

import pytest

from backend.security import (
    RateLimiter,
    UrlRejected,
    content_disposition,
    safe_filename,
    validate_remote_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/file.mp4",
        "http://localhost/file.mp4",
        "http://[::1]/file.mp4",
        "http://10.0.0.5/file.mp4",
        "http://192.168.1.10/file.mp4",
        "http://172.16.3.4/file.mp4",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://[::ffff:127.0.0.1]/file.mp4",  # IPv4-mapped loopback
        "http://0.0.0.0/file.mp4",
    ],
)
def test_private_and_loopback_targets_are_rejected(url: str) -> None:
    with pytest.raises(UrlRejected) as excinfo:
        validate_remote_url(url)
    assert excinfo.value.code in {"url_private_address", "dns_failed"}


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("file:///etc/passwd", "url_bad_scheme"),
        ("ftp://example.org/video.mp4", "url_bad_scheme"),
        ("gopher://example.org/", "url_bad_scheme"),
        ("javascript:alert(1)", "url_bad_scheme"),
        ("example.org/video.mp4", "url_no_scheme"),
        ("https://user:secret@example.org/v.mp4", "url_has_credentials"),
        ("https://example.org:8080/v.mp4", "url_bad_port"),
        ("https://", "url_no_host"),
        ("", "url_empty"),
        ("   ", "url_empty"),
    ],
)
def test_malformed_or_unsupported_urls_are_rejected(url: str, code: str) -> None:
    with pytest.raises(UrlRejected) as excinfo:
        validate_remote_url(url)
    assert excinfo.value.code == code


def test_overlong_url_is_rejected() -> None:
    with pytest.raises(UrlRejected) as excinfo:
        validate_remote_url("https://example.org/" + "a" * 3000)
    assert excinfo.value.code == "url_too_long"


def test_crlf_injection_is_rejected() -> None:
    with pytest.raises(UrlRejected) as excinfo:
        validate_remote_url("https://example.org/a\r\nHost: evil")
    assert excinfo.value.code == "url_invalid"


def test_public_literal_address_is_accepted() -> None:
    validated = validate_remote_url("https://93.184.216.34/video.mp4")
    assert validated.hostname == "93.184.216.34"
    assert validated.port == 443


def test_allow_private_skips_resolution() -> None:
    validated = validate_remote_url("http://127.0.0.1/v.mp4", allow_private=True)
    assert validated.hostname == "127.0.0.1"
    assert validated.addresses == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal name.mp4", "normal name.mp4"),
        ("../../etc/passwd", "etc passwd"),
        ("with/slash\\and:colon", "with slash and colon"),
        ('quo"tes<>|?*', "quo tes"),
        ("   .hidden.   ", "hidden"),
        ("", "download"),
        ("CON.mp4", "_CON.mp4"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_safe_filename_truncates() -> None:
    assert len(safe_filename("x" * 400)) == 120


def test_content_disposition_handles_non_ascii() -> None:
    header = content_disposition("वीडियो.mp4")
    assert header.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in header


@pytest.mark.asyncio
async def test_rate_limiter_blocks_then_reports_retry_after() -> None:
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert await limiter.check("ip-a") == (True, 0)
    assert await limiter.check("ip-a") == (True, 0)

    allowed, retry_after = await limiter.check("ip-a")
    assert allowed is False
    assert retry_after > 0

    # Buckets are per key.
    assert await limiter.check("ip-b") == (True, 0)
