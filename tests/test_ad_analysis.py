"""Opt-in ad analysis: structured model from preserved ad frames, merged into spec."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import ad_analysis, spec_contract
from iosforge.mvp.paths import RunPaths


def _valid_spec() -> dict[str, Any]:
    return {
        "spec_version": spec_contract.SPEC_VERSION,
        "provenance": spec_contract.build_provenance(
            generator="t",
            generated_at="2026-07-03T00:00:00Z",
            source_crawl_sha256="x",
            screen_count=1,
        ),
        "app_name": "Drama",
        "app_type": "content-subscription",
        "description": "d",
        "how_it_works": "h",
        "market_research": {},
        "business_logic": {},
        "screens": [{"id": "0000", "name": "Home", "purpose": "list"}],
        "requirements": [{"id": "REQ-x", "type": "ubiquitous", "text": "t"}],
        "design_tokens": {},
        "navigation": {"type": "stack"},
        "content": {},
        "monetization": {"model": "mixed"},
        "backend": {},
        "permissions": [],
        "integrations": [],
        "cross_cutting": {},
        "analysis_quality": {
            "assumptions": [],
            "open_questions": [],
            "coverage_gaps": [],
            "confidence": {"overall": "high"},
        },
        "acceptance_criteria": ["a"],
    }


def _seed(tmp_path: Path, *, with_ads: bool, with_spec: bool) -> RunPaths:
    paths = RunPaths.create(tmp_path / "run")
    ads = []
    if with_ads:
        paths.ad_screens_dir.mkdir(parents=True, exist_ok=True)
        (paths.ad_screens_dir / "0002.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        ads = [
            {
                "id": "0002",
                "screenshot": "0002.png",
                "ordinal": 2,
                "prev_app_id": "0001",
                "next_app_id": "0003",
                "reason": "reelshort ad",
            }
        ]
    paths.screen_labels_json.write_text(json.dumps({"ads": ads, "counts": {"ads": len(ads)}}))
    if with_spec:
        paths.app_spec_json.write_text(json.dumps(_valid_spec()))
    return paths


def _canned_run(model: dict[str, Any]):
    def fake(prompt: str, workdir: Path, timeout: int) -> None:
        (Path(workdir) / "ad_analysis.json").write_text(json.dumps(model))

    return fake


def test_analyze_ads_normalises_and_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed(tmp_path, with_ads=True, with_spec=True)
    monkeypatch.setattr(
        ad_analysis,
        "_run_claude",
        _canned_run(
            {
                "ad_networks": [
                    {
                        "name": "AdMob",
                        "confidence": "low",
                        "evidence": "install card",
                        "source": "video_creative",
                    }
                ],
                "ad_placements": [
                    {
                        "format": "rewarded",
                        "trigger": "reward tap",
                        "screen_context": "Rewards",
                        "frequency": "on demand",
                        "frames": ["0002.png"],
                    },
                    {"format": "popup", "trigger": "invalid — should be dropped"},
                ],
                "notes": {"assumptions": []},
            }
        ),
    )

    out = ad_analysis.analyze_ads(paths)

    assert out == paths.ad_analysis_json
    model = json.loads(out.read_text())
    assert [p["format"] for p in model["ad_placements"]] == ["rewarded"]  # popup dropped
    assert model["ad_networks"][0]["name"] == "AdMob"

    # merged into the spec and still valid
    spec = json.loads(paths.app_spec_json.read_text())
    assert spec["monetization"]["ad_placements"][0]["format"] == "rewarded"
    assert spec_contract.validate_spec(spec)["monetization"]["ad_networks"][0]["name"] == "AdMob"


def test_analyze_ads_no_ads_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed(tmp_path, with_ads=False, with_spec=False)
    called = {"n": 0}

    def _boom(*a: object, **k: object) -> None:
        called["n"] += 1

    monkeypatch.setattr(ad_analysis, "_run_claude", _boom)

    assert ad_analysis.analyze_ads(paths) is None
    assert called["n"] == 0
