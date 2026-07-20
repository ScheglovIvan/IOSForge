"""Tests for per-job build profile / identity resolution."""

from __future__ import annotations

from typing import Any

from iosforge.common.config import Settings
from iosforge.mvp import build_profile as bp


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "codemagic_bundle_prefix": "com.batteam",
    }
    base.update(over)
    return Settings(**base)


def test_default_profile_is_test_with_derived_bundle_and_spec_name() -> None:
    ident = bp.resolve_identity({}, {"app_name": "Sounds for CarPlay"}, _settings())
    assert ident.profile == "test"
    assert ident.app_name == "Sounds for CarPlay"
    assert ident.bundle_id == "com.batteam.soundsforcarplay"
    assert ident.sandbox is True  # test profile → Apphud sandbox mode


def test_real_profile_uses_overrides_and_production_mode() -> None:
    meta = {
        "build_profile": "real",
        "override_bundle_id": "com.acme.demo",
        "override_app_name": "Demo App",
    }
    ident = bp.resolve_identity(meta, {"app_name": "Ignored"}, _settings())
    assert ident.profile == "real"
    assert ident.bundle_id == "com.acme.demo"  # exact override, not derived
    assert ident.app_name == "Demo App"
    assert ident.sandbox is False  # real profile → Apphud production mode


def test_real_profile_without_override_derives_bundle() -> None:
    ident = bp.resolve_identity({"build_profile": "real"}, {"app_name": "My Cool App"}, _settings())
    assert ident.bundle_id == "com.batteam.mycoolapp"


def test_unknown_profile_falls_back_to_test() -> None:
    ident = bp.resolve_identity({"build_profile": "weird"}, {"app_name": "X"}, _settings())
    assert ident.profile == "test"


def test_missing_metadata_and_spec_are_safe() -> None:
    ident = bp.resolve_identity(None, None, _settings())
    assert ident.profile == "test"
    assert ident.app_name == "App"
    assert ident.bundle_id == "com.batteam.app"
