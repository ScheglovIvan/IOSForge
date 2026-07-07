"""App Store URL validation and parsing (SPEC §10 — source app reference).

Accepts an official Apple App Store product URL, verifies the host and extracts
the numeric app id plus the storefront country code. Only the id/country are
trusted downstream; the worker resolves name/description/screenshots from the
iTunes Lookup API using them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_ALLOWED_HOSTS = {"apps.apple.com", "itunes.apple.com"}
_APP_ID = re.compile(r"/id(\d+)")
_COUNTRY = re.compile(r"^[a-z]{2}$")


class AppStoreUrlError(Exception):
    """Raised when a string is not a usable App Store product URL."""


@dataclass
class ParsedAppStoreUrl:
    app_id: str
    country: str | None
    url: str


def parse(raw: str, *, country_default: str) -> ParsedAppStoreUrl:
    """Parse an App Store URL into (app_id, country, normalized url), or raise."""
    text = (raw or "").strip()
    if not text:
        raise AppStoreUrlError("App Store URL is required")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise AppStoreUrlError("URL must start with http:// or https://")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise AppStoreUrlError("URL must point to apps.apple.com or itunes.apple.com")
    match = _APP_ID.search(parsed.path)
    if match is None:
        raise AppStoreUrlError("URL must contain an app id (…/id<digits>)")
    app_id = match.group(1)

    country = country_default
    segments = [s for s in parsed.path.split("/") if s]
    if segments and _COUNTRY.match(segments[0].lower()):
        country = segments[0].lower()

    return ParsedAppStoreUrl(app_id=app_id, country=country, url=text)
