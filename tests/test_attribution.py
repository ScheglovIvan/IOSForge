"""Attribution (Tenjin) config passthrough + iOS asset staging."""

from __future__ import annotations

import json
import plistlib
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


def test_stage_merges_att_into_existing_permissions(tmp_path: Path) -> None:
    app = tmp_path / "flutter_app"
    app.mkdir()
    # the generator already wrote permissions — ATT must be added, not clobber them
    (app / "ios_permissions.json").write_text(json.dumps({"NSCameraUsageDescription": "cam"}))
    cfg = at.provision(settings=_settings())

    staged = at.stage_ios_assets(_settings(), app, cfg)

    perms = json.loads((app / "ios_permissions.json").read_text())
    assert perms["NSCameraUsageDescription"] == "cam"  # preserved
    assert perms["NSUserTrackingUsageDescription"]  # ATT added
    assert perms["NSAdvertisingAttributionReportEndpoint"] == "https://tenjin-skan.com"
    assert staged["att"] is True
    assert staged["skan_endpoint"] is True
    assert staged["skadnetwork"] is False  # no plist configured


def test_stage_writes_permissions_when_file_absent(tmp_path: Path) -> None:
    app = tmp_path / "flutter_app"
    app.mkdir()
    at.stage_ios_assets(_settings(), app, at.provision(settings=_settings()))
    perms = json.loads((app / "ios_permissions.json").read_text())
    assert "NSUserTrackingUsageDescription" in perms


def test_stage_copies_skadnetwork_plist(tmp_path: Path) -> None:
    src = tmp_path / "skan.plist"
    items = [{"SKAdNetworkIdentifier": "abc123.skadnetwork"}]
    src.write_bytes(plistlib.dumps({"SKAdNetworkItems": items}))
    app = tmp_path / "flutter_app"
    app.mkdir()
    s = _settings(skadnetwork_ids_path=str(src))

    staged = at.stage_ios_assets(s, app, at.provision(settings=s))

    assert staged["skadnetwork"] is True
    copied = plistlib.loads((app / "skadnetwork_ids.plist").read_bytes())
    assert copied["SKAdNetworkItems"] == items
