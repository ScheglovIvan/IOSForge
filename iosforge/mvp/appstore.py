"""App Store metadata resolver via the iTunes Lookup API (MVP).

Given an app id + storefront country, fetches the product's name, description and
screenshot URLs, and downloads the screenshot bytes. Kept inline in ``mvp`` as an
MVP debt: per SPEC §5/§9 an external source should be a registered provider — this
module is the promotion candidate. All network access lives here so callers (the
worker WALKTHROUGH stage) can monkeypatch it in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


class AppStoreFetchError(Exception):
    """Raised when the App Store lookup fails or returns no matching app."""


class _ScreenshotTooLarge(Exception):
    """Internal: a screenshot response exceeded the byte cap and was skipped."""


@dataclass
class AppStoreMetadata:
    app_id: str
    country: str
    track_name: str | None = None
    description: str | None = None
    seller_name: str | None = None
    bundle_id: str | None = None
    artwork_url: str | None = None
    screenshot_urls: list[str] = field(default_factory=list)


def fetch_metadata(
    app_id: str,
    country: str,
    *,
    api_base: str,
    timeout: float,
    max_screenshots: int,
) -> AppStoreMetadata:
    """Resolve App Store metadata for an app id, or raise ``AppStoreFetchError``."""
    url = f"{api_base.rstrip('/')}/lookup"
    try:
        resp = httpx.get(url, params={"id": app_id, "country": country}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppStoreFetchError(f"App Store lookup failed: {exc}") from exc

    results = payload.get("results") or []
    if not results:
        raise AppStoreFetchError(f"no App Store app found for id {app_id} ({country})")
    item = results[0]

    shots: list[str] = []
    for kfield in ("screenshotUrls", "ipadScreenshotUrls"):
        for u in item.get(kfield) or []:
            if u and u not in shots:
                shots.append(u)
    if max_screenshots > 0:
        shots = shots[:max_screenshots]

    return AppStoreMetadata(
        app_id=app_id,
        country=country,
        track_name=item.get("trackName"),
        description=item.get("description"),
        seller_name=item.get("sellerName"),
        bundle_id=item.get("bundleId"),
        artwork_url=item.get("artworkUrl512") or item.get("artworkUrl100"),
        screenshot_urls=shots,
    )


def download_screenshots(urls: list[str], *, timeout: float) -> list[tuple[str, bytes]]:
    """Download screenshot bytes; skips any that fail or exceed the size cap.

    Streams each response with a hard byte cap so a hostile/oversized body from
    the (external) URL is never fully buffered. Returns (filename, bytes).
    """
    out: list[tuple[str, bytes]] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for i, u in enumerate(urls):
            try:
                with client.stream("GET", u) as r:
                    r.raise_for_status()
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > _MAX_SCREENSHOT_BYTES:
                            raise _ScreenshotTooLarge
                    out.append((f"{i:04d}.png", bytes(body)))
            except (httpx.HTTPError, _ScreenshotTooLarge):
                continue
    return out
