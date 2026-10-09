"""Resolve the locale the original app was captured in (Vision Judge pinning).

The Vision Judge compares generated screens against the ORIGINAL screenshots,
so the simulator must run in the same language/region (see
``providers.base.SIMULATOR_ENV_CONTRACT``). The locale is derived per job, never
hard-coded, by a fixed precedence chain:

1. capture manifest ``device.locale`` (the device locale at capture time — the
   most direct evidence; not emitted by the capture yet);
2. App Spec ``source_locale`` (inferred by analysis from the screenshot text);
3. App Store storefront ``country`` (a coarse default language per storefront);
4. otherwise :class:`LocaleUnresolved` — fail loud rather than judge across
   locales.
"""

from __future__ import annotations

import re
from typing import Any

from iosforge.mvp.spec_contract import SOURCE_LOCALE_PATTERN

STOREFRONT_LOCALES: dict[str, str] = {
    "us": "en-US",
    "gb": "en-GB",
    "au": "en-AU",
    "ca": "en-CA",
    "de": "de-DE",
    "fr": "fr-FR",
    "es": "es-ES",
    "it": "it-IT",
    "ru": "ru-RU",
    "jp": "ja-JP",
    "kr": "ko-KR",
    "br": "pt-BR",
    "cn": "zh-CN",
}

_LOCALE_RE = re.compile(SOURCE_LOCALE_PATTERN)


class LocaleUnresolved(RuntimeError):
    """Raised when no source of the original app's locale yields a usable tag."""


def _normalize(tag: str, *, origin: str) -> str:
    candidate = tag.strip().replace("_", "-")
    if not _LOCALE_RE.fullmatch(candidate):
        raise LocaleUnresolved(f"{origin} {tag!r} is not a BCP-47 language tag")
    return candidate


def _manifest_locale(manifest: dict[str, Any] | None) -> str | None:
    if not isinstance(manifest, dict):
        return None
    device = manifest.get("device")
    if not isinstance(device, dict):
        return None
    value = device.get("locale")
    if isinstance(value, str) and value.strip():
        return _normalize(value, origin="manifest device.locale")
    return None


def resolve_source_locale(
    spec: dict[str, Any],
    *,
    manifest: dict[str, Any] | None,
    storefront_country: str | None,
) -> str:
    """BCP-47 locale of the original screenshots: manifest → spec → storefront.

    The capture manifest's ``device.locale`` wins (iOS ``en_US`` identifiers are
    normalised to ``en-US``); then the spec's ``source_locale``; then the default
    language of the App Store storefront ``country``. A present but malformed
    value raises instead of silently falling through. Raises
    :class:`LocaleUnresolved` when nothing resolves.
    """
    from_manifest = _manifest_locale(manifest)
    if from_manifest is not None:
        return from_manifest
    spec_locale = spec.get("source_locale") if isinstance(spec, dict) else None
    if isinstance(spec_locale, str) and spec_locale.strip():
        return _normalize(spec_locale, origin="app_spec source_locale")
    if storefront_country:
        mapped = STOREFRONT_LOCALES.get(storefront_country.strip().lower())
        if mapped is not None:
            return mapped
        raise LocaleUnresolved(
            f"no source_locale in the spec or manifest and storefront country "
            f"{storefront_country!r} has no default language mapping"
        )
    raise LocaleUnresolved(
        "cannot resolve the original app locale: no manifest device.locale, "
        "no app_spec source_locale and no storefront country"
    )
