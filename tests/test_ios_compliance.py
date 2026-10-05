"""Per-job export-compliance staging + boolean Info.plist injection."""

from __future__ import annotations

import json
from pathlib import Path

from iosforge.mvp import codemagic_integration as cm
from iosforge.mvp import ios_compliance

_KEY = "ITSAppUsesNonExemptEncryption"


def test_stage_writes_false_when_exempt(tmp_path: Path) -> None:
    assert ios_compliance.stage(tmp_path, True) is True
    perms = json.loads((tmp_path / "ios_permissions.json").read_text())
    assert perms[_KEY] is False


def test_stage_is_noop_when_not_exempt(tmp_path: Path) -> None:
    assert ios_compliance.stage(tmp_path, False) is False
    assert ios_compliance.stage(tmp_path, None) is False
    assert not (tmp_path / "ios_permissions.json").exists()


def test_stage_merges_without_clobbering_existing_keys(tmp_path: Path) -> None:
    perms_file = tmp_path / "ios_permissions.json"
    perms_file.write_text(json.dumps({"NSUserTrackingUsageDescription": "why"}))
    ios_compliance.stage(tmp_path, True)
    perms = json.loads(perms_file.read_text())
    assert perms["NSUserTrackingUsageDescription"] == "why"
    assert perms[_KEY] is False


def test_injector_handles_booleans_in_both_templates() -> None:
    # the Info.plist injector must emit a PlistBuddy `bool`, not a string, or App Store
    # Connect ignores ITSAppUsesNonExemptEncryption and still shows "Missing Compliance".
    for template in (cm._CODEMAGIC_YAML, cm._CODEMAGIC_YAML_SIGNED):
        assert "isinstance(value, bool)" in template
        assert "bool {'true' if value else 'false'}" in template
