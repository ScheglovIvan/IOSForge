"""Attribution (Tenjin) config passthrough + iOS asset staging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.mvp import attribution as at


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "attribution_provision": True,
        "tenjin_api_key": "tj_test",
        "tenjin_secrets_path": "",  # isolate from the real gitignored secrets file
        "skadnetwork_ids_path": "",
    }
    base.update(over)
    return Settings(**base)


def test_provision_returns_provider_and_key() -> None:
    out = at.provision(settings=_settings())
    assert out is not None
    assert out["provider"] == "tenjin"
    assert out["sdk_key"] == "tj_test"
    assert out["att_usage_description"]  # ATT copy always travels with the config
    # without this endpoint in Info.plist Apple never forwards SKAN postbacks to Tenjin
    assert out["skan_report_endpoint"] == "https://tenjin-skan.com"


def test_disabled_or_unconfigured_returns_none() -> None:
    assert at.provision(settings=_settings(attribution_provision=False)) is None
    assert at.provision(settings=_settings(tenjin_api_key="")) is None


def test_load_key_from_secrets_file(tmp_path: Path) -> None:
    f = tmp_path / "tenjin.env"
    f.write_text('TENJIN_API_KEY="tj_fromfile"\n')
    s = _settings(tenjin_api_key="", tenjin_secrets_path=str(f))
    assert at.load_key(s) == "tj_fromfile"
    assert at.provision(settings=s)["sdk_key"] == "tj_fromfile"


def test_per_job_api_key_override_wins() -> None:
    assert at.provision(settings=_settings(), api_key="tj_perjob")["sdk_key"] == "tj_perjob"
