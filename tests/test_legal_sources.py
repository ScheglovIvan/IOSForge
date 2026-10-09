"""Legal-page facts read from the stored SwiftUI app (SDKs and usage descriptions)."""

from __future__ import annotations

from pathlib import Path

from iosforge.mvp import swiftui_integrations
from iosforge.worker import run_job


def test_sdks_follow_the_frozen_integrations(tmp_path: Path) -> None:
    assert run_job._app_sdks(tmp_path) == []
    swiftui_integrations.write(
        tmp_path, swiftui_integrations.Integrations(apphud_key="app_x", tenjin_key="T")
    )
    assert run_job._app_sdks(tmp_path) == ["apphud", "tenjin"]
    swiftui_integrations.write(tmp_path, swiftui_integrations.Integrations(tenjin_key="T"))
    assert run_job._app_sdks(tmp_path) == ["tenjin"]


def test_usage_descriptions_come_from_the_project(tmp_path: Path) -> None:
    assert run_job._usage_descriptions(tmp_path) == {}
    (tmp_path / "project.yml").write_text(
        "targets:\n  App:\n    info:\n      properties:\n"
        '        NSCameraUsageDescription: "Scan a document."\n'
        "        NSMicrophoneUsageDescription: Measure the noise level.\n"
        "        UILaunchScreen: {}\n"
    )
    assert run_job._usage_descriptions(tmp_path) == {
        "NSCameraUsageDescription": "Scan a document.",
        "NSMicrophoneUsageDescription": "Measure the noise level.",
    }


def test_vehicle_lookup_needs_the_word_vin() -> None:
    assert run_job._remote_endpoints({"screens": [{"name": "Driving tips"}]}) == []
    assert run_job._remote_endpoints({"screens": [{"name": "VIN decoder"}]})
