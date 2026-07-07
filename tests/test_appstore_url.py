"""App Store URL parsing tests."""

from __future__ import annotations

import pytest

from iosforge.admin.appstore_url import AppStoreUrlError, parse


def test_parses_full_url_with_country() -> None:
    r = parse(
        "https://apps.apple.com/us/app/instagram/id389801252?mt=8",
        country_default="ru",
    )
    assert r.app_id == "389801252"
    assert r.country == "us"


def test_parses_short_url_without_country_uses_default() -> None:
    r = parse("https://apps.apple.com/app/id389801252", country_default="ru")
    assert r.app_id == "389801252"
    assert r.country == "ru"


def test_accepts_itunes_host() -> None:
    r = parse("https://itunes.apple.com/gb/app/x/id42", country_default="us")
    assert r.app_id == "42"
    assert r.country == "gb"


def test_rejects_empty() -> None:
    with pytest.raises(AppStoreUrlError):
        parse("   ", country_default="us")


def test_rejects_foreign_host() -> None:
    with pytest.raises(AppStoreUrlError):
        parse("https://play.google.com/store/apps/details?id=com.x", country_default="us")


def test_rejects_missing_app_id() -> None:
    with pytest.raises(AppStoreUrlError):
        parse("https://apps.apple.com/us/app/instagram", country_default="us")


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(AppStoreUrlError):
        parse("ftp://apps.apple.com/app/id1", country_default="us")
