"""Wiring of the Frida v1.1 archive context (rich source / fonts / media) into
the analyze and codegen workspaces."""

from __future__ import annotations

import json
from pathlib import Path

from iosforge.mvp import analyze, claude_gen
from iosforge.mvp.analyze import ANALYZE_PROMPT, stage_archive_context
from iosforge.mvp.paths import RunPaths

_TTF = b"\x00\x01\x00\x00ttf"
_MP4 = b"\x00\x00\x00\x18ftypmp42"


def _seed_archive(rp: RunPaths) -> None:
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.source_dir.mkdir(parents=True, exist_ok=True)
    (rp.source_dir / "0000.json").write_text(
        json.dumps(
            {
                "scale": 3,
                "screen_size": {"w": 393, "h": 852},
                "root": {
                    "class": "UIImageView",
                    "frame": {"x": 0, "y": 0, "w": 393, "h": 852},
                    "kind": "video",
                    "node_id": "n1",
                    "asset_ref": {"media_id": "m1", "resolved": True},
                    "colors": {"background": "#0B0B0FFF"},
                },
            }
        )
    )
    rp.fonts_json.write_text(
        json.dumps(
            [
                {
                    "postscript_name": "Urbanist-Bold",
                    "family": "Urbanist",
                    "file": "fonts/Urbanist-Bold.ttf",
                    "is_system": False,
                }
            ]
        )
    )
    rp.fonts_dir.mkdir(parents=True, exist_ok=True)
    (rp.fonts_dir / "Urbanist-Bold.ttf").write_bytes(_TTF)
    rp.media_json.write_text(
        json.dumps(
            [
                {
                    "id": "m1",
                    "source": "bundle",
                    "kind": "video",
                    "path": "media/splash.mp4",
                    "role": "splash_background",
                }
            ]
        )
    )
    rp.media_dir.mkdir(parents=True, exist_ok=True)
    (rp.media_dir / "splash.mp4").write_bytes(_MP4)


def test_stage_archive_context_metadata_only(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    _seed_archive(rp)
    ws = rp.claude_ws
    ws.mkdir(parents=True, exist_ok=True)

    summary = stage_archive_context(rp, ws, include_bytes=False)

    assert summary == {"source": 1, "fonts": 1, "media": 1}
    assert (ws / "source" / "0000.json").is_file()
    assert (ws / "fonts.json").is_file() and (ws / "media.json").is_file()
    # metadata pass must NOT drag the heavy byte files into the workspace
    assert not (ws / "fonts").exists()
    assert not (ws / "media").exists()


def test_stage_archive_context_with_bytes(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    _seed_archive(rp)
    ws = rp.claude_ws
    ws.mkdir(parents=True, exist_ok=True)

    stage_archive_context(rp, ws, include_bytes=True)

    assert (ws / "fonts" / "Urbanist-Bold.ttf").read_bytes() == _TTF
    assert (ws / "media" / "splash.mp4").read_bytes() == _MP4


def test_stage_archive_context_noop_for_legacy_run(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)  # no source/fonts/media seeded (v1.0 / apk)
    ws = rp.claude_ws
    ws.mkdir(parents=True, exist_ok=True)

    summary = stage_archive_context(rp, ws, include_bytes=True)

    assert summary == {"source": 0, "fonts": 0, "media": 0}
    assert not (ws / "source").exists()


def test_analyze_prompt_references_archive_context() -> None:
    for token in (
        "source/<id>.json",
        "fonts.json",
        "media.json",
        "GROUND TRUTH",
        "subscriptions.json",
        "sdks.json",
        "ads_raw.json",
        "EVIDENCE-BASED",
    ):
        assert token in ANALYZE_PROMPT


def test_analyze_workspace_stages_monetization_channels(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    _seed_archive(rp)
    rp.screens_json.write_text(
        json.dumps({"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]})
    )
    rp.subscriptions_json.write_text(json.dumps({"products": [], "purchase_attempts": []}))
    rp.sdks_json.write_text(json.dumps({"sdks": [{"sdk": "Firebase", "evidence": ["x"]}]}))
    rp.ads_raw_json.write_text(json.dumps({"ready_hooks": {"adblock": ["GADRewardedAd"]}}))

    analyze._prepare_analysis_workspace(rp)

    ws = rp.claude_ws
    assert (ws / "subscriptions.json").is_file()
    assert (ws / "sdks.json").is_file()
    assert (ws / "ads_raw.json").is_file()
    # these are analysis-only signal — codegen workspace must NOT carry them
    cg = rp.run_dir / "cg_ws"
    cg.mkdir(parents=True, exist_ok=True)
    stage_archive_context(rp, cg, include_bytes=True)
    assert not (cg / "sdks.json").exists()


def test_task_prompt_wires_fonts_media_and_source() -> None:
    scaffold = claude_gen._task_prompt(
        {"id": "t0", "type": "scaffold", "title": "x", "screens": []}
    )
    # divergent design: fonts come from google_fonts using the substituted families,
    # not by bundling the original app's .ttf files
    assert "google_fonts" in scaffold and "design_tokens.font" in scaffold

    screen = claude_gen._task_prompt(
        {"id": "t1", "type": "screen", "title": "Splash", "screens": ["0000"]}
    )
    assert "source/0000.json" in screen
    assert "asset_ref.media_id" in screen
    assert "splash_background" in screen


def test_task_prompt_no_ads_toggle() -> None:
    on = claude_gen._task_prompt({"id": "t", "type": "screen", "title": "x", "screens": []}, True)
    off = claude_gen._task_prompt({"id": "t", "type": "screen", "title": "x", "screens": []}, False)
    assert "NO ADS" in on and "google_mobile_ads" in on
    assert "NO ADS" not in off


def test_strip_ads_empties_monetization() -> None:
    from iosforge.mvp.analyze import _strip_ads

    spec = {
        "monetization": {
            "model": "freemium",
            "ad_networks": [{"name": "AdMob"}],
            "ad_placements": [{"format": "banner"}],
            "ads": ["banner bottom"],
            "packages": [{"name": "Weekly"}],
        }
    }
    _strip_ads(spec)
    m = spec["monetization"]
    assert m["ad_networks"] == [] and m["ad_placements"] == [] and m["ads"] == []
    assert m["packages"] == [{"name": "Weekly"}]  # paywalls/subscriptions preserved
