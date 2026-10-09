"""Phase 4 iOS render path of the Vision Judge (simulator mocked; scoring untouched)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import compliance, simulator, xcode
from iosforge.mvp.paths import RunPaths

PROJECT = "name: SpeakerTest\ntargets:\n  SpeakerTest:\n    settings:\n      base:\n" \
    "        PRODUCT_BUNDLE_IDENTIFIER: com.example.speaker\n"  # fmt: skip


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path / "runs")
    rp.xcode_app.mkdir()
    (rp.xcode_app / "project.yml").write_text(PROJECT)
    rp.screens_json.write_text(json.dumps({"screens": [{"id": "0001"}, {"id": "0011"}]}))
    rp.ads_raw_json.write_text(
        json.dumps(
            {
                "ad_events": [
                    {"provider": "Google AdMob", "format": "interstitial", "screen": "0001"}
                ]
            }
        )
    )
    rp.app_spec_json.write_text(
        json.dumps(
            {
                "screens": [
                    {
                        "id": "0011",
                        "layout_notes": "Light background. The native-ad banner at the top "
                        "is removed; routine card moves up. Loading ads… overlay gone.",
                        "components": [
                            {
                                "type": "text",
                                "role": "disclaimer",
                                "data": "This action may contain Ads",
                            },
                            {"type": "card", "role": "routine"},
                        ],
                    }
                ]
            }
        )
    )
    return rp


def test_judge_prompt_is_ios_and_ignores_removed_ads() -> None:
    prompt = compliance.JUDGE_PROMPT
    assert "Flutter" not in prompt and "Android" not in prompt
    assert "native iOS (SwiftUI)" in prompt and "-screen-id <id>" in prompt
    assert "removed_ads.json" in prompt
    assert "never lower\n`structure_score` for their absence" in prompt
    assert "structure_score" in prompt and "divergence_score" in prompt and "{ids}" in prompt


def test_removed_ads_collects_facts_per_screen(paths: RunPaths) -> None:
    facts = compliance.removed_ads(paths)
    assert facts["0001"] == ["captured ad: Google AdMob interstitial"]
    assert any(f.startswith("ad component: text / disclaimer") for f in facts["0011"])
    notes = [f for f in facts["0011"] if f.startswith("analysis note:")]
    assert notes == [
        "analysis note: The native-ad banner at the top is removed;",
        "analysis note: Loading ads… overlay gone.",
    ]


def test_render_generated_ios_writes_the_same_contract(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(simulator, "pin_environment", lambda env: calls.append("pin"))
    monkeypatch.setattr(xcode, "install", lambda udid, app: calls.append("install"))

    def render(env: Any, bundle: str, sid: str, out: Path) -> simulator.StableShot:
        calls.append(f"{bundle}:{sid}")
        out.write_bytes(b"png")
        return simulator.StableShot(out, 3, 0.0012)

    monkeypatch.setattr(simulator, "render_screen", render)
    env = simulator.SimEnvironment(udid="U", locale="en-US")

    result = compliance.render_generated_ios(paths, env, app=Path("/tmp/App.app"))

    assert calls == ["pin", "install", "com.example.speaker:0001", "com.example.speaker:0011"]
    written = json.loads(paths.generated_screens_json.read_text())
    assert written == result
    assert written["package"] == "ios" and written["locale"] == "en-US"
    assert written["screens"][0] == {
        "id": "0001",
        "screenshot": "generated_screens/0001.png",
        "frames": 3,
        "frame_diff": 0.0012,
        "blank": False,
    }
    matches = compliance.match_screens([{"id": "0001"}, {"id": "0011"}], written["screens"])
    assert matches == [
        ("0001", "generated_screens/0001.png"),
        ("0011", "generated_screens/0011.png"),
    ]
    ads = json.loads((paths.claude_ws / compliance.REMOVED_ADS_JSON).read_text())
    assert ads["0001"] == ["captured ad: Google AdMob interstitial"]


def test_render_generated_ios_fails_loudly_without_toolchain(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    env = simulator.SimEnvironment(udid="U", locale="en-US")
    with pytest.raises(simulator.SimulatorUnavailable):
        compliance.render_generated_ios(paths, env, app=Path("/tmp/App.app"))
    with pytest.raises(simulator.SimulatorUnavailable):
        compliance.build_ios(paths)


def test_build_ios_raises_with_build_errors(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(xcode, "generate_project", lambda app_dir: None)
    monkeypatch.setattr(
        xcode,
        "build",
        lambda *a, **k: xcode.BuildOutcome(False, ["App/X.swift:1:1: error: boom"], "log"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        compliance.build_ios(paths)
    assert (paths.run_dir / "xcodebuild.log").read_text() == "log"


def _first_iphone() -> str | None:
    if not xcode.toolchain_available():
        return None
    out = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "-j"],
        capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    for runtime, devices in json.loads(out or "{}").get("devices", {}).items():
        if "iOS" in runtime:
            for device in devices:
                if device.get("name", "").startswith("iPhone"):
                    return str(device["udid"])
    return None


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_reference_app_renders_on_the_simulator(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path / "runs")
    reference = Path(__file__).resolve().parents[1] / "docs" / "swiftui-reference"
    import shutil

    shutil.copytree(reference, rp.xcode_app)
    udid = _first_iphone()
    assert udid is not None
    env = simulator.SimEnvironment(udid=udid, locale="en-US")

    app = compliance.build_ios(rp)
    result = compliance.render_generated_ios(rp, env, app=app, screen_ids=["detail", "paywall"])

    assert [s["id"] for s in result["screens"]] == ["detail", "paywall"]
    for screen in result["screens"]:
        assert screen["frame_diff"] < simulator.STABLE_FRAME_MAX_DIFF and not screen["blank"]
        assert (rp.run_dir / screen["screenshot"]).stat().st_size > 10_000
