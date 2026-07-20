"""Tests for Apphud config passthrough (no remote calls — dashboard owns setup)."""

from __future__ import annotations

from typing import Any

from iosforge.common.config import Settings
from iosforge.mvp import apphud_provision as ap


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "apphud_provision": True,
        "apphud_api_key": "app_test",
        "apphud_secrets_path": "",  # isolate from the real gitignored secrets file
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


def test_provision_returns_sdk_key_and_derived_products() -> None:
    out = ap.provision(_SPEC, settings=_settings())
    assert out is not None
    assert out["sdk_key"] == "app_test"
    assert out["bundle_id"] == "com.batteam.soundsforcarplay"
    assert out["products"] == [
        "com.batteam.soundsforcarplay.weekly",
        "com.batteam.soundsforcarplay.lifetime",
    ]
    assert out["placement"] == "main_sounds-for-carplay"


def test_sandbox_flag_sets_mode() -> None:
    assert ap.provision(_SPEC, settings=_settings(), sandbox=True)["mode"] == "sandbox"
    assert ap.provision(_SPEC, settings=_settings(), sandbox=False)["mode"] == "production"


def test_placement_override_from_settings() -> None:
    out = ap.provision(_SPEC, settings=_settings(apphud_placement="paywall_a"))
    assert out["placement"] == "paywall_a"


def test_bundle_and_name_overrides() -> None:
    out = ap.provision(_SPEC, settings=_settings(), bundle_id="com.acme.demo", app_name="Demo App")
    assert out["bundle_id"] == "com.acme.demo"
    assert out["products"] == ["com.acme.demo.weekly", "com.acme.demo.lifetime"]


def test_no_packages_yields_empty_placement_and_products() -> None:
    out = ap.provision({"app_name": "X", "monetization": {}}, settings=_settings())
    assert out["products"] == []
    assert out["placement"] == ""


def test_disabled_or_unconfigured_returns_none() -> None:
    assert ap.provision(_SPEC, settings=_settings(apphud_provision=False)) is None
    # no key anywhere (empty field + missing secrets file) → skipped
    assert (
        ap.provision(_SPEC, settings=_settings(apphud_api_key="", apphud_secrets_path="")) is None
    )


def test_load_key_from_secrets_file(tmp_path: Any) -> None:
    f = tmp_path / "apphud.env"
    f.write_text('APPHUD_API_KEY="appstr_fromfile"\n')
    s = _settings(apphud_api_key="", apphud_secrets_path=str(f))
    assert ap.load_key(s) == "appstr_fromfile"
    assert ap.provision(_SPEC, settings=s)["sdk_key"] == "appstr_fromfile"


def test_per_job_api_key_override_wins() -> None:
    out = ap.provision(_SPEC, settings=_settings(), api_key="appstr_perjob")
    assert out["sdk_key"] == "appstr_perjob"
