"""App Store metadata resolver tests (network mocked)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from iosforge.mvp import appstore


class _FakeResp:
    def __init__(self, payload: dict[str, Any] | None = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        assert self._payload is not None
        return self._payload


def test_fetch_metadata_parses_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackName": "Example",
                "description": "An example app",
                "sellerName": "Example Inc",
                "bundleId": "com.example.app",
                "artworkUrl512": "https://x/art.png",
                "screenshotUrls": ["https://x/1.png", "https://x/2.png"],
                "ipadScreenshotUrls": ["https://x/2.png", "https://x/pad.png"],
            }
        ],
    }
    monkeypatch.setattr(appstore.httpx, "get", lambda *a, **k: _FakeResp(payload))

    meta = appstore.fetch_metadata(
        "42", "us", api_base="https://itunes.apple.com", timeout=5.0, max_screenshots=10
    )
    assert meta.track_name == "Example"
    assert meta.bundle_id == "com.example.app"
    assert meta.artwork_url == "https://x/art.png"
    # De-duplicated across iPhone + iPad lists, order preserved.
    assert meta.screenshot_urls == ["https://x/1.png", "https://x/2.png", "https://x/pad.png"]


def test_fetch_metadata_respects_max(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"results": [{"screenshotUrls": ["a", "b", "c", "d"]}]}
    monkeypatch.setattr(appstore.httpx, "get", lambda *a, **k: _FakeResp(payload))
    meta = appstore.fetch_metadata(
        "42", "us", api_base="https://itunes.apple.com", timeout=5.0, max_screenshots=2
    )
    assert meta.screenshot_urls == ["a", "b"]


def test_fetch_metadata_raises_when_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appstore.httpx, "get", lambda *a, **k: _FakeResp({"results": []}))
    with pytest.raises(appstore.AppStoreFetchError):
        appstore.fetch_metadata(
            "42", "us", api_base="https://itunes.apple.com", timeout=5.0, max_screenshots=10
        )


def test_fetch_metadata_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> _FakeResp:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(appstore.httpx, "get", _boom)
    with pytest.raises(appstore.AppStoreFetchError):
        appstore.fetch_metadata(
            "42", "us", api_base="https://itunes.apple.com", timeout=5.0, max_screenshots=10
        )


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> list[bytes]:
        return self._chunks


class _FakeClient:
    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def stream(self, _method: str, u: str) -> _FakeStream:
        if "bad" in u:
            raise httpx.ConnectError("nope")
        if "big" in u:
            return _FakeStream([b"\x00" * (30 * 1024 * 1024)])
        return _FakeStream([b"\x89PNG", b"data"])


def test_download_screenshots_skips_failures_and_oversized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(appstore.httpx, "Client", lambda *a, **k: _FakeClient())
    out = appstore.download_screenshots(
        [
            "https://x/ok.png",
            "https://x/bad.png",
            "https://x/big.png",
            "https://x/ok2.png",
        ],
        timeout=5.0,
    )
    assert [name for name, _ in out] == ["0000.png", "0003.png"]
    assert all(data == b"\x89PNGdata" for _, data in out)
