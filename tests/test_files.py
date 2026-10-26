"""files service — pure helpers (no DB)."""

import re

from app.modules.files.service import content_disposition


def test_content_disposition_none() -> None:
    assert content_disposition(None) is None
    assert content_disposition("") is None


def test_content_disposition_cyrillic_is_latin1_safe() -> None:
    """A Cyrillic filename used to crash the response (UnicodeEncodeError on header encoding)."""
    header = content_disposition("Паразитология.jpg")
    assert header is not None
    header.encode("latin-1")  # must not raise
    assert "filename*=UTF-8''" in header  # original name travels RFC 5987-encoded
    assert "parazitologiya" in header  # transliterated ASCII fallback


def test_content_disposition_fallback_is_slugified_ascii() -> None:
    header = content_disposition("Muhr №3 (klinika).png")
    assert header is not None
    header.encode("latin-1")
    fallback = re.search(r'filename="([^"]*)"', header)
    assert fallback is not None
    assert re.fullmatch(r"[a-z0-9-]+", fallback.group(1))  # no spaces / quotes / dots inside


def test_content_disposition_neutralises_header_injection() -> None:
    header = content_disposition('muhr; drop "table"')
    assert header is not None
    header.encode("latin-1")
    fallback = re.search(r'filename="([^"]*)"', header)
    assert fallback is not None and ';' not in fallback.group(1) and '"' not in fallback.group(1)
