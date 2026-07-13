"""Tests for RevenueCat v2 provisioning (mocked httpx — no live API)."""

from __future__ import annotations

from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import revenuecat_provision as rc


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "revenuecat_provision": True,
        "revenuecat_api_key": "sk_test",
        "revenuecat_project_id": "proj_test",
        "revenuecat_test_store_key": "",  # explicit: don't inherit a real key from .env
        "codemagic_bundle_prefix": "com.batteam",
    }
    base.update(over)
    return Settings(**base)


_SPEC = {
    "app_name": "Sounds for CarPlay",
    "monetization": {
        "packages": [
            {"name": "Weekly", "price": "$4.99", "period": "week"},
            {"name": "Lifetime", "price": "$29.99"},
        ]
    },
}


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._p = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._p


class _FakeClient:
    def __init__(self, *a: Any, **k: Any) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        _FakeClient.last = self

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def post(self, url: str, json: Any = None) -> _Resp:
        self.calls.append(("POST", url, json))
        if url.endswith("/apps"):
            return _Resp({"id": "app123", "app_store": {"custom_url_scheme": "rc-x"}})
        if "/entitlements" in url and "attach" not in url:
            return _Resp({"id": "ent123"})
        if "/products" in url:
            return _Resp({"id": "prod_" + str(len(self.calls))})
        if url.endswith("/offerings"):
            return _Resp({"id": "off123"})
        return _Resp({"id": "x"})

    def get(self, url: str) -> _Resp:
        self.calls.append(("GET", url, None))
        return _Resp({"items": [{"key": "appl_PUBLICKEY"}]})


def test_provision_creates_app_and_returns_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.httpx, "Client", _FakeClient)
    out = rc.provision(_SPEC, settings=_settings())
    assert out is not None
    assert out["app_id"] == "app123"
    assert out["apple_public_key"] == "appl_PUBLICKEY"
    assert out["sdk_key"] == "appl_PUBLICKEY"  # no test store → per-clone key
    assert out["mode"] == "app_store"
    assert out["bundle_id"] == "com.batteam.soundsforcarplay"
    assert out["entitlement"] == "premium_sounds-for-carplay"
    assert out["offering"] == "default_sounds-for-carplay"
    assert len(out["products"]) == 2
    urls = [u for _, u, _ in _FakeClient.last.calls]
    assert any(u.endswith("/apps") for u in urls)
    assert any("/public_api_keys" in u for u in urls)
    assert any(u.endswith("/entitlements") for u in urls)
    assert any(u.endswith("/offerings") for u in urls)


def test_provision_uses_test_store_key_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.httpx, "Client", _FakeClient)
    out = rc.provision(_SPEC, settings=_settings(revenuecat_test_store_key="test_KEY"))
    assert out is not None
    assert out["sdk_key"] == "test_KEY"  # testable now, no Apple signing
    assert out["mode"] == "test_store"
    assert out["apple_public_key"] == "appl_PUBLICKEY"  # per-clone key still provisioned


def test_product_store_ids_are_appstore_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.httpx, "Client", _FakeClient)
    spec = {
        "app_name": "X",
        "monetization": {"packages": [{"name": "Weekly Pro", "period": "week"}]},
    }
    out = rc.provision(spec, settings=_settings())
    assert out is not None
    # App Store product ids allow alphanumerics/underscore/period only — no hyphens
    for store_id in out["products"]:
        assert "-" not in store_id
    assert "weekly_pro" in out["products"][0]
    assert rc._store_slug("Weekly Pro!") == "weekly_pro"


def test_provision_disabled_returns_none() -> None:
    assert rc.provision(_SPEC, settings=_settings(revenuecat_provision=False)) is None


def test_provision_no_key_or_project_returns_none() -> None:
    assert rc.provision(_SPEC, settings=_settings(revenuecat_api_key="")) is None
    assert rc.provision(_SPEC, settings=_settings(revenuecat_project_id="")) is None


def test_provision_without_packages_still_makes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.httpx, "Client", _FakeClient)
    out = rc.provision({"app_name": "Tiny"}, settings=_settings())
    assert out is not None
    assert out["app_id"] == "app123"
    assert out["products"] == []
    assert out["entitlement"] == ""


def test_provision_app_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient(_FakeClient):
        def post(self, url: str, json: Any = None) -> _Resp:
            if url.endswith("/apps"):
                return _Resp({}, status=401)
            return super().post(url, json)

    monkeypatch.setattr(rc.httpx, "Client", _BoomClient)
    assert rc.provision(_SPEC, settings=_settings()) is None
