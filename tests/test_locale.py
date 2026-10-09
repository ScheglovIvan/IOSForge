"""Tests for the original-app locale resolution chain (Vision Judge pinning)."""

from __future__ import annotations

from typing import Any

import pytest

from iosforge.mvp.locale import STOREFRONT_LOCALES, LocaleUnresolved, resolve_source_locale


def _spec(locale: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"app_name": "Speaker Cleaner"}
    if locale is not None:
        spec["source_locale"] = locale
    return spec


def test_manifest_device_locale_wins() -> None:
    manifest = {"device": {"model": "iPhone15,2", "locale": "de_DE"}}
    got = resolve_source_locale(_spec("en-US"), manifest=manifest, storefront_country="ru")
    assert got == "de-DE"


def test_spec_source_locale_used_without_manifest_locale() -> None:
    assert resolve_source_locale(_spec("ru-RU"), manifest=None, storefront_country="us") == "ru-RU"


def test_legacy_string_device_in_manifest_is_ignored() -> None:
    manifest = {"device": "iPhone 15 Pro"}
    assert resolve_source_locale(_spec("de"), manifest=manifest, storefront_country=None) == "de"


def test_storefront_country_fallback() -> None:
    assert resolve_source_locale(_spec(), manifest={}, storefront_country="JP") == "ja-JP"
    assert resolve_source_locale(_spec(), manifest=None, storefront_country="gb") == "en-GB"


def test_storefront_table_values_are_valid_tags() -> None:
    for tag in STOREFRONT_LOCALES.values():
        assert resolve_source_locale(_spec(tag), manifest=None, storefront_country=None) == tag


def test_unknown_storefront_country_raises() -> None:
    with pytest.raises(LocaleUnresolved, match="no default language mapping"):
        resolve_source_locale(_spec(), manifest=None, storefront_country="zz")


def test_nothing_to_resolve_raises() -> None:
    with pytest.raises(LocaleUnresolved, match="cannot resolve"):
        resolve_source_locale(_spec(), manifest=None, storefront_country=None)


def test_malformed_manifest_locale_raises() -> None:
    manifest = {"device": {"locale": "English"}}
    with pytest.raises(LocaleUnresolved, match="manifest device.locale"):
        resolve_source_locale(_spec("en-US"), manifest=manifest, storefront_country="us")
